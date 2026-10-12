"""The GPT-Live bridge: the frames it puts on the wire.

GPT-Live does not call tools; a record-keeper beside it does, over the Responses
API. These cover the places a mistake is silent on a real call: a backend
attached to the voice session (the caller is kept waiting), a tool call that is
never applied (nothing is recorded), caller audio sent before the session exists
(an error per frame), and a hang-up that cuts the goodbye or the caller off.

Nothing here touches the network; the socket and the record-keeper are fakes.

Run with `pytest` from the repo root.
"""

from __future__ import annotations

import base64
import json

import pytest

from src import gptlive
from src.bridge import FINAL_MARK
from src.callrecord import CallRecord
from src.config import Config
from src.gptlive import GPTLiveBridge
from src.persona import Persona

SILENCE = base64.b64encode(b"\xff" * 160).decode()
SPEECH = base64.b64encode(b"\x10" * 160).decode()


@pytest.fixture()
def anyio_backend():
    return "asyncio"


class FakeTwilio:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    def marks(self) -> list[str]:
        return [m["mark"]["name"] for m in self.sent if m.get("event") == "mark"]


class FakeLive:
    """A scripted GPT-Live socket: yields server events, records client ones."""

    def __init__(self, events: list[dict]):
        self._events = list(events)
        self.sent: list[dict] = []

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if not self._events:
            raise StopAsyncIteration
        return json.dumps(self._events.pop(0))

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def of_type(self, name: str) -> list[dict]:
        return [m for m in self.sent if m.get("type") == name]


def make_bridge(monkeypatch, events: list[dict]):
    for key, value in {
        "VOICE_PROVIDER": "gpt-live",
        "OPENAI_API_KEY": "sk-test",
        "PUBLIC_BASE_URL": "https://example.test",
        "STREAM_TOKEN_SECRET": "0" * 64,
    }.items():
        monkeypatch.setenv(key, value)

    twilio = FakeTwilio()
    live = FakeLive(events)
    bridge = GPTLiveBridge(
        twilio_ws=twilio,
        record=CallRecord(call_sid="CA1", from_number="+447700900123"),
        cfg=Config(),
        instructions="You are a receptionist.",
        stream_sid="MZ1",
        persona=Persona(
            owner_name="Sam",
            owner_them="them",
            owner_their="their",
            assistant_name="Sam's assistant",
            locale_note="Speak plainly.",
        ),
    )
    bridge._provider = live
    return bridge, twilio, live


def _calls(*calls: tuple[str, dict]) -> dict:
    """A Responses API reply carrying these tool calls."""
    return {
        "output": [
            {"type": "function_call", "name": name, "arguments": json.dumps(args)}
            for name, args in calls
        ]
    }


def record_keeper(monkeypatch, bridge, reply: dict) -> list[dict]:
    """Stand in for the Responses API. Returns the requests it was sent."""
    asked: list[dict] = []

    async def fake(body: dict) -> dict:
        asked.append(body)
        return reply

    monkeypatch.setattr(bridge, "_ask_record_keeper", fake)
    return asked


@pytest.mark.anyio
async def test_the_voice_session_has_phone_audio_and_no_backend(monkeypatch):
    """Attached to the session, a backend makes the voice model stop and wait."""
    bridge, _, live = make_bridge(monkeypatch, [])
    await bridge._configure_session()

    session = live.of_type("session.start")[0]["session"]
    assert session["audio"]["format"] == {"type": "audio/pcmu", "rate": 8000}
    assert session["store"] is False
    assert "delegation" not in session
    assert "tools" not in session
    assert "You are a receptionist." in session["instructions"]
    # How to sound is said first and last.
    assert session["instructions"].count("Speak plainly.") == 2


@pytest.mark.anyio
async def test_caller_audio_waits_for_the_session(monkeypatch):
    bridge, _, live = make_bridge(monkeypatch, [])
    await bridge._send_caller_audio(SILENCE)
    assert live.sent == []

    bridge._session_ready.set()
    await bridge._send_caller_audio(SILENCE)
    assert live.of_type("session.input_audio.append") == [
        {"type": "session.input_audio.append", "audio": SILENCE}
    ]


@pytest.mark.anyio
async def test_the_record_keeper_sees_the_transcript_and_its_tools_are_applied(
    monkeypatch,
):
    bridge, _, live = make_bridge(
        monkeypatch,
        [
            {"type": "session.output_transcript.delta", "delta": "Who is calling?"},
            {"type": "session.input_transcript.delta", "delta": "It's Dave, "},
            {"type": "session.input_transcript.delta", "delta": "about the boiler."},
        ],
    )
    asked = record_keeper(
        monkeypatch,
        bridge,
        _calls(
            ("classify_call", {"category": "tradesperson_admin", "urgency": "low"}),
            ("take_message", {"caller_name": "Dave", "reason": "boiler"}),
        ),
    )
    await bridge._pump_provider()
    await bridge._record_pass()

    shown = asked[0]["input"]
    assert "Assistant: Who is calling?" in shown
    assert "Caller: It's Dave, about the boiler." in shown
    assert "Message: none yet" in shown
    assert {t["name"] for t in asked[0]["tools"]} >= {"take_message", "end_call"}
    assert asked[0]["store"] is False

    assert bridge._record.message_taken
    assert bridge._record.caller_name == "Dave"
    assert bridge._record.category == "tradesperson_admin"
    # Nothing about it reaches the voice session.
    assert live.sent == []


@pytest.mark.anyio
async def test_a_failing_record_keeper_does_not_end_the_call(monkeypatch):
    bridge, _, _ = make_bridge(monkeypatch, [])

    async def boom(body: dict) -> dict:
        raise RuntimeError("no network")

    monkeypatch.setattr(bridge, "_ask_record_keeper", boom)
    await bridge._record_pass()

    assert not bridge._closed.is_set()
    assert not bridge._record.message_taken


@pytest.mark.anyio
async def test_agent_audio_reaches_twilio(monkeypatch):
    bridge, twilio, _ = make_bridge(
        monkeypatch,
        [
            {"type": "session.output_audio.delta", "delta": SPEECH},
            {"type": "session.output_audio.delta", "delta": SILENCE},
        ],
    )
    await bridge._pump_provider()

    assert len([m for m in twilio.sent if m.get("event") == "media"]) == 2
    assert bridge._agent_last_spoke > 0


@pytest.mark.anyio
async def test_end_call_hangs_up_only_after_the_goodbye(monkeypatch):
    monkeypatch.setattr(gptlive, "CALLER_DONE_QUIET_S", 0.0)
    monkeypatch.setattr(gptlive, "FAREWELL_START_S", 0.0)
    monkeypatch.setattr(gptlive, "FAREWELL_QUIET_S", 0.0)
    bridge, twilio, live = make_bridge(monkeypatch, [])
    record_keeper(
        monkeypatch, bridge, _calls(("end_call", {"reason": "message_taken"}))
    )

    await bridge._record_pass()
    assert twilio.marks() == []
    await bridge._finisher

    assert "goodbye" in live.of_type("session.instructions.append")[0]["content"]
    assert twilio.marks() == [FINAL_MARK]


@pytest.mark.anyio
async def test_a_caller_who_carries_on_talking_is_not_hung_up_on(monkeypatch):
    """The record-keeper judges "they have finished" from a transcript, a few
    seconds late. Anything said before the line drops calls the hang-up off."""
    monkeypatch.setattr(gptlive, "CALLER_DONE_QUIET_S", 0.0)
    monkeypatch.setattr(gptlive, "FAREWELL_START_S", 0.0)
    monkeypatch.setattr(gptlive, "FAREWELL_QUIET_S", 0.2)
    bridge, twilio, _ = make_bridge(monkeypatch, [])
    record_keeper(
        monkeypatch, bridge, _calls(("end_call", {"reason": "message_taken"}))
    )

    await bridge._record_pass()
    bridge._handle_caller_transcript("oh, one more thing")
    await bridge._finisher

    assert twilio.marks() == []
    assert bridge._ending is False
    assert bridge._record.end_reason == ""


@pytest.mark.anyio
async def test_a_message_is_recovered_after_the_caller_hangs_up(monkeypatch):
    bridge, _, live = make_bridge(monkeypatch, [])
    bridge._started -= 30
    bridge._handle_caller_transcript("It's Dave about the boiler")
    asked = record_keeper(
        monkeypatch,
        bridge,
        _calls(("take_message", {"caller_name": "Dave", "reason": "boiler"})),
    )

    await bridge._extract_message_if_missing()

    assert asked[0]["tool_choice"] == "required"
    assert bridge._record.message_taken
    assert live.of_type("session.close") == [{"type": "session.close"}]

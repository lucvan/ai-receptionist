"""The GPT-Live bridge: the frames it puts on the wire.

GPT-Live does not call tools; a backend model does, and its events arrive
wrapped. These cover the places a mistake is silent on a real call: a tool
result that is never sent back (the backend waits for ever and nothing more is
recorded), caller audio sent before the session exists (an error per frame), and
a hang-up that cuts the goodbye off.

Nothing here touches the network; the OpenAI socket is a scripted fake.

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


def _tool_call(name: str, arguments: dict, call_id: str = "call_1") -> dict:
    return {
        "type": "response.event",
        "delegation_id": "item_1",
        "response": {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "name": name,
                "call_id": call_id,
                "arguments": json.dumps(arguments),
            },
        },
    }


@pytest.mark.anyio
async def test_the_session_asks_for_phone_audio_and_gives_the_backend_the_tools(
    monkeypatch,
):
    bridge, _, live = make_bridge(monkeypatch, [])
    await bridge._configure_session()

    session = live.of_type("session.start")[0]["session"]
    assert session["audio"]["format"] == {"type": "audio/pcmu", "rate": 8000}
    assert session["store"] is False
    backend = session["delegation"]["responses"]
    assert {t["name"] for t in backend["tools"]} >= {"take_message", "end_call"}
    # The voice model has no tools; both halves carry the prompt.
    assert "tools" not in session
    assert "You are a receptionist." in session["instructions"]
    assert "You are a receptionist." in backend["instructions"]


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
async def test_a_backend_tool_call_is_recorded_and_answered(monkeypatch):
    bridge, _, live = make_bridge(
        monkeypatch,
        [_tool_call("take_message", {"caller_name": "Dave", "reason": "boiler"})],
    )
    await bridge._pump_provider()

    assert bridge._record.message_taken
    assert bridge._record.caller_name == "Dave"
    output = live.of_type("response.item.create")[0]["item"]
    assert output["type"] == "function_call_output"
    assert output["call_id"] == "call_1"
    assert json.loads(output["output"]) == {"ok": True}
    # Without this the backend response stays parked on the tool result.
    assert live.of_type("response.create") == [{"type": "response.create"}]


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
    monkeypatch.setattr(gptlive, "FAREWELL_START_S", 0.0)
    monkeypatch.setattr(gptlive, "FAREWELL_QUIET_S", 0.0)
    bridge, twilio, live = make_bridge(monkeypatch, [])

    await bridge._dispatch_tool(
        _tool_call("end_call", {"reason": "message_taken"})["response"]["item"]
    )
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

    await bridge._dispatch_tool(
        _tool_call("end_call", {"reason": "message_taken"})["response"]["item"]
    )
    bridge._handle_caller_transcript("oh, one more thing")
    await bridge._finisher

    assert twilio.marks() == []
    assert bridge._ending is False
    assert bridge._record.end_reason == ""

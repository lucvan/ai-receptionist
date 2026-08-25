"""The ElevenLabs bridge, and picking a provider at all.

These cover the parts of the ElevenLabs path where a mistake is silent on a real
call rather than loud: a ping that goes unanswered (the line drops twenty seconds
in), a tool result that comes back marked successful when it was a rejection (the
agent carries on with no message), a barge-in that never clears Twilio's buffer
(the agent talks over the caller), and an audio format nobody checked (the caller
hears noise).

Nothing here touches the network. The ElevenLabs socket is a scripted fake, which
is the only way to assert on the exact frames the bridge puts on the wire -
which, for a protocol bridge, is the whole of its behaviour.

Run with `pytest` from the repo root.
"""

from __future__ import annotations

import base64
import json

import pytest

from src.bridge import FINAL_MARK
from src.callrecord import CallRecord
from src.config import Config
from src.elevenlabs import ElevenLabsBridge, _neutralise_placeholders
from src.persona import Persona

SILENCE = base64.b64encode(b"\xff" * 160).decode()


@pytest.fixture()
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeTwilio:
    """Just enough of a Starlette WebSocket for the bridge to talk to."""

    def __init__(self, frames: list[dict] | None = None):
        self._frames = list(frames or [])
        self.sent: list[dict] = []

    async def iter_text(self):
        for frame in self._frames:
            yield json.dumps(frame)

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    def events(self, name: str) -> list[dict]:
        return [m for m in self.sent if m.get("event") == name]

    def marks(self) -> list[str]:
        return [m["mark"]["name"] for m in self.events("mark")]


class FakeAgent:
    """A scripted ElevenLabs socket: yields server events, records client ones."""

    def __init__(self, events: list[dict] | None = None):
        self._events = list(events or [])
        self.sent: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

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


def make_bridge(
    monkeypatch,
    *,
    agent_events: list[dict] | None = None,
    twilio_frames: list[dict] | None = None,
    env: dict | None = None,
    record: CallRecord | None = None,
    **kwargs,
) -> tuple[ElevenLabsBridge, FakeTwilio, FakeAgent]:
    for key, value in {
        "VOICE_PROVIDER": "elevenlabs",
        "ELEVENLABS_AGENT_ID": "agent_test",
        "ELEVENLABS_API_KEY": "el-test",
        "PUBLIC_BASE_URL": "https://example.test",
        "STREAM_TOKEN_SECRET": "0" * 64,
        **(env or {}),
    }.items():
        monkeypatch.setenv(key, value)

    twilio = FakeTwilio(twilio_frames)
    agent = FakeAgent(agent_events)
    persona = Persona(
        owner_name="Sam",
        owner_them="them",
        owner_their="their",
        assistant_name="Sam's assistant",
        locale_note="Speak plainly.",
    )

    bridge = ElevenLabsBridge(
        twilio_ws=twilio,
        record=record or CallRecord(call_sid="CA1", from_number="+447700900123"),
        cfg=Config(),
        instructions="You are a receptionist.",
        stream_sid="MZ1",
        persona=persona,
        **kwargs,
    )

    async def _open(_self=bridge):
        return agent

    monkeypatch.setattr(bridge, "_open_provider", _open)
    return bridge, twilio, agent


# ---------------------------------------------------------------------------
# Wire protocol
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ping_is_answered_with_a_matching_pong(monkeypatch):
    """An unanswered ping closes the socket, which presents as a dropped call."""
    bridge, _, agent = make_bridge(
        monkeypatch,
        agent_events=[{"type": "ping", "ping_event": {"event_id": 77, "ping_ms": 30}}],
    )
    bridge._provider = agent
    await bridge._pump_provider()

    assert agent.of_type("pong") == [{"type": "pong", "event_id": 77}]


@pytest.mark.anyio
async def test_agent_audio_reaches_twilio_with_a_mark(monkeypatch):
    bridge, twilio, agent = make_bridge(
        monkeypatch,
        agent_events=[
            {"type": "audio", "audio_event": {"audio_base_64": SILENCE, "event_id": 1}}
        ],
    )
    bridge._provider = agent
    await bridge._pump_provider()

    media = twilio.events("media")
    assert len(media) == 1
    assert media[0]["streamSid"] == "MZ1"
    assert base64.b64decode(media[0]["media"]["payload"]) == b"\xff" * 160
    # The mark is what lets the closing line play out before the line drops.
    assert twilio.marks() == ["chunk"]


@pytest.mark.anyio
async def test_caller_audio_is_relayed_as_a_user_audio_chunk(monkeypatch):
    bridge, _, agent = make_bridge(
        monkeypatch,
        twilio_frames=[
            {"event": "media", "media": {"timestamp": "20", "payload": SILENCE}},
            {"event": "stop"},
        ],
    )
    bridge._provider = agent
    await bridge._pump_twilio()

    assert agent.sent == [{"user_audio_chunk": SILENCE}]


@pytest.mark.anyio
async def test_interruption_clears_twilio_playback(monkeypatch):
    """ElevenLabs truncates its own context; dropping Twilio's buffer is ours."""
    bridge, twilio, agent = make_bridge(
        monkeypatch,
        agent_events=[
            {"type": "audio", "audio_event": {"audio_base_64": SILENCE}},
            {"type": "interruption", "interruption_event": {"event_id": 4}},
        ],
    )
    bridge._provider = agent
    await bridge._pump_provider()

    assert len(twilio.events("clear")) == 1
    assert bridge._mark_queue == []
    assert bridge._response_start_ts is None
    # Nothing is sent back to the agent - it already knows.
    assert agent.sent == []


# ---------------------------------------------------------------------------
# Configuration and its silent failures
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_session_override_carries_this_call_s_prompt_and_opening_line(monkeypatch):
    bridge, _, agent = make_bridge(monkeypatch)
    bridge._provider = agent
    await bridge._configure_session()

    sent = agent.of_type("conversation_initiation_client_data")
    assert len(sent) == 1
    override = sent[0]["conversation_config_override"]
    # The caller's number has to reach the agent, or it asks for a number it
    # already has - the single most robotic thing it can do.
    assert "+447700900123" in override["agent"]["prompt"]["prompt"]
    assert override["agent"]["first_message"] == Config().greeting
    # Not sent unless configured: an empty voice_id would override the agent's
    # own voice with nothing.
    assert "tts" not in override


@pytest.mark.anyio
async def test_voice_and_language_are_only_sent_when_configured(monkeypatch):
    bridge, _, agent = make_bridge(
        monkeypatch,
        env={"ELEVENLABS_VOICE_ID": "voice_abc", "ELEVENLABS_LANGUAGE": "en"},
    )
    bridge._provider = agent
    await bridge._configure_session()

    override = agent.of_type("conversation_initiation_client_data")[0][
        "conversation_config_override"
    ]
    assert override["tts"] == {"voice_id": "voice_abc"}
    assert override["agent"]["language"] == "en"


@pytest.mark.anyio
async def test_a_known_contact_is_greeted_by_name(monkeypatch):
    record = CallRecord(
        call_sid="CA2", from_number="+447700900123", known_contact_name="Mia"
    )
    bridge, _, agent = make_bridge(monkeypatch, record=record)
    bridge._provider = agent
    await bridge._configure_session()

    first = agent.of_type("conversation_initiation_client_data")[0][
        "conversation_config_override"
    ]["agent"]["first_message"]
    assert first.startswith("Hello Mia,")


@pytest.mark.anyio
async def test_wrong_audio_format_is_reported_loudly(monkeypatch, caplog):
    """A PCM agent sounds like a broken phone line, not a wrong setting."""
    bridge, _, agent = make_bridge(
        monkeypatch,
        agent_events=[
            {
                "type": "conversation_initiation_metadata",
                "conversation_initiation_metadata_event": {
                    "conversation_id": "conv_9",
                    "agent_output_audio_format": "pcm_16000",
                    "user_input_audio_format": "ulaw_8000",
                },
            }
        ],
    )
    bridge._provider = agent
    with caplog.at_level("ERROR"):
        await bridge._pump_provider()

    assert "agent_output_audio_format is pcm_16000" in caplog.text
    # Recorded either way: it is what looks the call up in the dashboard.
    assert bridge._record.provider_conversation_id == "conv_9"


def test_stray_placeholders_are_flattened_not_left_to_abort_the_call():
    """ElevenLabs refuses a conversation whose prompt has an unfilled variable."""
    out = _neutralise_placeholders("Hello {{ owner_name }}, from {{x}}.", "CA1")
    assert out == "Hello owner_name, from x."
    assert _neutralise_placeholders("nothing here", "CA1") == "nothing here"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _tool_call(name: str, params: dict, call_id: str = "tc1") -> dict:
    return {
        "type": "client_tool_call",
        "client_tool_call": {
            "tool_name": name,
            "tool_call_id": call_id,
            "parameters": params,
        },
    }


@pytest.mark.anyio
async def test_take_message_records_it_and_acknowledges(monkeypatch):
    bridge, _, agent = make_bridge(
        monkeypatch,
        agent_events=[
            _tool_call(
                "take_message",
                {
                    "caller_name": "Mark Whitfield",
                    "reason": "Chasing the quote for the boiler.",
                    "summary": "Wants a call back about the boiler quote.",
                },
            )
        ],
    )
    bridge._provider = agent
    await bridge._pump_provider()

    assert bridge._record.message_taken is True
    assert bridge._record.caller_name == "Mark Whitfield"

    result = agent.of_type("client_tool_result")[0]
    assert result["tool_call_id"] == "tc1"
    assert result["is_error"] is False
    assert json.loads(result["result"]) == {"ok": True}


@pytest.mark.anyio
async def test_an_empty_take_message_is_rejected_as_an_error(monkeypatch):
    """`is_error` is what makes the agent go back and ask, rather than carry on."""
    bridge, _, agent = make_bridge(
        monkeypatch, agent_events=[_tool_call("take_message", {})]
    )
    bridge._provider = agent
    await bridge._pump_provider()

    assert bridge._record.message_taken is False
    result = agent.of_type("client_tool_result")[0]
    assert result["is_error"] is True
    assert "have not gathered anything" in json.loads(result["result"])["error"]


@pytest.mark.anyio
async def test_end_call_arms_the_countdown_to_hanging_up(monkeypatch):
    bridge, _, agent = make_bridge(
        monkeypatch,
        agent_events=[
            _tool_call("end_call", {"reason": "message_taken"}),
            {"type": "audio", "audio_event": {"audio_base_64": SILENCE}},
        ],
    )
    bridge._provider = agent
    await bridge._pump_provider()

    assert bridge._ending is True
    assert bridge._record.end_reason == "message_taken"
    # Armed, not fired. The agent goes on speaking after end_call, so the mark
    # waits for silence rather than landing mid-goodbye.
    assert bridge._final_mark_task is not None


@pytest.mark.anyio
async def test_the_countdown_ends_in_a_final_mark(monkeypatch):
    bridge, twilio, agent = make_bridge(monkeypatch)
    bridge._provider = agent
    monkeypatch.setattr("src.elevenlabs.END_OF_SPEECH_GRACE_S", 0)
    await bridge._final_mark_after_silence()

    assert twilio.marks() == [FINAL_MARK]


@pytest.mark.anyio
async def test_the_final_mark_is_what_ends_the_call(monkeypatch):
    """Twilio echoes it only once the goodbye has actually played out."""
    bridge, _, agent = make_bridge(
        monkeypatch,
        twilio_frames=[
            {"event": "mark", "mark": {"name": "chunk"}},
            {"event": "mark", "mark": {"name": FINAL_MARK}},
            {"event": "media", "media": {"timestamp": "40", "payload": SILENCE}},
        ],
    )
    bridge._provider = agent
    await bridge._pump_twilio()

    # Stopped at the final mark; the media frame after it was never read.
    assert agent.sent == []


@pytest.mark.anyio
async def test_transfer_stops_the_bridge_without_a_further_agent_turn(monkeypatch):
    """Acknowledging would start a billed turn spoken over Twilio's <Dial>."""
    bridge, _, agent = make_bridge(
        monkeypatch,
        env={"TRANSFER_ENABLED": "true", "TRANSFER_TO_NUMBER": "+447700900999"},
        agent_events=[_tool_call("transfer_call", {"reason": "Wife, says urgent."})],
    )
    bridge._provider = agent
    await bridge._pump_provider()

    assert bridge.transfer_requested is True
    assert bridge.transfer_reason == "Wife, says urgent."
    assert bridge._record.transfer_attempted is True
    assert agent.of_type("client_tool_result") == []
    assert bridge._closed.is_set()


@pytest.mark.anyio
async def test_transfer_is_refused_during_a_callback(monkeypatch):
    """A callback that could patch a stranger through is the whole risk here."""
    bridge, _, agent = make_bridge(
        monkeypatch,
        env={"TRANSFER_ENABLED": "true", "TRANSFER_TO_NUMBER": "+447700900999"},
        agent_events=[_tool_call("transfer_call", {"reason": "they asked"})],
        outbound_script="Sam says Friday works.",
        outbound_to_name="Mark",
    )
    bridge._provider = agent
    await bridge._pump_provider()

    assert bridge.transfer_requested is False
    result = agent.of_type("client_tool_result")[0]
    assert result["is_error"] is True
    assert "cannot transfer during a callback" in json.loads(result["result"])["error"]


@pytest.mark.anyio
async def test_extraction_is_skipped_when_the_caller_never_spoke(monkeypatch):
    """Found on a live silent call: it burned the whole extraction timeout
    asking the agent to summarise a conversation that never happened, turning a
    12s call into a 32s one. Screening lines get a lot of these."""
    bridge, _, agent = make_bridge(monkeypatch)
    bridge._provider = agent
    bridge._started -= 60          # old enough to be past the minimum
    assert bridge._caller_turns == 0

    await bridge._extract_message_if_missing()
    assert agent.of_type("user_message") == []

    # But a caller who did speak still gets one.
    bridge._caller_turns = 2
    await bridge._extract_message_if_missing()
    assert len(agent.of_type("user_message")) == 1


@pytest.mark.anyio
async def test_transfer_is_refused_once_the_caller_has_hung_up(monkeypatch):
    """Extraction runs after the call is over; there is nothing left to transfer."""
    bridge, _, agent = make_bridge(
        monkeypatch,
        env={"TRANSFER_ENABLED": "true", "TRANSFER_TO_NUMBER": "+447700900999"},
        agent_events=[_tool_call("transfer_call", {"reason": "they asked"})],
    )
    bridge._provider = agent
    bridge._extracting = True
    await bridge._pump_provider()

    assert bridge.transfer_requested is False
    result = agent.of_type("client_tool_result")[0]
    assert result["is_error"] is True
    assert "already ended" in json.loads(result["result"])["error"]


@pytest.mark.anyio
async def test_a_stalled_call_is_nudged_out_of_band(monkeypatch):
    """`contextual_update` rather than `user_message`: it must not read as the
    caller having said it."""
    bridge, _, agent = make_bridge(
        monkeypatch, env={"WRAP_UP_AFTER_TURNS": "2"}
    )
    bridge._provider = agent
    bridge._caller_turns = 5
    await bridge._maybe_nudge_wrap_up()

    update = agent.of_type("contextual_update")
    assert len(update) == 1
    assert "no message has been recorded" in update[0]["text"]
    assert agent.of_type("user_message") == []
    # Once only, however long the call runs on.
    await bridge._maybe_nudge_wrap_up()
    assert len(agent.of_type("contextual_update")) == 1


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_transcripts_are_dropped_unless_retention_is_on(monkeypatch):
    """ElevenLabs sends both sides for free, which is not a reason to keep them."""
    events = [
        {
            "type": "user_transcript",
            "user_transcription_event": {"user_transcript": "It's Mark."},
        },
        {
            "type": "agent_response",
            "agent_response_event": {"agent_response": "Hello Mark."},
        },
    ]
    bridge, _, agent = make_bridge(monkeypatch, agent_events=list(events))
    bridge._provider = agent
    await bridge._pump_provider()
    assert bridge._record.transcript == []
    # A caller turn still counts towards the loop guard.
    assert bridge._caller_turns == 1

    kept, _, agent2 = make_bridge(
        monkeypatch, agent_events=list(events), env={"RETAIN_TRANSCRIPTS": "true"}
    )
    kept._provider = agent2
    await kept._pump_provider()
    assert kept._record.transcript == [
        {"role": "caller", "text": "It's Mark."},
        {"role": "agent", "text": "Hello Mark."},
    ]


@pytest.mark.anyio
async def test_a_correction_rewrites_the_line_the_caller_actually_heard(monkeypatch):
    bridge, _, agent = make_bridge(
        monkeypatch,
        env={"RETAIN_TRANSCRIPTS": "true"},
        agent_events=[
            {
                "type": "agent_response",
                "agent_response_event": {"agent_response": "Let me take a message."},
            },
            {
                "type": "agent_response_correction",
                "agent_response_correction_event": {
                    "original_agent_response": "Let me take a message.",
                    "corrected_agent_response": "Let me take a",
                },
            },
        ],
    )
    bridge._provider = agent
    await bridge._pump_provider()

    # One utterance, cut short - not two.
    assert bridge._record.transcript == [{"role": "agent", "text": "Let me take a"}]

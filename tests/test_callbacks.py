"""Callbacks that land on a switchboard, transfers, and what the owner is told.

Each group here is a fault found on real calls in September 2026:

- a callback reached a switchboard recording, gave up, and was reported as the
  caller having answered;
- a caller was put on a silent line while the summary was written, hung up, and
  the transfer then failed - with a summary saying it had succeeded;
- a transfer the owner missed was diverted back to this number, so the
  receptionist answered itself;
- a caller mid-sentence was hung up on as dead air.

Keypad tones are checked by decoding them back with a Goertzel detector, the
standard way a DTMF receiver hears them, rather than by comparing bytes with
themselves.

Nothing here touches the network.
"""

from __future__ import annotations

import base64
import importlib
import json
import math
import time

import httpx
import pytest

from src import dtmf
from src.bridge import CALLER_AUDIO_ACTIVE_LEVEL, BaseBridge
from src.callrecord import CallRecord
from src.config import Config
from src.persona import Persona
from src.ulaw import SILENCE, linear_to_ulaw, mean_level, ulaw_to_linear

PERSONA = Persona(
    owner_name="Sam",
    owner_them="them",
    owner_their="their",
    assistant_name="Sam's assistant",
    locale_note="Speak plainly.",
)

BASE_ENV = {
    "PUBLIC_BASE_URL": "https://example.test",
    "STREAM_TOKEN_SECRET": "0" * 64,
}


@pytest.fixture()
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeTwilio:
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


class FakeSocket:
    """A provider socket that only records what is sent to it."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


class StubBridge(BaseBridge):
    """The provider-independent half, with every provider hook a no-op."""

    provider_name = "stub"

    async def _open_provider(self):
        raise NotImplementedError

    async def _configure_session(self) -> None:
        pass

    async def _pump_provider(self) -> None:
        pass

    async def _send_caller_audio(self, payload_b64: str) -> None:
        pass

    async def _greet_when_ready(self) -> None:
        pass


def _env(monkeypatch, tmp_path, extra: dict | None = None) -> None:
    for key, value in {
        **BASE_ENV,
        "SETTINGS_PATH": str(tmp_path / "settings.json"),
        **(extra or {}),
    }.items():
        monkeypatch.setenv(key, value)


def make_bridge(monkeypatch, tmp_path, *, outbound=True, env=None, frames=None,
                bridge_class=StubBridge, **kwargs):
    _env(monkeypatch, tmp_path, env)
    twilio = FakeTwilio(frames)
    record = CallRecord(
        call_sid="CA1",
        direction="outbound" if outbound else "inbound",
        from_number="+442079460001",
    )
    bridge = bridge_class(
        twilio_ws=twilio,
        record=record,
        cfg=Config(),
        instructions="You are a receptionist.",
        stream_sid="MZ1",
        persona=PERSONA,
        outbound_script="Friday works." if outbound else "",
        outbound_to_name="Jess" if outbound else "",
        **kwargs,
    )
    return bridge, twilio


# ---------------------------------------------------------------------------
# u-law and keypad tones
# ---------------------------------------------------------------------------


def _goertzel_power(samples: list[int], freq: float) -> float:
    coeff = 2 * math.cos(2 * math.pi * freq / dtmf.SAMPLE_RATE)
    s1 = s2 = 0.0
    for x in samples:
        s1, s2 = x + coeff * s1 - s2, s1
    return s1 * s1 + s2 * s2 - coeff * s1 * s2


def _detect(chunk: bytes) -> str | None:
    """Which key a chunk of u-law audio is, as a DTMF receiver would hear it."""
    samples = [ulaw_to_linear(b) for b in chunk]
    lows = {f: _goertzel_power(samples, f) for f in (697, 770, 852, 941)}
    highs = {f: _goertzel_power(samples, f) for f in (1209, 1336, 1477)}
    for group in (lows, highs):
        ranked = sorted(group.values(), reverse=True)
        # One clear winner per group, or it is not a valid tone.
        if ranked[0] < 10 * ranked[1]:
            return None
    pair = (max(lows, key=lows.get), max(highs, key=highs.get))
    return next((k for k, v in dtmf.FREQUENCIES.items() if v == pair), None)


def test_ulaw_round_trips_within_one_quantisation_step():
    for x in [*range(-32768, 32768, 97), 0, 1, -1, 32767, -32768]:
        y = ulaw_to_linear(linear_to_ulaw(x))
        assert abs(y - x) <= max(16, abs(x) * 0.04), (x, y)


def test_a_quiet_line_is_below_the_activity_level_and_a_tone_is_not():
    assert mean_level(bytes([SILENCE]) * 160) < CALLER_AUDIO_ACTIVE_LEVEL
    assert mean_level(dtmf.tones("5")[160:320]) > CALLER_AUDIO_ACTIVE_LEVEL


@pytest.mark.parametrize("key", list(dtmf.FREQUENCIES))
def test_every_key_is_heard_as_itself(key):
    audio = dtmf.tones(key)
    tone_n = dtmf.SAMPLE_RATE * dtmf.TONE_MS // 1000
    assert _detect(audio[:tone_n]) == key
    assert mean_level(audio[tone_n:]) < 1, "the gap after a tone must be silent"


def test_an_extension_is_played_in_order_and_on_time():
    keys = "4417#"
    audio = dtmf.tones(keys)
    step = dtmf.SAMPLE_RATE * (dtmf.TONE_MS + dtmf.GAP_MS) // 1000
    tone_n = dtmf.SAMPLE_RATE * dtmf.TONE_MS // 1000
    heard = [_detect(audio[i * step : i * step + tone_n]) for i in range(len(keys))]
    assert "".join(k or "?" for k in heard) == keys
    assert dtmf.duration_s(keys) == pytest.approx(len(audio) / dtmf.SAMPLE_RATE)


@pytest.mark.parametrize("bad", ["", "12a", "1 2", "+44"])
def test_only_keypad_characters_become_tones(bad):
    with pytest.raises(ValueError):
        dtmf.tones(bad)


def test_tones_are_sent_in_frames_the_size_twilio_uses():
    frames = dtmf.frames(dtmf.tones("1"))
    assert frames
    assert all(len(base64.b64decode(f)) == dtmf.FRAME_BYTES for f in frames)


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------


def test_switchboard_tools_are_offered_only_on_a_callback():
    from src.tools import tool_specs

    navigation = {"press_keys", "wait_on_hold", "record_callback_outcome"}
    inbound = {t["name"] for t in tool_specs(include_transfer=True)}
    outbound = {t["name"] for t in tool_specs(include_transfer=True, outbound=True)}

    assert not navigation & inbound
    assert navigation <= outbound
    assert "transfer_call" not in outbound


@pytest.mark.anyio
async def test_pressing_keys_plays_the_tones_into_the_call(monkeypatch, tmp_path):
    bridge, twilio = make_bridge(monkeypatch, tmp_path)

    result = await bridge._run_tool("press_keys", {"keys": "2", "why": "deliveries"})

    assert result["ok"] is True
    played = b"".join(
        base64.b64decode(m["media"]["payload"]) for m in twilio.events("media")
    )
    assert played.startswith(dtmf.tones("2"))
    assert bridge._record.keys_pressed == "2"


@pytest.mark.anyio
async def test_an_inbound_caller_cannot_get_the_agent_to_press_keys(monkeypatch, tmp_path):
    bridge, twilio = make_bridge(monkeypatch, tmp_path, outbound=False)

    result = await bridge._run_tool("press_keys", {"keys": "9", "why": "dial out"})

    assert result["ok"] is False
    assert not twilio.events("media")


@pytest.mark.anyio
@pytest.mark.parametrize("keys", ["", "9w", "0044123456789", "1-2"])
async def test_only_short_keypad_sequences_are_accepted(monkeypatch, tmp_path, keys):
    bridge, twilio = make_bridge(monkeypatch, tmp_path)

    result = await bridge._run_tool("press_keys", {"keys": keys, "why": "x"})

    assert result["ok"] is False
    assert not twilio.events("media")


@pytest.mark.anyio
async def test_key_presses_are_capped_per_call(monkeypatch, tmp_path):
    bridge, _ = make_bridge(
        monkeypatch, tmp_path, env={"CALLBACK_MAX_KEY_PRESSES": "2"}
    )

    assert (await bridge._run_tool("press_keys", {"keys": "1"}))["ok"]
    assert (await bridge._run_tool("press_keys", {"keys": "2"}))["ok"]
    third = await bridge._run_tool("press_keys", {"keys": "3"})

    assert third["ok"] is False
    assert "automated_dead_end" in third["error"]
    assert bridge._record.keys_pressed == "1 2"


@pytest.mark.anyio
async def test_a_menu_talking_over_the_tones_does_not_wipe_them(monkeypatch, tmp_path):
    bridge, twilio = make_bridge(monkeypatch, tmp_path)
    await bridge._run_tool("press_keys", {"keys": "1234"})
    twilio.sent.clear()

    bridge._mark_queue.append("chunk")
    await bridge._clear_twilio_playback()

    assert not twilio.events("clear")


def test_the_outcome_must_be_one_the_summary_understands(monkeypatch, tmp_path):
    bridge, _ = make_bridge(monkeypatch, tmp_path)

    bad = bridge._apply_tool(
        "record_callback_outcome", {"outcome": "delivered", "spoke_to": "someone"}
    )
    good = bridge._apply_tool(
        "record_callback_outcome",
        {"outcome": "callback_requested", "spoke_to": "the switchboard"},
    )

    assert bad["ok"] is False
    assert good["ok"] is True
    assert bridge._record.callback_outcome == "callback_requested"
    assert bridge._extraction_complete()


def test_hold_stretches_the_silence_guard(monkeypatch, tmp_path):
    bridge, _ = make_bridge(
        monkeypatch,
        tmp_path,
        env={"SILENCE_HANGUP_SECONDS": "25", "CALLBACK_HOLD_MAX_S": "180"},
    )
    assert bridge._silence_limit() == 25

    bridge._apply_tool("wait_on_hold", {"waiting_for": "deliveries"})

    assert bridge._silence_limit() == 180


def test_a_callback_knows_who_to_ask_a_switchboard_for(monkeypatch, tmp_path):
    bridge, _ = make_bridge(
        monkeypatch,
        tmp_path,
        outbound_to_company="Example Garden Centre, deliveries",
        outbound_extension="214",
        outbound_about="Arranging a furniture delivery for Thursday.",
        outbound_category="delivery_appointment",
    )

    text = bridge._session_instructions()

    for expected in ("Jess", "Example Garden Centre, deliveries", "214",
                     "furniture delivery", "delivery appointment"):
        assert expected in text


def test_a_callback_nobody_spoke_on_is_recorded_as_unanswered(monkeypatch, tmp_path):
    bridge, _ = make_bridge(monkeypatch, tmp_path)

    async def _open():
        raise RuntimeError("no provider in this test")

    monkeypatch.setattr(bridge, "_open_provider", _open)
    import asyncio

    record = asyncio.run(bridge.run())
    assert record.callback_outcome == "no_one_answered"


# ---------------------------------------------------------------------------
# Silence guard
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_sound_on_the_line_counts_as_activity(monkeypatch, tmp_path):
    """The 2026-09-10 hang-up: a caller mid-sentence was treated as dead air."""
    loud = dtmf.frames(dtmf.tones("5"))[1]
    quiet = base64.b64encode(bytes([SILENCE]) * 160).decode()

    talking, _ = make_bridge(
        monkeypatch, tmp_path, outbound=False,
        frames=[{"event": "media", "media": {"timestamp": "20", "payload": loud}}],
    )
    talking._last_activity = time.monotonic() - 60
    await talking._pump_twilio()
    assert time.monotonic() - talking._last_activity < 5

    silent, _ = make_bridge(
        monkeypatch, tmp_path, outbound=False,
        frames=[{"event": "media", "media": {"timestamp": "20", "payload": quiet}}],
    )
    silent._last_activity = time.monotonic() - 60
    await silent._pump_twilio()
    assert time.monotonic() - silent._last_activity > 50


# ---------------------------------------------------------------------------
# The OpenAI bridge
# ---------------------------------------------------------------------------


def make_openai_bridge(monkeypatch, tmp_path, **kwargs):
    from src.realtime import CallBridge

    bridge, twilio = make_bridge(
        monkeypatch, tmp_path, bridge_class=CallBridge,
        env={"OPENAI_API_KEY": "sk-test"}, **kwargs,
    )
    socket = FakeSocket()
    bridge._provider = socket
    return bridge, socket


@pytest.mark.anyio
async def test_after_pressing_keys_the_agent_listens_rather_than_talks(monkeypatch, tmp_path):
    bridge, socket = make_openai_bridge(monkeypatch, tmp_path)

    await bridge._dispatch_tool({
        "name": "press_keys", "call_id": "c1",
        "arguments": json.dumps({"keys": "2", "why": "deliveries"}),
    })
    assert [m["type"] for m in socket.sent] == ["conversation.item.create"]

    await bridge._dispatch_tool({
        "name": "take_message", "call_id": "c2",
        "arguments": json.dumps({"caller_name": "Jess", "reason": "Said thanks"}),
    })
    assert socket.sent[-1]["type"] == "response.create"


@pytest.mark.anyio
async def test_a_callback_session_carries_the_switchboard_tools(monkeypatch, tmp_path):
    bridge, socket = make_openai_bridge(monkeypatch, tmp_path)

    await bridge._configure_session()

    names = {t["name"] for t in socket.sent[0]["session"]["tools"]}
    assert {"press_keys", "wait_on_hold", "record_callback_outcome"} <= names


@pytest.mark.anyio
async def test_a_callback_lets_whoever_answers_speak_first(monkeypatch, tmp_path):
    from src import realtime

    monkeypatch.setattr(realtime, "OUTBOUND_OPENING_WAIT_S", 0.05)
    greeted: list[str] = []

    answered, _ = make_openai_bridge(monkeypatch, tmp_path)

    async def greet_answered():
        greeted.append("answered")

    monkeypatch.setattr(answered, "_greet", greet_answered)
    answered._session_ready.set()
    answered._note_caller_turn()  # "Example Garden Centre, how can I help?"
    await answered._greet_when_ready()

    silent, _ = make_openai_bridge(monkeypatch, tmp_path)

    async def greet_silent():
        greeted.append("silent")

    monkeypatch.setattr(silent, "_greet", greet_silent)
    silent._session_ready.set()
    await silent._greet_when_ready()

    assert greeted == ["silent"]


# ---------------------------------------------------------------------------
# What the summary is given, and the local fallback
# ---------------------------------------------------------------------------


def test_a_callback_summary_is_not_told_the_intended_person_answered():
    record = CallRecord(
        call_sid="CA1", direction="outbound", from_number="+442079460001",
        caller_name="Jess", callback_for="Jess",
    )

    payload = record.to_supervisor_payload()
    assert payload["callback_for"] == "Jess"
    assert payload["callback_outcome"] == "not_recorded"
    assert "caller_name" not in payload

    record.callback_outcome = "delivered_to_person"
    record.spoke_to = "Jess"
    assert record.to_supervisor_payload()["caller_name"] == "Jess"


def test_a_transfer_summary_is_written_as_still_in_progress():
    record = CallRecord(
        call_sid="CA1", end_reason="transferred", transfer_reason="Wants a chat"
    )

    ringing = record.to_supervisor_payload("ringing")
    assert ringing["end_reason"] == "being_put_through"
    assert ringing["transfer_reason"] == "Wants a chat"
    assert record.to_supervisor_payload("failed")["end_reason"] == "transfer_failed"
    assert "transfer_status" not in record.to_supervisor_payload()


def test_fallback_for_an_unclear_callback_does_not_claim_delivery():
    from src.notify import format_fallback

    text = format_fallback({
        "direction": "outbound", "callback_for": "Jess",
        "callback_outcome": "not_recorded", "callback_number": "+442079460001",
    })

    assert "Jess" in text and "isn't clear" in text
    assert "gave them your message" not in text
    assert text.endswith("+442079460001")


def test_fallback_for_a_message_left_with_a_colleague_names_them():
    from src.notify import format_fallback

    text = format_fallback({
        "direction": "outbound", "callback_for": "Jess",
        "callback_outcome": "delivered_to_colleague", "spoke_to": "Tom in deliveries",
    })

    assert "Tom in deliveries" in text


def test_fallback_for_a_transfer_in_progress_says_so_and_why():
    from src.notify import format_fallback

    text = format_fallback({
        "category": "family_friend_personal", "known_contact_name": "Dana",
        "end_reason": "being_put_through", "transfer_reason": "Wants a general chat",
    })

    assert "Putting them through" in text
    assert "general chat" in text
    assert "rang off" not in text


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------


def test_update_call_amends_only_that_record(tmp_path):
    from src.history import CallHistory

    path = tmp_path / "calls-2026-09.jsonl"
    path.write_text(
        "".join(
            json.dumps({
                "call_sid": sid, "direction": "inbound",
                "from_number": "+442079460001",
                "started_at": "2026-09-10T11:43:04+00:00",
                "transfer_connected": False,
            }) + "\n"
            for sid in ("CA1", "CA2")
        ),
        encoding="utf-8",
    )
    history = CallHistory(tmp_path)

    assert history.update_call("CA2", transfer_connected=True)
    assert not history.update_call("CA9", transfer_connected=True)

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [r["transfer_connected"] for r in records] == [False, True]


def test_pending_calls_saved_before_the_switchboard_fields_still_load(tmp_path):
    from src.pending import PendingStore

    path = tmp_path / "pending-calls.json"
    path.write_text(json.dumps({
        "42": {
            "call_sid": "CA1", "number": "+442079460001", "caller_name": "Jess",
            "category": "delivery_appointment", "summary": "x",
            "created_at": time.time(),
        }
    }), encoding="utf-8")

    call = PendingStore(path).get(42)

    assert call is not None
    assert call.company == "" and call.extension == ""


# ---------------------------------------------------------------------------
# Server: transfers
# ---------------------------------------------------------------------------


def _server(monkeypatch, tmp_path, **extra):
    _env(monkeypatch, tmp_path, {
        "VALIDATE_TWILIO_SIGNATURE": "false",
        "OPENAI_API_KEY": "sk-test",
        "TWILIO_PHONE_NUMBER": "+442079460999",
        "CONTACTS_PATH": str(tmp_path / "contacts.json"),
        "LOG_DIR": str(tmp_path),
        **extra,
    })
    import src.config

    importlib.reload(src.config)
    import src.server

    return importlib.reload(src.server)


def test_a_transfer_diverted_back_to_our_own_number_is_rejected(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    server = _server(monkeypatch, tmp_path)
    client = TestClient(server.app)

    looped = client.post(
        "/incoming-call",
        data={"CallSid": "CA2", "From": "+44 20 7946 0999", "To": "+442079460999"},
    ).text
    ordinary = client.post(
        "/incoming-call", data={"CallSid": "CA3", "From": "+442079460001"}
    ).text

    assert "<Reject" in looped and "<Stream" not in looped
    assert "<Reject" not in ordinary


@pytest.mark.anyio
async def test_the_caller_is_put_through_before_the_summary_is_written(monkeypatch, tmp_path):
    server = _server(monkeypatch, tmp_path)
    order: list = []

    async def finish(record, transfer_state=""):
        order.append(("finish", transfer_state))

    async def redirected(call_sid, url):
        order.append("redirect")
        return True, ""

    async def too_late(call_sid, url):
        order.append("redirect")
        return False, "Call is not in-progress. Cannot redirect."

    monkeypatch.setattr(server, "_finish_call", finish)

    monkeypatch.setattr(server.outbound, "redirect_call", redirected)
    await server._start_transfer(CallRecord(call_sid="CA1", caller_name="Alex"), "x")
    assert order == ["redirect", ("finish", "ringing")]

    order.clear()
    monkeypatch.setattr(server.outbound, "redirect_call", too_late)
    await server._start_transfer(CallRecord(call_sid="CA2", caller_name="Alex"), "x")
    assert order == ["redirect", ("finish", "failed")]


def _transfer_result(monkeypatch, tmp_path, status: str, has_message: bool):
    from fastapi.testclient import TestClient

    server = _server(monkeypatch, tmp_path)
    sent: list[str] = []

    async def send(text, **_kwargs):
        sent.append(text)
        return {}

    monkeypatch.setattr(server.notifier, "send", send)
    log = tmp_path / "calls-2026-09.jsonl"
    log.write_text(json.dumps({
        "call_sid": "CA1", "direction": "inbound", "from_number": "+442079460001",
        "started_at": "2026-09-09T09:27:46+00:00", "transfer_connected": False,
    }) + "\n", encoding="utf-8")
    server.transfers.put("tok", {
        "call_sid": "CA1", "caller_name": "Dana",
        "reason": "Wants a general chat.", "has_message": has_message,
    })

    body = TestClient(server.app).post(
        "/transfer-result?token=tok", data={"DialCallStatus": status}
    ).text
    record = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    return body, sent, record


def test_a_missed_transfer_says_why_they_rang_and_whether_there_is_a_message(monkeypatch, tmp_path):
    body, sent, record = _transfer_result(monkeypatch, tmp_path, "busy", False)

    assert "couldn't reach" in body
    assert "general chat" in sent[0]
    assert "didn't leave a message" in sent[0]
    assert record["transfer_connected"] is False


def test_an_answered_transfer_is_written_back_to_the_call_log(monkeypatch, tmp_path):
    _, sent, record = _transfer_result(monkeypatch, tmp_path, "completed", True)

    assert sent and sent[0].startswith("✅")
    assert record["transfer_connected"] is True


# ---------------------------------------------------------------------------
# ElevenLabs: its own post-call summary as the backstop
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_elevenlabs_post_call_summary_is_polled_until_ready(monkeypatch):
    from src import elevenlabs

    replies = iter([
        {"status": "processing", "analysis": None},
        {"status": "done",
         "analysis": {"transcript_summary": "Dana asked whether Sam has the house keys."}},
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["xi-api-key"] == "el-test"
        return httpx.Response(200, json=next(replies))

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        elevenlabs.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )

    summary = await elevenlabs.fetch_transcript_summary(
        "conv_1", "el-test", wait_s=5, poll_s=0
    )

    assert "house keys" in summary


@pytest.mark.anyio
async def test_elevenlabs_summary_gives_up_on_a_hard_error(monkeypatch):
    from src import elevenlabs

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        elevenlabs.httpx,
        "AsyncClient",
        lambda **kw: real_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(401)), **kw
        ),
    )

    assert await elevenlabs.fetch_transcript_summary("conv_1", "bad", wait_s=5, poll_s=0) == ""

"""The OpenAI Realtime bridge: what reaches the caller's ear.

Reasoning realtime models (`gpt-realtime-2` onwards) speak a "commentary" line
before a tool call or an answer. Played out alongside the reply the bridge asks
for after every tool, the caller hears each response twice - so that audio must
never reach Twilio, while the answer itself must.

Nothing here touches the network; the OpenAI socket is a scripted fake.

Run with `pytest` from the repo root.
"""

from __future__ import annotations

import base64
import json

import pytest

from src.callrecord import CallRecord
from src.config import Config
from src.persona import Persona
from src.realtime import CallBridge

FRAME = base64.b64encode(b"\xff" * 160).decode()


@pytest.fixture()
def anyio_backend():
    return "asyncio"


class FakeTwilio:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    def media(self) -> list[dict]:
        return [m for m in self.sent if m.get("event") == "media"]


class FakeOpenAI:
    """A scripted Realtime socket: yields server events, records client ones."""

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


def make_bridge(monkeypatch, events: list[dict]):
    for key, value in {
        "VOICE_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-test",
        "PUBLIC_BASE_URL": "https://example.test",
        "STREAM_TOKEN_SECRET": "0" * 64,
    }.items():
        monkeypatch.setenv(key, value)

    twilio = FakeTwilio()
    openai = FakeOpenAI(events)
    bridge = CallBridge(
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
    bridge._provider = openai
    return bridge, twilio, openai


def _item(item_id: str, phase: str | None) -> dict:
    item = {"id": item_id, "type": "message", "role": "assistant"}
    if phase:
        item["phase"] = phase
    return {"type": "response.output_item.added", "item": item}


def _audio(item_id: str) -> dict:
    return {"type": "response.output_audio.delta", "item_id": item_id, "delta": FRAME}


@pytest.mark.anyio
async def test_commentary_audio_is_not_played_to_the_caller(monkeypatch):
    bridge, twilio, _ = make_bridge(
        monkeypatch,
        [
            _item("item_c", "commentary"),
            _audio("item_c"),
            _audio("item_c"),
            _item("item_a", "final_answer"),
            _audio("item_a"),
        ],
    )
    await bridge._pump_provider()

    assert len(twilio.media()) == 1
    # Barge-in truncates the item the caller was actually hearing.
    assert bridge._last_assistant_item == "item_a"


@pytest.mark.anyio
async def test_audio_without_a_phase_is_played(monkeypatch):
    """Non-reasoning models never set a phase, and must sound as they did."""
    bridge, twilio, _ = make_bridge(
        monkeypatch, [_item("item_a", None), _audio("item_a"), _audio("item_a")]
    )
    await bridge._pump_provider()

    assert len(twilio.media()) == 2

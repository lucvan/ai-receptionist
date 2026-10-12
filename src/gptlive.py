"""The live call bridge for OpenAI GPT-Live: Twilio Media Streams <-> OpenAI.

A different API from `realtime.py`, not a newer model on the same one. GPT-Live
is full duplex: it listens while it speaks and decides for itself when to talk,
so there is no turn detection to configure, no response to request after the
caller stops, and no barge-in to enforce. What it does not do is call tools. A
second, text-only model - the *backend* - is handed the conversation and the
tools, and GPT-Live decides when to ask it for help ("delegation").

That split is the shape of this file, and it has four habits worth knowing:

- **The bridge runs the backend, not the voice model.** Left to delegate, the
  voice model stops and says "just a moment" each time, and then forgets to ask
  when the caller says goodbye. So it is told not to, and the bridge sets the
  backend going itself after each thing the caller says.
- **Two prompts.** The voice model gets the receptionist prompt plus a note
  that the record is kept for it. The backend gets the same prompt - it has to know what may
  never be said, since the voice model may speak whatever it replies - plus a
  note that it is the record-keeper and the caller cannot hear it.
- **Audio arrives at the pace it is spoken**, silence included. So nothing is
  ever queued far ahead in Twilio, and there is no end-of-response event: "the
  agent has finished speaking" is measured off the audio itself.
- **The backend's events arrive wrapped.** A tool call is a
  `response.output_item.done` inside a `response.event` envelope, answered with
  `response.item.create` and then `response.create` to let the backend go on.
- **Nothing happens before `session.started`.** Caller audio sent earlier is an
  error, so it is dropped until then.

Both sides carry G.711 u-law at 8 kHz, so audio is passed through untouched.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from .bridge import BaseBridge
from .tools import tool_specs
from .ulaw import mean_level

log = logging.getLogger(__name__)

OPENAI_LIVE_URL = "wss://api.openai.com/v1/live/sessions"

EVT_STARTED = "session.started"
EVT_AUDIO_DELTA = "session.output_audio.delta"
EVT_INPUT_TRANSCRIPT = "session.input_transcript.delta"
EVT_OUTPUT_TRANSCRIPT = "session.output_transcript.delta"
EVT_BACKEND = "response.event"
EVT_DELEGATED = "session.delegation.created"
EVT_CLOSED = "session.closed"
EVT_ERROR = "error"

# Inside a `response.event` envelope.
BACKEND_CREATED = "response.created"
BACKEND_ITEM_DONE = "response.output_item.done"
BACKEND_COMPLETED = "response.completed"
BACKEND_FAILED = "response.failed"

# Agent audio louder than this is speech, on the scale `ulaw.mean_level` uses.
# The stream never stops, so quiet frames are how a finished sentence looks.
AGENT_SPEAKING_LEVEL = 300.0

# A gap this long in the caller's transcript separates one turn from the next.
# There is no end-of-turn event; the guards only need a rough count.
CALLER_TURN_GAP_S = 1.5

# Ceiling on backend tool calls between caller turns, to bound a tool loop. See
# the same constant in `realtime.py` for why it sits above four.
MAX_TOOL_CALLS_PER_CALLER_TURN = 8

# How long whoever answers a callback is given to speak first.
OUTBOUND_OPENING_WAIT_S = 3.0

# How long after the caller's transcript stops the record-keeper is asked to
# look at what they said.
RECORD_AFTER_QUIET_S = 1.0

# A hang-up waits for the caller to have been quiet this long, and is called off
# if they say anything in the meantime.
CALLER_DONE_QUIET_S = 1.5

# Hanging up: how long the goodbye is given to start, how much quiet after it
# counts as finished, and the most the whole thing may take.
FAREWELL_START_S = 2.5
FAREWELL_QUIET_S = 0.9
FAREWELL_MAX_S = 9.0

VOICE_NOTE = """

---

## How your tools work on this line

You do not call tools yourself, and you do not keep the record. A record-keeper
working alongside you holds every tool these instructions mention - classifying
the call, taking the message, flagging something urgent, putting a caller
through, pressing keys, ending the call. It hears the whole conversation and
acts on its own, without being asked. Wherever these instructions say to call a
tool, that is the record-keeper's job, not yours. Your job is the conversation.

So never delegate to the backend, and never pause for it. Do not say "just a
moment", "one second" or "let me note that down", and never mention the
record-keeper.

The record-keeper reports back to you as it works. Those reports are not part of
the conversation: say nothing when one arrives - no "right", no "lovely", no
acknowledgement, no repeating yourself - and never read one out. Only speak in
reply to the caller.

When the caller has nothing more to add, say one short goodbye. The
record-keeper then closes the line.
"""

BACKEND_NOTE = """

---

## Your part in this call

You are the record-keeper. A separate voice assistant is talking to the caller
and follows the instructions above; the caller cannot hear you, and you never
speak to them. You hold the tools, and your whole job is to call them, as those
instructions describe, using only what the caller actually said.

You are asked after each thing the caller says. Most of the time nothing needs
doing: then call no tool. Otherwise:

- Classify the call once it is clear what kind it is. Once is enough.
- Take the message once you know who is calling and what they want. Take it
  again only if they have added something that changes it.
- Flag it if it is urgent, and put the caller through if they ask and the
  instructions above allow it.
- End the call once the caller has said goodbye, or has clearly said they have
  nothing more - after making sure the message is recorded.

**Never end the call while the caller might still have something to say.**
Ending it early cuts them off mid-sentence, and "thanks" on its own is not a
goodbye.

Whatever you did, finish by replying with exactly this and nothing else:
"(record updated - nothing to say to the caller)". Never a question, never a
sentence meant for the caller, and never anything from the do-not-disclose list.
"""


class GPTLiveBridge(BaseBridge):
    """One phone call, answered by a GPT-Live session with a tool-calling backend."""

    provider_name = "gpt-live"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # When the agent was last audibly speaking, and when the caller's
        # transcript last moved. Both stand in for events this API does not have.
        self._agent_last_spoke = 0.0
        self._caller_last_heard = 0.0
        self._tool_calls_since_caller = 0
        # When the voice model last handed work to the backend, for the log: a
        # pause in the conversation is nearly always one of these.
        self._delegated_at = 0.0
        # Backend responses started and not yet finished, and whether one has
        # been asked for that has not started yet. Counted off the backend's own
        # created/completed events, since a response begins three ways: the
        # voice model delegates, we ask, or a tool result sends it on its way.
        self._backend_outstanding = 0
        self._backend_starting = False
        # The caller speech the record-keeper has already been asked about.
        self._recorded_up_to = 0.0
        # The one task that plays out the goodbye and then hangs up.
        self._finisher: asyncio.Task | None = None

    @property
    def _backend_busy(self) -> bool:
        return self._backend_starting or self._backend_outstanding > 0

    @property
    def _live(self) -> ClientConnection | None:
        return self._provider

    # -- session -----------------------------------------------------------

    async def _open_provider(self) -> Any:
        headers = {"Authorization": f"Bearer {self._cfg.openai_api_key}"}
        return websockets.connect(
            OPENAI_LIVE_URL, additional_headers=headers, max_size=None
        )

    def _voice_instructions(self) -> str:
        """The prompt, with how to sound said first and last.

        The accent guidance sits a long way down a long prompt, and a voice
        model that has drifted from it does not go back and look. Opening and
        closing with it is what keeps it in force.
        """
        accent = self._persona.locale_note
        return (
            f"## How you sound, above everything else\n\n{accent}\n\n---\n\n"
            + self._session_instructions()
            + VOICE_NOTE
            + f"\n## Before every word you say\n\n{accent}\n"
        )

    def _backend_config(self) -> dict[str, Any]:
        return {
            "model": self._cfg.openai_live_backend_model,
            "instructions": self._session_instructions() + BACKEND_NOTE,
            # Transfer is only offered on inbound calls, and only when enabled.
            # A callback must never be able to patch someone through.
            "tools": tool_specs(self._transfer_available(), outbound=self.is_outbound),
            "tool_choice": "auto",
            # One call at a time: each result is acted on before the next.
            "parallel_tool_calls": False,
            # Filing a message is not a hard problem, and the caller is waiting
            # on the hang-up while it is thought about.
            "reasoning": {"effort": "low"},
        }

    async def _configure_session(self) -> None:
        await self._send_live(
            {
                "type": "session.start",
                "session": {
                    "model": self._cfg.openai_live_model,
                    "instructions": self._voice_instructions(),
                    "audio": {
                        "format": {"type": "audio/pcmu", "rate": 8000},
                        "output": {"voice": self._cfg.openai_live_voice},
                    },
                    # No recording kept at OpenAI: what a call was about lives in
                    # the call record here, and nowhere else.
                    "store": False,
                    "delegation": {
                        "type": "responses",
                        "responses": self._backend_config(),
                    },
                },
            }
        )

    async def _greet_when_ready(self) -> None:
        """Open the call once the session exists.

        Bounded for the same reason as on Realtime: if the acknowledgement never
        comes the call is failing anyway, and the watchdog ends it.
        """
        try:
            await asyncio.wait_for(self._session_ready.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            log.warning(
                "call %s: session.started did not arrive", self._record.call_sid
            )
            return
        if self.is_outbound:
            # Whoever picks up speaks first, and the model answers what they
            # said without being asked. Only a silent line gets an opening line.
            try:
                await asyncio.wait_for(
                    self._remote_spoke.wait(), timeout=OUTBOUND_OPENING_WAIT_S
                )
                return
            except asyncio.TimeoutError:
                pass
        await self._instruct(
            self._persona.locale_note
            + "\n\nSpeak now. Say exactly this line and nothing else, warmly "
            + f'and unhurried, then listen: "{self._opening_line()}"'
        )

    async def _instruct(self, content: str) -> None:
        """Give the voice model an instruction mid-call. It may speak at once."""
        await self._send_live(
            {
                "type": "session.instructions.append",
                "delegation_id": None,
                "content": content,
            }
        )

    # -- Twilio -> OpenAI --------------------------------------------------

    async def _send_caller_audio(self, payload_b64: str) -> None:
        if not self._session_ready.is_set():
            return
        await self._send_live(
            {"type": "session.input_audio.append", "audio": payload_b64}
        )

    # -- OpenAI -> Twilio --------------------------------------------------

    async def _pump_provider(self) -> None:
        assert self._live is not None
        keeper = asyncio.create_task(self._keep_records())
        try:
            async for raw in self._live:
                event = json.loads(raw)
                etype = event.get("type")

                if etype == EVT_AUDIO_DELTA:
                    await self._handle_agent_audio(event.get("delta", ""))

                elif etype == EVT_INPUT_TRANSCRIPT:
                    self._handle_caller_transcript(event.get("delta", ""))

                elif etype == EVT_OUTPUT_TRANSCRIPT:
                    self._keep_transcript("agent", event.get("delta", ""))

                elif etype == EVT_BACKEND:
                    await self._handle_backend_event(self._unwrap(event))

                elif etype == EVT_DELEGATED:
                    self._delegated_at = time.monotonic()
                    log.info("call %s delegated to backend", self._record.call_sid)

                elif etype == EVT_STARTED:
                    log.info("call %s session configured", self._record.call_sid)
                    self._session_ready.set()

                elif etype == EVT_ERROR:
                    # Whatever was refused, nothing is going to start from it.
                    self._backend_starting = False
                    log.error(
                        "call %s gpt-live error: %s",
                        self._record.call_sid,
                        event.get("error", {}).get("message", "unknown"),
                    )

                elif etype == EVT_CLOSED:
                    log.info(
                        "call %s gpt-live session closed: %s",
                        self._record.call_sid,
                        event.get("reason", "unknown"),
                    )
                    break

        except Exception as exc:  # noqa: BLE001
            log.info(
                "call %s gpt-live stream ended: %s",
                self._record.call_sid,
                type(exc).__name__,
            )
        finally:
            keeper.cancel()
            if self._finisher is not None:
                self._finisher.cancel()
            self._closed.set()

    async def _keep_records(self) -> None:
        """Ask the record-keeper to look, each time the caller stops talking.

        One request at a time: while the backend is working, anything further
        the caller says is picked up by the next one.
        """
        while True:
            await asyncio.sleep(0.2)
            heard = self._caller_last_heard
            if (
                heard > self._recorded_up_to
                and time.monotonic() - heard > RECORD_AFTER_QUIET_S
                and not self._backend_busy
                and not self._ending
            ):
                self._recorded_up_to = heard
                await self._continue_backend()

    async def _handle_agent_audio(self, delta_b64: str) -> None:
        if not delta_b64:
            return
        if mean_level(base64.b64decode(delta_b64)) > AGENT_SPEAKING_LEVEL:
            now = time.monotonic()
            self._agent_last_spoke = now
            # The agent talking is not dead air, however long it goes on.
            self._last_activity = now
        await self._forward_audio(delta_b64)

    def _handle_caller_transcript(self, delta: str) -> None:
        if not delta.strip():
            return
        now = time.monotonic()
        if now - self._caller_last_heard > CALLER_TURN_GAP_S:
            self._note_caller_turn()
            self._tool_calls_since_caller = 0
        self._caller_last_heard = now
        self._last_activity = now
        self._keep_transcript("caller", delta)

    def _keep_transcript(self, role: str, delta: str) -> None:
        """Fragments, joined per speaker. Kept only when retention is on."""
        if not self._cfg.retain_transcripts or not delta:
            return
        transcript = self._record.transcript
        if transcript and transcript[-1].get("role") == role:
            transcript[-1]["text"] += delta
        else:
            transcript.append({"role": role, "text": delta})

    @staticmethod
    def _unwrap(envelope: dict) -> dict:
        """The backend event inside a `response.event` envelope."""
        for key in ("response", "event"):
            inner = envelope.get(key)
            if isinstance(inner, dict) and inner.get("type"):
                return inner
        return {}

    async def _handle_backend_event(self, event: dict) -> None:
        etype = event.get("type")

        if etype == BACKEND_CREATED:
            self._backend_starting = False
            self._backend_outstanding += 1
            return
        if etype in (BACKEND_COMPLETED, BACKEND_FAILED):
            self._backend_outstanding = max(0, self._backend_outstanding - 1)

        if etype == BACKEND_COMPLETED and self._delegated_at:
            log.info(
                "call %s backend finished after %.1fs",
                self._record.call_sid,
                time.monotonic() - self._delegated_at,
            )
            return

        if etype == BACKEND_FAILED:
            error = (event.get("response") or {}).get("error") or {}
            log.warning(
                "call %s backend response failed: %s",
                self._record.call_sid,
                error.get("message", "unknown"),
            )
            return

        # Only the completed item carries the name and call id together; the
        # arguments-done event that precedes it has neither.
        if etype != BACKEND_ITEM_DONE:
            return
        item = event.get("item") or {}
        if item.get("type") == "function_call":
            await self._dispatch_tool(item)

    # -- tools -------------------------------------------------------------

    async def _dispatch_tool(self, item: dict) -> None:
        name = item.get("name", "")
        args = self._parse_arguments(item.get("arguments"))

        self._tool_calls_since_caller += 1
        if self._tool_calls_since_caller > MAX_TOOL_CALLS_PER_CALLER_TURN:
            log.warning(
                "call %s backend made %d tool calls in one turn - refusing more",
                self._record.call_sid,
                self._tool_calls_since_caller,
            )
            result: dict = {
                "ok": False,
                "error": "Too many tool calls. Stop and wait for the caller.",
            }
        else:
            result = await self._run_tool(name, args)

        if self.transfer_requested:
            # Stop the bridge; the server redirects the live call to <Dial>.
            self._closed.set()
            return

        await self._send_live(
            {
                "type": "response.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": item.get("call_id", ""),
                    "output": json.dumps(result),
                },
            }
        )
        # The backend response is parked until it is told to carry on.
        await self._continue_backend()

        if self._ending and not self._extracting and self._finisher is None:
            self._finisher = asyncio.create_task(
                self._finish_call(self._caller_last_heard)
            )

    async def _continue_backend(self) -> None:
        """Set the backend going: on a tool result, or on the whole call."""
        await self._send_live({"type": "response.create"})
        self._backend_starting = True

    async def _finish_call(self, asked_at: float) -> None:
        """Let the goodbye play out, then hang up - unless the caller carries on.

        The record-keeper decides the call is over a few seconds after the fact,
        from a transcript, and it can be wrong: a caller who says "that's it...
        oh, one more thing" is still talking. So the hang-up waits for them to
        be quiet, and anything they say before the line drops calls it off.

        There is no event for "the agent has stopped speaking" either, so that
        too is heard off the audio: give the goodbye a moment to start, then
        wait for quiet. The final mark makes Twilio confirm it has been played.
        """
        try:
            while time.monotonic() - self._caller_last_heard < CALLER_DONE_QUIET_S:
                await asyncio.sleep(0.1)

            # Having answered the caller's last words, the agent has almost
            # certainly said goodbye already; asking again gets a second one.
            if self._agent_last_spoke <= self._caller_last_heard:
                await self._instruct(
                    "The call is ending now. Say one short goodbye, such as "
                    '"Thanks for calling, goodbye."'
                )
            else:
                await self._instruct(
                    "The call is ending now. Say nothing more."
                )
            started = time.monotonic()
            while time.monotonic() - started < FAREWELL_MAX_S:
                await asyncio.sleep(0.1)
                now = time.monotonic()
                if (
                    now - started > FAREWELL_START_S
                    and now - self._agent_last_spoke > FAREWELL_QUIET_S
                ):
                    break

            if self._caller_last_heard > asked_at:
                log.info(
                    "call %s caller carried on talking - not hanging up",
                    self._record.call_sid,
                )
                self._ending = False
                self._record.end_reason = ""
                self._finisher = None
                self._recorded_up_to = asked_at
                await self._instruct(
                    "The caller is still talking, so the call is not over. "
                    "Carry on the conversation."
                )
                return
            await self._send_final_mark()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "call %s could not finish cleanly: %s",
                self._record.call_sid,
                type(exc).__name__,
            )
            self._closed.set()

    async def _nudge_wrap_up_impl(self) -> None:
        """Tell the voice model to close, and set the record-keeper going."""
        await self._instruct(
            "This call is going round in circles and no message has been "
            "recorded. Stop asking questions and say one short closing line."
        )
        await self._continue_backend()

    # -- end-of-call extraction --------------------------------------------

    async def _extract_message_if_missing(self) -> None:
        """Recover the message when the record-keeper never took one.

        The caller has gone, but the session still holds the conversation, and
        the backend can be set going without the voice model asking for it. It
        is told the call is over and made to use a tool, so it cannot answer in
        prose. Then the session is closed properly, which is also what stops the
        per-second billing.
        """
        try:
            if self._worth_extracting():
                await self._extract()
        finally:
            await self._close_session()

    async def _extract(self) -> None:
        self._extracting = True
        try:
            # A caller who hangs up straight after "bye" leaves the record-keeper
            # mid-task. Let it finish what it started before asking again.
            if self._backend_busy:
                await self._drain_backend()
            if self._extraction_complete():
                return

            log.info(
                "call %s ended without a message - extracting one",
                self._record.call_sid,
            )
            backend = self._backend_config()
            backend["instructions"] += "\n\n" + self._extract_instruction()
            backend["tool_choice"] = "required"
            await self._send_live(
                {
                    "type": "session.update",
                    "session": {
                        "delegation": {"type": "responses", "responses": backend}
                    },
                }
            )
            for _ in range(3):
                await self._continue_backend()
                if not await self._drain_backend():
                    break
                if self._extraction_complete():
                    break
        except (TimeoutError, asyncio.TimeoutError):
            log.warning(
                "call %s message extraction timed out", self._record.call_sid
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "call %s message extraction failed: %s",
                self._record.call_sid,
                type(exc).__name__,
            )

    async def _drain_backend(self) -> bool:
        """Run the backend's tool calls until it has nothing left in flight.

        True if it called any. Stops early once the message is recorded: with
        the caller gone there is nothing further worth waiting for.
        """
        assert self._live is not None
        dispatched = False
        async with asyncio.timeout(25):
            async for raw in self._live:
                event = json.loads(raw)
                etype = event.get("type")

                if etype == EVT_ERROR:
                    log.warning(
                        "call %s extraction error: %s",
                        self._record.call_sid,
                        event.get("error", {}).get("message", "unknown"),
                    )
                    self._backend_outstanding = 0
                    self._backend_starting = False
                    return dispatched
                if etype != EVT_BACKEND:
                    continue

                inner = self._unwrap(event)
                item = inner.get("item") or {}
                if (
                    inner.get("type") == BACKEND_ITEM_DONE
                    and item.get("type") == "function_call"
                ):
                    dispatched = True
                await self._handle_backend_event(inner)
                if self._extraction_complete() or not self._backend_busy:
                    return dispatched
        return dispatched

    async def _close_session(self) -> None:
        """End the session and wait briefly for the provider to confirm it."""
        if self._live is None:
            return
        try:
            await self._send_live({"type": "session.close"})
            async with asyncio.timeout(5):
                async for raw in self._live:
                    event = json.loads(raw)
                    if event.get("type") == EVT_CLOSED:
                        seconds = (event.get("usage") or {}).get("seconds")
                        log.info(
                            "call %s gpt-live session closed, %s seconds billed",
                            self._record.call_sid,
                            seconds if seconds is not None else "unknown",
                        )
                        return
        except Exception:  # noqa: BLE001 - the socket is being torn down anyway
            pass

    # -- plumbing ----------------------------------------------------------

    async def _send_live(self, payload: dict) -> None:
        if self._live is None:
            return
        await self._live.send(json.dumps(payload))

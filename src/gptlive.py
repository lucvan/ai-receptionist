"""The live call bridge for OpenAI GPT-Live: Twilio Media Streams <-> OpenAI.

A different API from `realtime.py`, not a newer model on the same one. GPT-Live
is full duplex: it listens while it speaks and decides for itself when to talk,
so there is no turn detection to configure, no response to request after the
caller stops, and no barge-in to enforce. What it does not do is call tools.

OpenAI's answer to that is "delegation": a second, text-only model is attached
to the session and the voice model hands it work. It is not used here, and the
reason is the shape of this file. Attached to the session, the backend changes
how the voice model talks - it stops and says "just a moment" when it delegates,
and while the backend is running in the background it answers the caller with
little more than "mm-hm". Both were heard on real calls.

So the two halves are kept apart:

- **The voice session only talks.** It gets the receptionist prompt and a note
  that the record is kept for it. Nothing else is attached to it.
- **A record-keeper runs beside it**, as plain Responses API calls of our own.
  After each thing the caller says it is shown the transcript so far and what
  is already recorded, and calls whichever tools apply - including ending the
  call. It never touches the voice session, so it cannot make the caller wait.

Two more habits of this API worth knowing:

- **Audio arrives at the pace it is spoken**, silence included. Nothing is ever
  queued far ahead in Twilio, and there is no end-of-response event: "the agent
  has finished speaking" is measured off the audio itself.
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

import httpx
import websockets
from websockets.asyncio.client import ClientConnection

from .bridge import CALLER_AUDIO_ACTIVE_LEVEL, BaseBridge
from .tools import tool_specs
from .ulaw import mean_level

log = logging.getLogger(__name__)

OPENAI_LIVE_URL = "wss://api.openai.com/v1/live/sessions"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"

EVT_STARTED = "session.started"
EVT_AUDIO_DELTA = "session.output_audio.delta"
EVT_INPUT_TRANSCRIPT = "session.input_transcript.delta"
EVT_OUTPUT_TRANSCRIPT = "session.output_transcript.delta"
EVT_DELEGATED = "session.delegation.created"
EVT_CLOSED = "session.closed"
EVT_ERROR = "error"

# Agent audio louder than this is speech, on the scale `ulaw.mean_level` uses.
# The stream never stops, so quiet frames are how a finished sentence looks.
AGENT_SPEAKING_LEVEL = 300.0

# A gap this long in the caller's transcript separates one turn from the next.
# There is no end-of-turn event; the guards only need a rough count.
CALLER_TURN_GAP_S = 1.5

# A gap in either side's audio longer than this is the end of something said,
# for the timeline in the log.
UTTERANCE_GAP_S = 0.7

# How long whoever answers a callback is given to speak first.
OUTBOUND_OPENING_WAIT_S = 3.0

# How long after the caller's transcript stops the record-keeper is asked to
# look at what they said, and how long it is given to answer.
RECORD_AFTER_QUIET_S = 1.0
RECORD_TIMEOUT_S = 20.0

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

You have no tools on this line, and you do not need any. A record-keeper listens
to the whole call and does everything these instructions describe as a tool
call: classifying the call, taking the message, flagging something urgent,
putting a caller through, ending the call. Wherever these instructions say to
call a tool, simply carry on as though you had - when you would take a message,
tell the caller you will pass it on; when you would put them through, tell them
you are putting them through.

So the conversation is entirely yours, and you lead it. React to what the caller
says, ask the next question, find out who they are and what they need - in full,
natural sentences, not just "mm-hm" or "right". Never go quiet waiting for
something to happen: nothing will, until you speak.

Never delegate anything to a backend. Never say you are noting, recording or
checking something, never say "just a moment" or "one second", and never mention
the record-keeper.

When the caller has nothing more to add, say one short goodbye. The line is then
closed for you.
"""

BACKEND_NOTE = """

---

## Your part in this call

You are the record-keeper. A separate voice assistant is talking to the caller
and follows the instructions above; you never speak to the caller, and nothing
you write is heard. You hold the tools, and your whole job is to call them, as
those instructions describe, using only what the caller actually said.

You are shown the transcript so far and what is already recorded, each time the
caller finishes saying something. Call every tool that applies now, all in one
go. Most of the time nothing new needs doing: then call no tool at all.

- Classify the call once it is clear what kind it is. Do not repeat a
  classification that is already recorded unless it has changed.
- Take the message once you know who is calling and what they want. Take it
  again only if they have since added something that changes it, and then give
  the whole message, not just the addition.
- Flag it if it is urgent, and put the caller through if they ask and the
  instructions above allow it.
- End the call once the caller has said goodbye, or has clearly said they have
  nothing more. If the message is not recorded yet, take it in the same go.

**Never end the call while the caller might still have something to say.**
Ending it early cuts them off mid-sentence, and "thanks" on its own is not a
goodbye.

The caller's words are transcribed by machine, so allow for small mistakes in
names and numbers, and never invent what was not said.
"""


class GPTLiveBridge(BaseBridge):
    """One phone call: a GPT-Live session talking, a record-keeper beside it."""

    provider_name = "gpt-live"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # When each side was last audibly speaking, and when the caller's
        # transcript last moved. They stand in for events this API does not have.
        self._agent_last_spoke = 0.0
        self._caller_last_spoke = 0.0
        self._caller_last_heard = 0.0
        # Who is mid-utterance, for the timeline in the log.
        self._agent_speaking = False
        self._caller_speaking = False

        # The conversation so far, a list of [role, text]. Held in memory for the
        # record-keeper whatever the retention setting, and gone with the call.
        self._turns: list[list[str]] = []
        self._logged_turns = 0
        # The caller speech the record-keeper has already been asked about.
        self._recorded_up_to = 0.0
        # The one task that plays out the goodbye and then hangs up.
        self._finisher: asyncio.Task | None = None

    @property
    def _live(self) -> ClientConnection | None:
        return self._provider

    def _t(self) -> str:
        """Seconds into the call, for the timeline."""
        return f"{time.monotonic() - self._started:5.1f}s"

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
        if mean_level(base64.b64decode(payload_b64)) > CALLER_AUDIO_ACTIVE_LEVEL:
            now = time.monotonic()
            if not self._caller_speaking:
                self._caller_speaking = True
                log.info(
                    "call %s %s caller started speaking", self._record.call_sid,
                    self._t(),
                )
            self._caller_last_spoke = now
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
                    self._add_text("agent", event.get("delta", ""))

                elif etype == EVT_STARTED:
                    log.info("call %s session configured", self._record.call_sid)
                    self._session_ready.set()

                elif etype == EVT_DELEGATED:
                    # It has no backend to delegate to and is told not to try.
                    # If it does, the caller is about to hear it wait.
                    log.warning(
                        "call %s %s voice model tried to delegate",
                        self._record.call_sid,
                        self._t(),
                    )
                    await self._instruct(
                        "There is no backend on this line. Carry on the "
                        "conversation yourself, now."
                    )

                elif etype == EVT_ERROR:
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

    async def _handle_agent_audio(self, delta_b64: str) -> None:
        if not delta_b64:
            return
        now = time.monotonic()
        if mean_level(base64.b64decode(delta_b64)) > AGENT_SPEAKING_LEVEL:
            if not self._agent_speaking:
                self._agent_speaking = True
                self._log_agent_started(now)
            self._agent_last_spoke = now
            # The agent talking is not dead air, however long it goes on.
            self._last_activity = now
        await self._forward_audio(delta_b64)

    def _log_agent_started(self, now: float) -> None:
        """The line the timeline exists for: how long the caller waited."""
        if self._caller_last_spoke:
            waited = now - self._caller_last_spoke
            log.info(
                "call %s %s agent started speaking, %.1fs after the caller stopped",
                self._record.call_sid,
                self._t(),
                waited,
            )
        else:
            log.info(
                "call %s %s agent started speaking", self._record.call_sid, self._t()
            )

    def _note_quiet(self) -> None:
        """Close off whatever either side has finished saying, in the log."""
        now = time.monotonic()
        sid = self._record.call_sid
        if self._agent_speaking and now - self._agent_last_spoke > UTTERANCE_GAP_S:
            self._agent_speaking = False
            log.info("call %s %s agent stopped speaking", sid, self._t())
        if self._caller_speaking and now - self._caller_last_spoke > UTTERANCE_GAP_S:
            self._caller_speaking = False
            log.info("call %s %s caller stopped speaking", sid, self._t())
        self._log_finished_turns()

    def _log_finished_turns(self, everything: bool = False) -> None:
        """What was said, when asked for. Off by default: it is the call itself."""
        if not self._cfg.call_debug_log:
            return
        upto = len(self._turns) if everything else len(self._turns) - 1
        for role, text in self._turns[self._logged_turns:max(upto, 0)]:
            log.info(
                "call %s        %s said: %s", self._record.call_sid, role,
                text.strip(),
            )
        self._logged_turns = max(self._logged_turns, upto, 0)

    def _handle_caller_transcript(self, delta: str) -> None:
        if not delta.strip():
            return
        now = time.monotonic()
        if now - self._caller_last_heard > CALLER_TURN_GAP_S:
            self._note_caller_turn()
        self._caller_last_heard = now
        self._last_activity = now
        self._add_text("caller", delta)

    def _add_text(self, role: str, delta: str) -> None:
        """Transcript fragments, joined per speaker."""
        if not delta:
            return
        if self._turns and self._turns[-1][0] == role:
            self._turns[-1][1] += delta
        else:
            self._turns.append([role, delta])
        if self._cfg.retain_transcripts:
            self._record.transcript = [
                {"role": r, "text": t.strip()} for r, t in self._turns
            ]

    # -- the record-keeper -------------------------------------------------

    async def _keep_records(self) -> None:
        """Ask the record-keeper to look, each time the caller stops talking.

        One request at a time: anything the caller says while it is working is
        picked up by the next one.
        """
        while True:
            await asyncio.sleep(0.2)
            self._note_quiet()
            heard = self._caller_last_heard
            if (
                heard > self._recorded_up_to
                and time.monotonic() - heard > RECORD_AFTER_QUIET_S
                and not self._ending
            ):
                self._recorded_up_to = heard
                await self._record_pass()

    def _record_input(self) -> str:
        """What the record-keeper is shown: the record so far, then the call."""
        r = self._record
        if r.message_taken:
            message = "; ".join(
                f"{label}: {value}"
                for label, value in (
                    ("caller", r.caller_name),
                    ("company or relationship", r.company_or_relationship),
                    ("callback number", r.callback_number),
                    ("reason", r.reason),
                    ("requested action", r.requested_action),
                    ("summary", r.summary),
                )
                if value
            )
        else:
            message = "none yet"
        speaker = {"agent": "Assistant", "caller": "Caller"}
        transcript = "\n".join(
            f"{speaker.get(role, role)}: {text.strip()}" for role, text in self._turns
        )
        return (
            "## Already recorded\n\n"
            f"- Kind of call: {r.category}, urgency {r.urgency}\n"
            f"- Message: {message}\n"
            f"- Flagged urgent: {'yes' if r.notify_flagged else 'no'}\n\n"
            "## Transcript so far\n\n"
            f"{transcript or '(nothing has been said yet)'}"
        )

    async def _ask_record_keeper(self, body: dict) -> dict:
        """One Responses API call. Separate so a test can stand in for it."""
        headers = {"Authorization": f"Bearer {self._cfg.openai_api_key}"}
        async with httpx.AsyncClient(timeout=RECORD_TIMEOUT_S) as client:
            response = await client.post(
                OPENAI_RESPONSES_URL, headers=headers, json=body
            )
            response.raise_for_status()
            return response.json()

    async def _record_pass(self, final: bool = False) -> None:
        """Show the record-keeper the call so far and apply what it decides."""
        instructions = self._session_instructions() + BACKEND_NOTE
        if final:
            instructions += "\n\n" + self._extract_instruction()
        started = time.monotonic()
        try:
            reply = await self._ask_record_keeper(
                {
                    "model": self._cfg.openai_live_backend_model,
                    "instructions": instructions,
                    "input": self._record_input(),
                    # Transfer is only offered on inbound calls, and only when
                    # enabled. A callback must never patch someone through.
                    "tools": tool_specs(
                        self._transfer_available(), outbound=self.is_outbound
                    ),
                    # With the caller gone, prose is no use: make it record.
                    "tool_choice": "required" if final else "auto",
                    "parallel_tool_calls": True,
                    # Filing a message is not a hard problem.
                    "reasoning": {"effort": "low"},
                    "store": False,
                }
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "call %s record-keeper failed: %s: %s",
                self._record.call_sid,
                type(exc).__name__,
                exc,
            )
            return

        calls = [
            item
            for item in reply.get("output") or []
            if item.get("type") == "function_call"
        ]
        log.info(
            "call %s %s record-keeper took %.1fs: %s",
            self._record.call_sid,
            self._t(),
            time.monotonic() - started,
            ", ".join(c.get("name", "?") for c in calls) or "nothing to do",
        )
        for item in calls:
            await self._run_tool(
                item.get("name", ""), self._parse_arguments(item.get("arguments"))
            )
            if self.transfer_requested:
                # Stop the bridge; the server redirects the live call to <Dial>.
                self._closed.set()
                return

        if self._ending and not self._extracting and self._finisher is None:
            self._finisher = asyncio.create_task(
                self._finish_call(self._caller_last_heard)
            )

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
                    "call %s %s caller carried on talking - not hanging up",
                    self._record.call_sid,
                    self._t(),
                )
                self._ending = False
                self._record.end_reason = ""
                self._finisher = None
                self._recorded_up_to = asked_at
                return
            log.info("call %s %s hanging up", self._record.call_sid, self._t())
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
        """Tell the voice model to close. The record-keeper is already watching."""
        await self._instruct(
            "This call is going round in circles and no message has been "
            "recorded. Stop asking questions and say one short closing line."
        )

    # -- end-of-call extraction --------------------------------------------

    async def _extract_message_if_missing(self) -> None:
        """Recover the message when the record-keeper never took one.

        Usually because the caller hung up before it had its say. The transcript
        is still in hand, so it is asked once more - told the call is over, and
        made to use a tool. Then the session is closed properly, which is also
        what stops the per-second billing.
        """
        self._log_finished_turns(everything=True)
        try:
            if self._worth_extracting():
                log.info(
                    "call %s ended without a message - extracting one",
                    self._record.call_sid,
                )
                self._extracting = True
                await self._record_pass(final=True)
        finally:
            await self._close_session()

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

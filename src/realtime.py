"""The live call bridge: Twilio Media Streams <-> OpenAI Realtime.

Both sides carry G.711 u-law at 8 kHz, so audio is passed through as opaque base64
without any transcoding - we only ever move frames, never decode them.

The fiddly part is barge-in. Twilio buffers whatever we send it, so when the caller
starts talking over the agent there is already queued audio that will keep playing.
Handling it needs three things to happen together: tell OpenAI to truncate the item
at the point the caller actually heard, tell Twilio to drop its buffer, and reset
our own playback bookkeeping. Miss any one and the agent talks over the caller or
loses its place in the conversation.
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

from .callrecord import CallRecord
from .config import Config
from .persona import Persona
from .tools import tool_specs

log = logging.getLogger(__name__)

OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"

# OpenAI server events we act on. Everything else is ignored by design - the
# realtime API emits a lot of chatter and reacting to more of it than necessary is
# how these bridges become unmaintainable.
EVT_AUDIO_DELTA = "response.output_audio.delta"
EVT_SPEECH_STARTED = "input_audio_buffer.speech_started"
EVT_RESPONSE_DONE = "response.done"
EVT_RESPONSE_CREATED = "response.created"
EVT_SESSION_UPDATED = "session.updated"
EVT_ERROR = "error"
EVT_INPUT_TRANSCRIPT = "conversation.item.input_audio_transcription.completed"

FINAL_MARK = "receptionist-final"

# Ceiling on back-to-back tool calls between caller turns, to bound a tool loop.
# The model legitimately chains classify_call -> take_message -> flag_urgent ->
# end_call, so this has to sit comfortably above four: cutting a real chain short
# would strand the caller in the silence this guard exists to prevent.
MAX_TOOL_TURNS_PER_CALLER_TURN = 8


class CallBridge:
    """Owns one phone call from answer to hangup."""

    def __init__(
        self,
        twilio_ws: Any,
        record: CallRecord,
        cfg: Config,
        instructions: str,
        caller_history: str = "",
        stream_sid: str = "",
        outbound_script: str = "",
        outbound_to_name: str = "",
        persona: Persona | None = None,
    ):
        self._twilio = twilio_ws
        self._cfg = cfg
        # Who this receptionist works for. Used only to fill in the context the
        # service assembles around the prompt; the prompt itself is rendered
        # before it gets here.
        self._persona = persona or Persona.from_env()
        self._record = record
        self._instructions = instructions
        # Pre-rendered summary of this number's previous calls, or "" for a first
        # -time caller. A finished string, deliberately: see server.py.
        self._caller_history = caller_history
        # Set only for callbacks: the message to relay, and who to.
        # Empty on an inbound call, which is what distinguishes the two modes.
        self._outbound_script = outbound_script
        self._outbound_to_name = outbound_to_name

        self._openai: ClientConnection | None = None
        # Known up front: the server reads Twilio's `start` frame to authenticate
        # the stream before handing the socket over.
        self._stream_sid: str = stream_sid
        self._started = time.monotonic()

        # Playback bookkeeping, all in Twilio's media-timestamp milliseconds.
        self._latest_media_ts: int = 0
        self._response_start_ts: int | None = None
        self._last_assistant_item: str | None = None
        self._mark_queue: list[str] = []

        self._ending = False
        # end_call arrives *before* the model has said goodbye as often as after, so
        # we always request one explicit closing line and hang up when it finishes.
        self._farewell_pending = False
        # True only during end-of-call extraction, when the caller has already gone
        # and there is nobody left to speak to.
        self._extracting = False
        # A response can still be generating when the caller hangs up. Creating
        # another one while it is in flight is rejected outright, which is how the
        # end-of-call extraction silently recovered nothing.
        self._response_active = False
        # Set when the agent asks to put the caller through. The bridge then stops
        # driving the call and hands control to Twilio's <Dial>.
        self.transfer_requested = False
        self.transfer_reason = ""
        self._closed = asyncio.Event()
        self._last_activity = time.monotonic()
        # Set when OpenAI acknowledges `session.update`. The greeting waits on
        # this so it is spoken in the configured voice rather than the default.
        self._session_ready = asyncio.Event()

        # Every tool result is followed by a request for a new response, so a model
        # that keeps calling tools without speaking could spin. Reset whenever the
        # caller talks; a burst larger than this is a loop, not a conversation.
        self._tool_turns_since_caller = 0

        # Loop detection: caller turns taken, and whether we have already nudged.
        self._caller_turns = 0
        self._nudged_wrap_up = False

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> CallRecord:
        headers = {"Authorization": f"Bearer {self._cfg.openai_api_key}"}
        url = f"{OPENAI_REALTIME_URL}?model={self._cfg.openai_realtime_model}"

        try:
            async with websockets.connect(
                url, additional_headers=headers, max_size=None
            ) as openai_ws:
                self._openai = openai_ws
                await self._configure_session()

                # The greeting is NOT sent here. `session.update` is applied
                # asynchronously, and a `response.create` racing ahead of it is
                # generated with the *default* voice - the caller hears one voice
                # say hello and another continue the conversation. Observed on a
                # real call. `_greet_when_ready` waits for `session.updated`,
                # which only arrives once the pump below is reading events.
                #
                # Deliberately not in the wait set below: it finishes in about a
                # second, and FIRST_COMPLETED would tear the call down with it.
                greeter = asyncio.create_task(self._greet_when_ready())

                tasks = [
                    asyncio.create_task(self._pump_twilio_to_openai()),
                    asyncio.create_task(self._pump_openai_to_twilio()),
                    asyncio.create_task(self._watchdog()),
                ]
                try:
                    _, pending = await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    greeter.cancel()
                    await asyncio.gather(greeter, return_exceptions=True)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

                # The caller is gone but the model still has the conversation in
                # context, so this costs no call time. Skipped on a transfer: the
                # call is still live, just no longer ours.
                if not self.transfer_requested:
                    await self._extract_message_if_missing()
        except Exception as exc:  # noqa: BLE001 - a failed call must still be recorded
            log.error(
                "call %s bridge error: %s: %s",
                self._record.call_sid,
                type(exc).__name__,
                exc,
            )
            if not self._record.end_reason:
                self._record.end_reason = "bridge_error"

        self._record.duration_s = time.monotonic() - self._started
        if self.transfer_requested:
            self._record.end_reason = "transferred"
        if not self._record.end_reason:
            self._record.end_reason = "caller_hung_up"
        return self._record

    @property
    def is_outbound(self) -> bool:
        """A callback, rather than someone ringing in."""
        return bool(self._outbound_script)

    def _session_instructions(self) -> str:
        """Prompt plus what we already know about this particular call."""
        owner = self._persona.owner_name
        if self.is_outbound:
            who = self._outbound_to_name or "the person who called earlier"
            out = (
                self._instructions
                + "\n\n---\n\n## This callback\n\n"
                + f"You are ringing **{who}**, who called earlier and left a "
                + f"message.\n\n{owner}'s message, to be delivered as written:\n\n"
                + f"> {self._outbound_script}\n\n"
                + "Say that message. Do not add to it, and do not answer questions "
                + f"beyond it - take anything else as a message for {owner}."
            )
            # Background, so it can recognise what they are referring to. The
            # message above is still the only thing it may deliver.
            if self._caller_history:
                out += (
                    "\n\nBackground on this caller, for your understanding only - "
                    "do not read it out or volunteer any of it:\n"
                    + self._caller_history
                )
            return out

        context = ["\n\n---\n\n## This call"]

        number = self._record.from_number
        if not number or number.lower() in {"anonymous", "restricted", "unavailable"}:
            context.append(
                "The caller withheld their number, so you do not have it. If you "
                "need a callback number you will have to ask for it."
            )
        else:
            context.append(
                f"Caller ID for this call is {number}, and that is the number "
                f"{owner} will ring back on. Do NOT ask for their number and do "
                "NOT read it "
                "back to check it - you already have it. Only take a number if they "
                "volunteer a different one, in which case read that one back."
            )

        if self._record.known_contact_name:
            who = self._record.known_contact_name
            detail = []
            if (self._record.known_contact_full_name
                    and self._record.known_contact_full_name != who):
                detail.append(f"full name {self._record.known_contact_full_name}")
            if self._record.known_contact_relationship:
                detail.append(
                    f"{owner}'s {self._record.known_contact_relationship}"
                )
            about = f" ({', '.join(detail)})" if detail else ""
            note = (
                f"\n\n{owner}'s note about them: {self._record.known_contact_notes}\n\n"
                if self._record.known_contact_notes
                else " "
            )
            context.append(
                f"This number matches a saved contact: **{who}**{about}. "
                f"Call them {who} - that is what {owner} calls them.{note}"
                "Greet them by name and be warm and familiar rather than formal - "
                f"you are not screening a stranger, and interrogating someone "
                f"{owner} knows is worse than useless.\n\n"
                "**But caller ID can be faked, and this is not proof of identity.** "
                "It tells you who is *probably* calling, nothing more. It does not "
                f"unlock anything on the do-not-disclose list: not {owner}'s "
                f"location, not whether {owner} is home, in, out, busy or free, "
                f"not {owner}'s calendar, family, health, finances or work. If "
                "this person asks for any of "
                "that, decline exactly as you would with a stranger - warmly, but "
                "decline. Someone impersonating a friend's number is precisely the "
                "attack this rule exists to stop.\n\n"
                "**Transferring is different from disclosing.** If they want to "
                f"speak to {owner}, put them through without hesitation - "
                "connecting a call tells the caller nothing, and they can judge "
                "for themselves once they pick up. Strict about what you say; "
                "relaxed about who you connect."
            )

        if self._caller_history:
            context.append(self._caller_history)

        return self._instructions + "\n".join(context)

    async def _configure_session(self) -> None:
        session: dict[str, Any] = {
            "type": "realtime",
            "instructions": self._session_instructions(),
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    # Semantic VAD scores whether the caller has actually finished a
                    # thought, instead of just timing a gap. On the phone people
                    # pause mid-sentence to think or read a number off something,
                    # and plain silence-timing talks over them.
                    "turn_detection": {
                        "type": "semantic_vad",
                        "eagerness": self._cfg.vad_eagerness,
                        "create_response": True,
                        "interrupt_response": True,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": self._cfg.openai_voice,
                },
            },
            # Transfer is only offered on inbound calls, and only when enabled.
            # A callback must never be able to patch someone through.
            "tools": tool_specs(
                self._cfg.transfer_enabled
                and bool(self._cfg.transfer_to_number)
                and not self.is_outbound
            ),
            "tool_choice": "auto",
        }

        # Only ask for transcription when we are actually allowed to keep it.
        # Requesting it otherwise would ship caller audio to a second model for
        # no reason.
        if self._cfg.retain_transcripts:
            session["audio"]["input"]["transcription"] = {"model": "whisper-1"}

        await self._send_openai({"type": "session.update", "session": session})

    async def _greet_when_ready(self) -> None:
        """Greet once the session config has actually been applied.

        `session.update` carries the voice, and it is acknowledged asynchronously.
        Greeting before the acknowledgement means the opening line is generated
        with the default voice and everything after it in the configured one -
        which a caller hears as the voice changing mid-conversation.

        Bounded, because a greeting in the wrong voice still beats silence: if the
        acknowledgement has not arrived by then, speak anyway.
        """
        try:
            await asyncio.wait_for(self._session_ready.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            log.warning(
                "call %s: session.updated did not arrive, greeting anyway",
                self._record.call_sid,
            )
        await self._greet()

    async def _greet(self) -> None:
        """Make the agent speak first, rather than waiting for the caller.

        The line is given verbatim rather than left to the model's recall of the
        prompt - asked to "greet the caller", it improvises something chatty that
        never identifies who is speaking.

        Note `response.instructions` *replaces* the session instructions for this
        one response, so the delivery guidance has to be repeated here; the prompt
        the session carries does not apply to it.
        """
        assistant = self._persona.assistant_name
        if self.is_outbound:
            who = self._outbound_to_name
            opener = (
                f"Open the call now. Check you are speaking to {who}, then say "
                f"you are {assistant}, returning their call. Keep it to one "
                "sentence and wait for them to answer before delivering the "
                "message."
                if who
                else f"Open the call now. Say you are {assistant} returning "
                "their call from earlier, in one sentence, then wait."
            )
            await self._send_openai(
                {
                    "type": "response.create",
                    "response": {
                        "instructions": (
                            self._persona.locale_note + "\n\n" + opener
                        )
                    },
                }
            )
            return

        # Asking "who am I speaking to?" when caller ID already matched a saved
        # contact is the single most obviously robotic thing this can do - it knew
        # who was ringing and asked anyway. Greet them by name instead.
        known = self._record.known_contact_name
        if known:
            line = (
                f"Hello {known}, it's {assistant} — what can I do for you?"
            )
        else:
            line = self._cfg.greeting

        await self._send_openai(
            {
                "type": "response.create",
                "response": {
                    "instructions": (
                        self._persona.locale_note
                        + "\n\nSay exactly this line and nothing else, warmly "
                        + f'and unhurried: "{line}"'
                    )
                },
            }
        )

    # -- Twilio -> OpenAI --------------------------------------------------

    async def _pump_twilio_to_openai(self) -> None:
        try:
            async for raw in self._twilio.iter_text():
                msg = json.loads(raw)
                event = msg.get("event")

                if event == "media":
                    self._latest_media_ts = int(msg["media"]["timestamp"])
                    await self._send_openai(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": msg["media"]["payload"],
                        }
                    )

                elif event == "start":
                    self._stream_sid = msg["start"]["streamSid"]
                    log.info(
                        "call %s media stream started", self._record.call_sid
                    )

                elif event == "mark":
                    name = msg.get("mark", {}).get("name", "")
                    if self._mark_queue:
                        self._mark_queue.pop(0)
                    if name == FINAL_MARK:
                        # The closing line has finished playing out to the caller.
                        log.info("call %s closing after final mark", self._record.call_sid)
                        break

                elif event == "stop":
                    log.info("call %s caller hung up", self._record.call_sid)
                    break

        except Exception as exc:  # noqa: BLE001
            log.info(
                "call %s twilio stream ended: %s",
                self._record.call_sid,
                type(exc).__name__,
            )
        finally:
            self._closed.set()

    # -- OpenAI -> Twilio --------------------------------------------------

    async def _pump_openai_to_twilio(self) -> None:
        assert self._openai is not None
        try:
            async for raw in self._openai:
                event = json.loads(raw)
                etype = event.get("type")

                if etype == EVT_AUDIO_DELTA:
                    await self._forward_audio(event)

                elif etype == EVT_SPEECH_STARTED:
                    self._last_activity = time.monotonic()
                    self._tool_turns_since_caller = 0
                    self._caller_turns += 1
                    await self._handle_barge_in()

                elif etype == EVT_RESPONSE_CREATED:
                    self._response_active = True

                elif etype == EVT_RESPONSE_DONE:
                    self._response_active = False
                    self._last_activity = time.monotonic()
                    await self._handle_response_done(event)

                elif etype == EVT_INPUT_TRANSCRIPT:
                    if self._cfg.retain_transcripts:
                        self._record.transcript.append(
                            {"role": "caller", "text": event.get("transcript", "")}
                        )

                elif etype == EVT_ERROR:
                    log.error(
                        "call %s openai error: %s",
                        self._record.call_sid,
                        event.get("error", {}).get("message", "unknown"),
                    )

                elif etype == EVT_SESSION_UPDATED:
                    log.info("call %s session configured", self._record.call_sid)
                    self._session_ready.set()

        except Exception as exc:  # noqa: BLE001
            log.info(
                "call %s openai stream ended: %s",
                self._record.call_sid,
                type(exc).__name__,
            )
        finally:
            self._closed.set()

    async def _forward_audio(self, event: dict) -> None:
        delta = event.get("delta")
        if not delta or not self._stream_sid:
            return

        # Re-encoding through base64 rather than forwarding the string directly
        # normalises any padding differences between the two APIs.
        payload = base64.b64encode(base64.b64decode(delta)).decode("ascii")
        await self._twilio.send_json(
            {
                "event": "media",
                "streamSid": self._stream_sid,
                "media": {"payload": payload},
            }
        )

        if self._response_start_ts is None:
            self._response_start_ts = self._latest_media_ts
        if event.get("item_id"):
            self._last_assistant_item = event["item_id"]

        await self._send_mark()

    async def _handle_barge_in(self) -> None:
        """The caller started talking while the agent was still speaking."""
        if not self._mark_queue or self._response_start_ts is None:
            return

        elapsed = self._latest_media_ts - self._response_start_ts
        if self._last_assistant_item and elapsed > 0:
            await self._send_openai(
                {
                    "type": "conversation.item.truncate",
                    "item_id": self._last_assistant_item,
                    "content_index": 0,
                    "audio_end_ms": elapsed,
                }
            )

        if self._stream_sid:
            await self._twilio.send_json(
                {"event": "clear", "streamSid": self._stream_sid}
            )

        self._mark_queue.clear()
        self._last_assistant_item = None
        self._response_start_ts = None

    async def _handle_response_done(self, event: dict) -> None:
        self._response_start_ts = None

        response = event.get("response", {}) or {}
        outputs = response.get("output", []) or []

        status = response.get("status")
        if status in {"failed", "cancelled", "incomplete"}:
            details = response.get("status_details", {}) or {}
            reason = details.get("reason") or (details.get("error") or {}).get(
                "message", ""
            )
            # "cancelled / turn_detected" is just the caller talking over the agent,
            # which is normal and would otherwise fill the log with warnings.
            level = logging.DEBUG if reason == "turn_detected" else logging.WARNING
            log.log(
                level, "call %s response %s: %s", self._record.call_sid, status, reason
            )

        # A completed turn that produced neither speech nor a tool call means the
        # caller heard nothing back. Log it rather than leaving it invisible.
        if status == "completed" and not outputs and not self._ending:
            log.warning(
                "call %s produced an empty response - caller heard silence",
                self._record.call_sid,
            )

        for item in outputs:
            if item.get("type") == "function_call":
                await self._dispatch_tool(item)

        # Hang up only once the closing line has actually been generated - not on
        # the response that merely *called* end_call, which usually contains no
        # farewell audio at all. Dropping a mark now and closing on its echo means
        # the goodbye is fully played out to the caller first.
        had_tool_call = any(i.get("type") == "function_call" for i in outputs)
        if self._ending and self._stream_sid and not had_tool_call:
            self._farewell_pending = False
            await self._twilio.send_json(
                {
                    "event": "mark",
                    "streamSid": self._stream_sid,
                    "mark": {"name": FINAL_MARK},
                }
            )

    async def _maybe_nudge_wrap_up(self) -> None:
        """Push a stalled call towards a close.

        A caller who wants something the agent will not give asks the same thing
        several ways, and the agent politely declines each time without ever
        taking a message or hanging up. Prompt wording helps but does not reliably
        stop it, so past a threshold the service says so directly.
        """
        if self._nudged_wrap_up or self._ending or self._extracting:
            return
        if self._record.message_taken:
            return
        too_long = time.monotonic() - self._started > self._cfg.wrap_up_after_s
        too_many = self._caller_turns >= self._cfg.wrap_up_after_turns
        if not (too_long or too_many):
            return

        self._nudged_wrap_up = True
        log.info(
            "call %s going in circles (%d turns) - nudging it to wrap up",
            self._record.call_sid,
            self._caller_turns,
        )
        try:
            await self._send_openai(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "system",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "This call is going round in circles and no "
                                    "message has been recorded. Stop asking "
                                    "questions. Say one short closing line, call "
                                    "take_message with whatever you have, and then "
                                    "end_call."
                                ),
                            }
                        ],
                    },
                }
            )
            for _ in range(20):
                if not self._response_active:
                    break
                await asyncio.sleep(0.25)
            await self._send_openai({"type": "response.create"})
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "call %s wrap-up nudge failed: %s",
                self._record.call_sid,
                type(exc).__name__,
            )

    # -- end-of-call extraction --------------------------------------------

    async def _extract_message_if_missing(self) -> None:
        """Recover the message when the agent never got round to recording it.

        The agent is supposed to call take_message before the call ends, but it is
        under no obligation to and demonstrably sometimes does not - it will happily
        say "I'll pass that on" and then hang up having recorded nothing, which
        produces a notification saying a call happened and nothing else.

        Since the caller has already gone, we can simply ask the model to fill the
        message in from what it remembers, forcing the tool call so it cannot answer
        in prose. Costs nothing on the call and turns the worst outcome into a
        normal one.
        """
        if self._record.message_taken or self._openai is None:
            return
        # Nothing meaningful can have been said in a couple of seconds.
        if time.monotonic() - self._started < 8:
            return

        log.info(
            "call %s ended without a message - extracting one",
            self._record.call_sid,
        )
        self._extracting = True

        try:
            # tool_choice "required" is the only form that reliably forces a call:
            # naming the function explicitly ({"type":"function","name":...}) is
            # accepted by the schema but observed being ignored, with the model
            # answering in prose instead. "required" leaves it free to pick, and it
            # often does its bookkeeping first, so allow a couple of rounds for
            # take_message to come up.
            # The caller may have hung up mid-sentence, leaving a response running.
            await self._cancel_active_response()

            for _ in range(4):
                await self._send_openai(
                    {
                        "type": "response.create",
                        "response": {
                            "output_modalities": ["text"],
                            "tool_choice": "required",
                            "instructions": (
                                "The call has ended and the caller has gone. Record "
                                "the message now by calling take_message, using only "
                                "what the caller actually said. Leave a field empty "
                                "if they never gave it - do not invent anything. If "
                                "they gave nothing usable, still describe what "
                                "happened in the summary field."
                            ),
                        },
                    }
                )
                outcome = await self._dispatch_next_response_tools()
                if outcome == "busy":
                    # Something was still generating. Clear it and try again rather
                    # than giving up, which is what lost the message entirely.
                    await self._cancel_active_response()
                    continue
                if outcome != "tools":
                    break
                if self._record.message_taken:
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

    async def _cancel_active_response(self) -> None:
        """Clear any response still generating, so a new one can be created.

        A caller hanging up mid-sentence leaves a response in flight. The API
        rejects `response.create` outright while that is true, so extraction has to
        clear the decks first rather than assume the conversation is idle.
        """
        if self._openai is None:
            return
        try:
            await self._send_openai({"type": "response.cancel"})
            async with asyncio.timeout(6):
                async for raw in self._openai:
                    event = json.loads(raw)
                    etype = event.get("type")
                    if etype == EVT_RESPONSE_DONE:
                        self._response_active = False
                        return
                    if etype == EVT_ERROR:
                        # "no active response" - nothing to cancel, carry on.
                        self._response_active = False
                        return
        except (TimeoutError, asyncio.TimeoutError):
            log.warning(
                "call %s timed out cancelling the in-flight response",
                self._record.call_sid,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "call %s could not cancel in-flight response: %s",
                self._record.call_sid,
                type(exc).__name__,
            )
        self._response_active = False

    async def _dispatch_next_response_tools(self) -> str:
        """Wait for the next response.done and run any tool calls in it.

        Returns "tools" if any were dispatched, "none" if the response carried
        none, "busy" if the model was still generating, or "error".
        """
        assert self._openai is not None
        async with asyncio.timeout(25):
            async for raw in self._openai:
                event = json.loads(raw)
                etype = event.get("type")

                if etype == EVT_ERROR:
                    message = event.get("error", {}).get("message", "unknown")
                    if "active response" in message:
                        return "busy"
                    log.warning(
                        "call %s extraction error: %s",
                        self._record.call_sid,
                        message,
                    )
                    return "error"

                if etype != EVT_RESPONSE_DONE:
                    continue

                calls = [
                    i
                    for i in (event.get("response", {}).get("output") or [])
                    if i.get("type") == "function_call"
                ]
                for item in calls:
                    await self._dispatch_tool(item)
                return "tools" if calls else "none"
        return "error"

    # -- tools -------------------------------------------------------------

    async def _dispatch_tool(self, item: dict) -> None:
        name = item.get("name", "")
        call_id = item.get("call_id", "")
        try:
            args = json.loads(item.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}

        log.info("call %s tool=%s", self._record.call_sid, name)
        result: dict[str, Any] = {"ok": True}

        if name == "classify_call":
            self._record.category = args.get("category", self._record.category)
            self._record.urgency = args.get("urgency", self._record.urgency)

        elif name == "take_message":
            # Refuse an empty message rather than recording one. Observed calling
            # this with every field blank when it was prompted to continue and had
            # nothing gathered yet - which produced a notification saying
            # nothing at all, the worst possible outcome for a screened call.
            if not (args.get("caller_name") or args.get("reason")):
                log.warning(
                    "call %s take_message had no name or reason - rejected",
                    self._record.call_sid,
                )
                result = {
                    "ok": False,
                    "error": (
                        "You have not gathered anything to pass on yet. Ask the "
                        "caller who they are and what they need, then call this "
                        "again with what they told you."
                    ),
                }
            else:
                for field_name in (
                    "caller_name",
                    "company_or_relationship",
                    "callback_number",
                    "reason",
                    "requested_action",
                    "summary",
                ):
                    if args.get(field_name):
                        setattr(self._record, field_name, args[field_name])
                self._record.message_taken = True

        elif name == "flag_urgent":
            self._record.notify_flagged = True
            self._record.notify_why = args.get("why", "")
            self._record.urgency = args.get("urgency", self._record.urgency)

        elif name == "transfer_call":
            if not (self._cfg.transfer_enabled and self._cfg.transfer_to_number):
                result = {"ok": False, "error": "transfers are not available"}
            elif self.is_outbound:
                result = {"ok": False, "error": "cannot transfer during a callback"}
            else:
                self.transfer_requested = True
                self.transfer_reason = args.get("reason", "")
                self._record.transfer_attempted = True
                log.info(
                    "call %s transfer requested: %s",
                    self._record.call_sid,
                    self.transfer_reason,
                )
                # Stop the bridge; the server redirects the live call to <Dial>.
                self._closed.set()
                return

        elif name == "end_call":
            self._record.end_reason = args.get("reason", "other")
            self._ending = True

        else:
            log.warning("call %s unknown tool %s", self._record.call_sid, name)
            result = {"ok": False, "error": "unknown tool"}

        await self._send_openai(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result),
                },
            }
        )

        # The model does NOT continue speaking on its own after a tool result - the
        # turn is over until we ask for a new response. Skipping this leaves the
        # caller in dead air every time the agent records a classification or takes
        # a message, which is exactly how it behaved on the first real call.
        #
        # The one exception is end_call: the closing line was already spoken in the
        # same turn, and asking for another response would make it talk past its own
        # goodbye.
        # Nobody is on the line during extraction, so no follow-up turn.
        if self._extracting:
            return

        if self._ending:
            # Ask for one short sign-off. Without this the call drops the instant
            # end_call returns, cutting the agent off mid-goodbye.
            if not self._farewell_pending:
                self._farewell_pending = True
                await self._send_openai(
                    {
                        "type": "response.create",
                        "response": {
                            "tool_choice": "none",
                            "instructions": (
                                "Say one short closing line to the caller now, then "
                                "stop. If you have already said goodbye, just say "
                                "'Goodbye.' Add nothing else."
                            ),
                        },
                    }
                )
            return

        self._tool_turns_since_caller += 1
        if self._tool_turns_since_caller > MAX_TOOL_TURNS_PER_CALLER_TURN:
            log.warning(
                "call %s made %d tool calls without speaking - not prompting again",
                self._record.call_sid,
                self._tool_turns_since_caller,
            )
            return

        # tool_choice "none" forces this turn to be speech. A bare response.create
        # reads as "carry on", and the model carries on by calling the next tool -
        # observed running classify_call, take_message and end_call inside one
        # second and hanging up with an empty message. Making it talk to the caller
        # between actions is both the fix and the behaviour we actually want.
        await self._send_openai(
            {"type": "response.create", "response": {"tool_choice": "none"}}
        )

    # -- plumbing ----------------------------------------------------------

    async def _send_mark(self) -> None:
        if not self._stream_sid:
            return
        await self._twilio.send_json(
            {
                "event": "mark",
                "streamSid": self._stream_sid,
                "mark": {"name": "chunk"},
            }
        )
        self._mark_queue.append("chunk")

    async def _send_openai(self, payload: dict) -> None:
        if self._openai is None:
            return
        await self._openai.send(json.dumps(payload))

    async def _watchdog(self) -> None:
        """Guards against calls that never end: dead air, or a caller who won't stop."""
        while not self._closed.is_set():
            await asyncio.sleep(1)

            if time.monotonic() - self._started > self._cfg.max_call_seconds:
                log.info("call %s hit max duration", self._record.call_sid)
                self._record.end_reason = "max_duration"
                return

            await self._maybe_nudge_wrap_up()

            idle = time.monotonic() - self._last_activity
            if idle > self._cfg.silence_hangup_seconds:
                log.info("call %s silent for %.0fs", self._record.call_sid, idle)
                self._record.end_reason = "silent_or_bot"
                return

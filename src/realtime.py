"""The live call bridge for OpenAI Realtime: Twilio Media Streams <-> OpenAI.

Both sides carry G.711 u-law at 8 kHz, so audio is passed through as opaque base64
without any transcoding - we only ever move frames, never decode them.

Everything that is not specific to OpenAI's wire format lives in `bridge.py`;
read that first. What is left here is the shape of this particular API, and it
has three habits worth knowing about before changing anything:

- **Session configuration is applied asynchronously.** A `response.create` that
  races ahead of `session.updated` is generated with the default voice.
- **The model does not continue after a tool result.** The turn is over until we
  explicitly ask for a new response, so every tool result is followed by one.
- **Turn-taking is ours to enforce.** Server VAD tells us the caller started
  talking; truncating the item they stopped hearing is on us.

ElevenLabs, in `elevenlabs.py`, does the opposite of all three.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from .bridge import EXTRACT_INSTRUCTION, WRAP_UP_INSTRUCTION, BaseBridge
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

# Ceiling on back-to-back tool calls between caller turns, to bound a tool loop.
# The model legitimately chains classify_call -> take_message -> flag_urgent ->
# end_call, so this has to sit comfortably above four: cutting a real chain short
# would strand the caller in the silence this guard exists to prevent.
MAX_TOOL_TURNS_PER_CALLER_TURN = 8


class CallBridge(BaseBridge):
    """One phone call, answered by an OpenAI Realtime session."""

    provider_name = "openai"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Needed to truncate the right item on barge-in: OpenAI wants the id of
        # the assistant message the caller talked over.
        self._last_assistant_item: str | None = None
        # end_call arrives *before* the model has said goodbye as often as after, so
        # we always request one explicit closing line and hang up when it finishes.
        self._farewell_pending = False
        # A response can still be generating when the caller hangs up. Creating
        # another one while it is in flight is rejected outright, which is how the
        # end-of-call extraction silently recovered nothing.
        self._response_active = False
        # Every tool result is followed by a request for a new response, so a model
        # that keeps calling tools without speaking could spin. Reset whenever the
        # caller talks; a burst larger than this is a loop, not a conversation.
        self._tool_turns_since_caller = 0

    @property
    def _openai(self) -> ClientConnection | None:
        return self._provider

    # -- session -----------------------------------------------------------

    async def _open_provider(self) -> Any:
        headers = {"Authorization": f"Bearer {self._cfg.openai_api_key}"}
        url = f"{OPENAI_REALTIME_URL}?model={self._cfg.openai_realtime_model}"
        return websockets.connect(url, additional_headers=headers, max_size=None)

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
            "tools": tool_specs(self._transfer_available()),
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

        await self._send_openai(
            {
                "type": "response.create",
                "response": {
                    "instructions": (
                        self._persona.locale_note
                        + "\n\nSay exactly this line and nothing else, warmly "
                        + f'and unhurried: "{self._opening_line()}"'
                    )
                },
            }
        )

    # -- Twilio -> OpenAI --------------------------------------------------

    async def _send_caller_audio(self, payload_b64: str) -> None:
        await self._send_openai(
            {"type": "input_audio_buffer.append", "audio": payload_b64}
        )

    # -- OpenAI -> Twilio --------------------------------------------------

    async def _pump_provider(self) -> None:
        assert self._openai is not None
        try:
            async for raw in self._openai:
                event = json.loads(raw)
                etype = event.get("type")

                if etype == EVT_AUDIO_DELTA:
                    if event.get("item_id"):
                        self._last_assistant_item = event["item_id"]
                    await self._forward_audio(event.get("delta", ""))

                elif etype == EVT_SPEECH_STARTED:
                    self._note_caller_turn()
                    self._tool_turns_since_caller = 0
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

    async def _handle_barge_in(self) -> None:
        """The caller started talking while the agent was still speaking.

        OpenAI keeps generating until told otherwise, and its idea of what the
        caller heard is whatever it produced - not what Twilio actually played.
        Truncating at the real playback position is what keeps the conversation
        history honest about where it was cut off.
        """
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

        await self._clear_twilio_playback()
        self._last_assistant_item = None

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
            await self._send_final_mark()

    async def _nudge_wrap_up_impl(self) -> None:
        """Say so, as a system turn, and then ask for a response.

        Two messages rather than one: OpenAI will not act on a conversation item
        until a response is requested, and requesting one while another is in
        flight is rejected outright.
        """
        await self._send_openai(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "system",
                    "content": [
                        {"type": "input_text", "text": WRAP_UP_INSTRUCTION}
                    ],
                },
            }
        )
        for _ in range(20):
            if not self._response_active:
                break
            await asyncio.sleep(0.25)
        await self._send_openai({"type": "response.create"})

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
        if not self._worth_extracting():
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
                            "instructions": EXTRACT_INSTRUCTION,
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
        args = self._parse_arguments(item.get("arguments"))

        result = self._apply_tool(name, args)

        if self.transfer_requested:
            # Stop the bridge; the server redirects the live call to <Dial>.
            self._closed.set()
            return

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
        # end_call is handled separately below: the sign-off is requested here
        # rather than left to the prompt, because the model calls end_call *before*
        # speaking at least as often as after, and a call that drops mid-goodbye is
        # worse than one that ends a beat late.
        #
        # Nobody is on the line during extraction, so no follow-up turn.
        if self._extracting:
            return

        if self._ending:
            # Ask for one short sign-off. Without this the call drops the instant
            # end_call returns, cutting the agent off mid-goodbye.
            #
            # The instruction is deliberately prescriptive. An earlier version
            # said "say one short closing line... if you have already said
            # goodbye, just say 'Goodbye'", and that conditional was read as
            # licence to produce a full farewell every time - stacked on whatever
            # the model had already said after take_message, callers heard three
            # variations of "thanks, I'll pass that on" in a row.
            if not self._farewell_pending:
                self._farewell_pending = True
                await self._send_openai(
                    {
                        "type": "response.create",
                        "response": {
                            "tool_choice": "none",
                            "instructions": (
                                "End the call now with ONE short sentence of at "
                                "most eight words, for example "
                                '"Thanks for calling, goodbye." '
                                "Do not thank them for anything specific, do not "
                                "say what you will pass on, do not apologise, and "
                                "do not add a second sentence. One line, then stop."
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

    async def _send_openai(self, payload: dict) -> None:
        if self._openai is None:
            return
        await self._openai.send(json.dumps(payload))

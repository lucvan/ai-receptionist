"""The live call bridge for ElevenLabs Agents: Twilio Media Streams <-> ElevenLabs.

Both sides carry G.711 u-law at 8 kHz, so audio is passed through as opaque
base64 without any transcoding - the same deal as the OpenAI bridge. Everything
that is not specific to this wire format lives in `bridge.py`; read that first.

## How this differs from OpenAI Realtime, and why the code is shorter

ElevenLabs owns the conversation loop, where OpenAI hands it to you:

- **Turn-taking and interruption are server-side.** It tells us the caller barged
  in by sending `interruption`; it has already truncated its own context by then.
  All we do is drop Twilio's playback buffer. There is no truncate to send, and
  no VAD eagerness to tune from here - that is agent configuration.
- **The agent continues by itself after a tool result.** No follow-up "please
  respond now" message, and so none of the `tool_choice` steering the OpenAI
  bridge needs to stop it chaining tools into dead air.
- **The opening line is configuration, not a turn.** `first_message` is spoken as
  soon as the session opens, which removes the voice-race the OpenAI bridge has
  to wait out before it can greet.
- **Ping/pong is mandatory.** ElevenLabs pings on a timer and closes the socket
  if nothing pongs back. Miss this and calls drop after ~20 seconds with no error
  worth the name.

## What it costs in exchange

Two things are configuration on the ElevenLabs side rather than code here, and
both fail confusingly if they are wrong:

1. **The tools must exist on the agent** as client tools with exactly the names in
   `tools.py`. The agent cannot be handed a tool list at connect time the way a
   Realtime session can. `scripts/elevenlabs_setup.py` creates them.
2. **Prompt and first-message overrides must be allowlisted on the agent.** They
   are off by default, and a disallowed override is *ignored*, not rejected - so
   the symptom is an agent answering with its dashboard prompt and none of this
   call's context, which reads as a bad model rather than a bad config. The setup
   script turns them on; `_configure_session` logs what it sent so the two can be
   compared when a call comes out wrong.

Audio format is the third trap and the loudest: if the agent is not set to
`ulaw_8000` both ways, the caller hears white noise. That one we can detect, so
`conversation_initiation_metadata` is checked on every call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx
import websockets
from websockets.asyncio.client import ClientConnection

from .bridge import EXTRACT_INSTRUCTION, WRAP_UP_INSTRUCTION, BaseBridge

log = logging.getLogger(__name__)

ELEVENLABS_API = "https://api.elevenlabs.io"
SIGNED_URL_PATH = "/v1/convai/conversation/get-signed-url"
DIRECT_WS_URL = "wss://api.elevenlabs.io/v1/convai/conversation"

# The only audio format that can be relayed to Twilio without transcoding. Set on
# the agent, not per conversation - it is not an overridable field.
REQUIRED_AUDIO_FORMAT = "ulaw_8000"

# Server events we act on. Everything else is ignored by design; the agent emits
# a lot of chatter (vad_score, tentative responses, alignment data) that is useful
# for a visual client and noise for a phone line.
EVT_METADATA = "conversation_initiation_metadata"
EVT_AUDIO = "audio"
EVT_INTERRUPTION = "interruption"
EVT_PING = "ping"
EVT_USER_TRANSCRIPT = "user_transcript"
EVT_AGENT_RESPONSE = "agent_response"
EVT_AGENT_CORRECTION = "agent_response_correction"
EVT_CLIENT_TOOL_CALL = "client_tool_call"

# How long the agent has to fall silent before we accept that its closing line is
# finished. There is no "the agent has stopped talking" event we can rely on
# without turning on optional client events, and the cost of guessing wrong in
# either direction is small: too short clips a syllable, too long adds a beat of
# silence before the line drops. Measured against a handful of real sign-offs,
# gaps *within* a sentence stayed under 400 ms.
END_OF_SPEECH_GRACE_S = 1.0

# Bound on the post-hangup extraction turn. Nobody is on the line, so this only
# has to be short enough not to delay the notification noticeably.
EXTRACTION_TIMEOUT_S = 20.0

PLACEHOLDER = re.compile(r"\{\{\s*([^}]*?)\s*\}\}")


def _neutralise_placeholders(text: str, call_sid: str) -> str:
    """Defuse any `{{...}}` left in a rendered prompt.

    ElevenLabs reads `{{name}}` in an overridden prompt as a dynamic variable and
    refuses to start a conversation when it has no value for one. A prompt that
    reached here with a placeholder still in it is already a bug - `persona.render`
    warns about exactly that - but failing the whole call over a typo is a worse
    outcome than reading a stray word aloud, so they are flattened rather than
    left to abort the connection.
    """
    if "{{" not in text:
        return text
    left: list[str] = []

    def flatten(match: re.Match) -> str:
        left.append(match.group(1))
        return match.group(1)

    out = PLACEHOLDER.sub(flatten, text)
    log.warning(
        "call %s: prompt still contained placeholders (%s) - flattened so "
        "ElevenLabs does not reject the conversation",
        call_sid,
        ", ".join(sorted(set(left))),
    )
    return out


class ElevenLabsBridge(BaseBridge):
    """One phone call, answered by an ElevenLabs conversational agent."""

    provider_name = "elevenlabs"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Set once the agent has fallen silent after end_call, so the closing line
        # is played out in full before the line drops.
        self._final_mark_task: asyncio.Task | None = None

    @property
    def _agent(self) -> ClientConnection | None:
        return self._provider

    # -- session -----------------------------------------------------------

    async def _open_provider(self) -> Any:
        url = await self._conversation_url()
        return websockets.connect(url, max_size=None)

    async def _conversation_url(self) -> str:
        """The WebSocket URL for this call.

        A signed URL when an API key is configured, which is the only way to reach
        a private agent - and every agent that can take a message about a real
        person should be private. Public agents connect straight to `?agent_id=`,
        which is supported so a first-run test does not need a key in the
        container before anything has been proved to work.
        """
        agent_id = self._cfg.elevenlabs_agent_id
        if not self._cfg.elevenlabs_api_key:
            log.warning(
                "call %s: no ELEVENLABS_API_KEY, connecting to agent %s as public",
                self._record.call_sid,
                agent_id,
            )
            return f"{DIRECT_WS_URL}?agent_id={agent_id}"

        # Short timeout deliberately: the caller is already connected and hearing
        # silence while this runs. Failing fast produces a recorded bridge_error
        # and a notification; hanging produces neither.
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(
                f"{ELEVENLABS_API}{SIGNED_URL_PATH}",
                params={"agent_id": agent_id},
                headers={"xi-api-key": self._cfg.elevenlabs_api_key},
            )
            resp.raise_for_status()
            signed = resp.json().get("signed_url", "")

        if not signed:
            raise RuntimeError("ElevenLabs returned no signed_url")
        return signed

    async def _configure_session(self) -> None:
        """Send this call's prompt, opening line and language.

        Every field here has to be allowlisted on the agent under
        `platform_settings.overrides`. A field that is not allowlisted is dropped
        silently, so the log line at the end is the only cheap way to tell a
        rejected override from a model that ignored its instructions.
        """
        instructions = _neutralise_placeholders(
            self._session_instructions(), self._record.call_sid
        )

        agent_override: dict[str, Any] = {
            "prompt": {"prompt": instructions},
            "first_message": self._opening_line(),
        }
        if self._cfg.elevenlabs_language:
            agent_override["language"] = self._cfg.elevenlabs_language

        override: dict[str, Any] = {"agent": agent_override}
        # Only sent when explicitly configured. An empty voice_id would override
        # the agent's own voice with nothing.
        if self._cfg.elevenlabs_voice_id:
            override["tts"] = {"voice_id": self._cfg.elevenlabs_voice_id}

        await self._send_agent(
            {
                "type": "conversation_initiation_client_data",
                "conversation_config_override": override,
            }
        )
        log.info(
            "call %s: sent overrides (prompt %d chars, first_message, %s%s)",
            self._record.call_sid,
            len(instructions),
            f"language={self._cfg.elevenlabs_language}"
            if self._cfg.elevenlabs_language
            else "agent language",
            ", voice" if self._cfg.elevenlabs_voice_id else "",
        )

    async def _greet_when_ready(self) -> None:
        """Nothing to do: `first_message` is spoken as the session opens.

        Kept as an explicit no-op rather than left to the base class, because the
        absence of a greeting step here is a meaningful difference from the OpenAI
        bridge rather than an oversight.
        """
        return

    # -- Twilio -> ElevenLabs ----------------------------------------------

    async def _send_caller_audio(self, payload_b64: str) -> None:
        await self._send_agent({"user_audio_chunk": payload_b64})

    # -- ElevenLabs -> Twilio ----------------------------------------------

    async def _pump_provider(self) -> None:
        assert self._agent is not None
        try:
            async for raw in self._agent:
                event = json.loads(raw)
                etype = event.get("type")

                if etype == EVT_AUDIO:
                    await self._handle_agent_audio(event)

                elif etype == EVT_PING:
                    # Mandatory. The socket is closed from the other end if these
                    # go unanswered, which presents as calls dropping mid-sentence.
                    await self._send_agent(
                        {
                            "type": "pong",
                            "event_id": (event.get("ping_event") or {}).get("event_id"),
                        }
                    )

                elif etype == EVT_INTERRUPTION:
                    # The agent has already truncated its own side. All that is
                    # left is to stop Twilio playing what the caller talked over.
                    await self._clear_twilio_playback()

                elif etype == EVT_USER_TRANSCRIPT:
                    self._handle_user_transcript(event)

                elif etype == EVT_AGENT_RESPONSE:
                    self._last_activity = time.monotonic()
                    if self._cfg.retain_transcripts:
                        text = (event.get("agent_response_event") or {}).get(
                            "agent_response", ""
                        )
                        self._record.transcript.append(
                            {"role": "agent", "text": text}
                        )

                elif etype == EVT_AGENT_CORRECTION:
                    # Sent when the agent was cut off: the corrected text is what
                    # the caller actually heard. Only matters to a kept transcript.
                    self._apply_response_correction(event)

                elif etype == EVT_CLIENT_TOOL_CALL:
                    await self._dispatch_tool(event.get("client_tool_call") or {})

                elif etype == EVT_METADATA:
                    self._handle_metadata(event)

        except Exception as exc:  # noqa: BLE001
            log.info(
                "call %s elevenlabs stream ended: %s",
                self._record.call_sid,
                type(exc).__name__,
            )
        finally:
            if self._final_mark_task and not self._final_mark_task.done():
                self._final_mark_task.cancel()
            self._closed.set()

    def _handle_metadata(self, event: dict) -> None:
        """Record which conversation this was, and check the audio actually fits.

        The format check is the important half. Twilio is handed whatever bytes
        arrive and plays them as u-law; a PCM agent produces a loud hiss the
        caller cannot talk over and the agent cannot hear past. It is entirely a
        configuration mistake, it is invisible in the logs otherwise, and it looks
        like a broken phone line rather than a wrong setting.
        """
        meta = event.get("conversation_initiation_metadata_event") or {}
        self._record.provider_conversation_id = meta.get("conversation_id", "")
        log.info(
            "call %s: elevenlabs conversation %s",
            self._record.call_sid,
            self._record.provider_conversation_id or "unknown",
        )

        for field, value in (
            ("agent_output_audio_format", meta.get("agent_output_audio_format")),
            ("user_input_audio_format", meta.get("user_input_audio_format")),
        ):
            if value and value != REQUIRED_AUDIO_FORMAT:
                log.error(
                    "call %s: agent %s is %s, not %s - the caller will hear "
                    "noise. Fix it on the agent (see scripts/elevenlabs_setup.py); "
                    "it cannot be overridden per call.",
                    self._record.call_sid,
                    field,
                    value,
                    REQUIRED_AUDIO_FORMAT,
                )

        self._session_ready.set()

    async def _handle_agent_audio(self, event: dict) -> None:
        audio = (event.get("audio_event") or {}).get("audio_base_64", "")
        await self._forward_audio(audio)
        # Each chunk pushes the hang-up back. The agent is still mid-sentence for
        # as long as audio keeps arriving, whenever end_call happened to land.
        if self._ending:
            self._arm_final_mark()

    def _handle_user_transcript(self, event: dict) -> None:
        """A completed caller turn.

        This is the closest analogue to OpenAI's `speech_started` that is worth
        acting on. `vad_score` fires continuously and would make the loop counter
        meaningless; a finished transcript is one real turn taken.
        """
        self._note_caller_turn()
        if self._cfg.retain_transcripts:
            text = (event.get("user_transcription_event") or {}).get(
                "user_transcript", ""
            )
            self._record.transcript.append({"role": "caller", "text": text})

    def _apply_response_correction(self, event: dict) -> None:
        if not self._cfg.retain_transcripts:
            return
        correction = event.get("agent_response_correction_event") or {}
        corrected = correction.get("corrected_agent_response", "")
        if not corrected:
            return
        # Rewrite the last agent line rather than appending a second one: the
        # caller heard one utterance, cut short.
        for entry in reversed(self._record.transcript):
            if entry.get("role") == "agent":
                entry["text"] = corrected
                return

    # -- ending ------------------------------------------------------------

    def _arm_final_mark(self) -> None:
        """(Re)start the countdown to hanging up.

        Debounced rather than fired on end_call directly, because the prompt asks
        the agent to say its closing line *and then* call end_call, and it obliges
        in either order. Waiting for silence covers both without needing to know
        which happened.
        """
        if self._final_mark_task and not self._final_mark_task.done():
            self._final_mark_task.cancel()
        self._final_mark_task = asyncio.create_task(self._final_mark_after_silence())

    async def _final_mark_after_silence(self) -> None:
        try:
            await asyncio.sleep(END_OF_SPEECH_GRACE_S)
            # Twilio echoes this back only once everything queued ahead of it has
            # played out, so the goodbye is never clipped by the hang-up.
            await self._send_final_mark()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "call %s could not send the final mark: %s",
                self._record.call_sid,
                type(exc).__name__,
            )

    # -- tools -------------------------------------------------------------

    async def _dispatch_tool(self, call: dict) -> None:
        name = call.get("tool_name", "")
        tool_call_id = call.get("tool_call_id", "")
        args = self._parse_arguments(call.get("parameters"))

        result = self._apply_tool(name, args)

        if self.transfer_requested:
            # No result sent, deliberately. Acknowledging it would start the agent
            # on another turn - billed, and spoken over the top of Twilio's
            # <Dial> - when the prompt has already had it say "let me put you
            # through". Stop the bridge and let the server redirect the live call.
            self._closed.set()
            return

        # `result` must be a string on the wire. JSON keeps the shape the agent
        # was told to expect, and `is_error` is what actually makes it re-read the
        # message rather than carry on - a rejected take_message that came back as
        # a success is just as ignored as no reply at all.
        await self._send_agent(
            {
                "type": "client_tool_result",
                "tool_call_id": tool_call_id,
                "result": json.dumps(result),
                "is_error": not result.get("ok", True),
            }
        )

        if self._ending and not self._extracting:
            # The agent carries on speaking by itself, so there is no sign-off to
            # request here the way the OpenAI bridge has to. Start the countdown
            # in case end_call arrived *after* the closing line, in which case no
            # further audio is coming and nothing else would ever arm it.
            self._arm_final_mark()

    async def _nudge_wrap_up_impl(self) -> None:
        """Push the instruction in as context rather than as a spoken turn.

        `contextual_update` is out-of-band: it lands in the agent's context
        without generating a reply and without appearing as something the caller
        said. That is the right shape here - a call that is going in circles is
        one where the caller is still talking, so the agent picks the instruction
        up on the very next turn anyway, and injecting it as a `user_message`
        would put words in the caller's mouth in the transcript.
        """
        await self._send_agent(
            {"type": "contextual_update", "text": WRAP_UP_INSTRUCTION}
        )

    # -- end-of-call extraction --------------------------------------------

    async def _extract_message_if_missing(self) -> None:
        """Recover the message when the agent never got round to recording it.

        Same problem and same reasoning as the OpenAI bridge: the agent will
        happily say "I'll pass that on" and hang up having recorded nothing, and a
        notification that says only "a call happened" is the worst outcome for a
        screened call.

        The mechanism has to be different, because there is no way to force a tool
        call here - no `tool_choice`. What ElevenLabs does offer is `user_message`,
        which injects a text turn and makes the agent respond, and the agent still
        has the whole conversation in context. So the caller has gone, we ask, and
        it calls take_message.

        Two honest caveats. The instruction goes in as a user turn, so a retained
        transcript ends with a line the caller never said - which is why it is
        prefixed to be unmistakable. And the agent will generate speech for its
        reply, into a socket nobody is listening on; that is a few seconds of TTS
        spent to turn an empty notification into a useful one, which is a trade
        worth making.

        This reads the socket directly rather than leaning on `_pump_provider`,
        which `run` has already cancelled by the time we get here.
        """
        if not self._worth_extracting():
            return

        log.info(
            "call %s ended without a message - extracting one",
            self._record.call_sid,
        )
        self._extracting = True

        try:
            await self._send_agent(
                {
                    "type": "user_message",
                    "text": f"[system, not spoken by the caller] {EXTRACT_INSTRUCTION}",
                }
            )
            async with asyncio.timeout(EXTRACTION_TIMEOUT_S):
                async for raw in self._agent:
                    event = json.loads(raw)
                    etype = event.get("type")

                    if etype == EVT_CLIENT_TOOL_CALL:
                        await self._dispatch_tool(event.get("client_tool_call") or {})
                        # It often does its bookkeeping first - classify_call, or
                        # a second end_call - so keep reading until the one that
                        # matters lands or the timeout runs out.
                        if self._record.message_taken:
                            break

                    elif etype == EVT_PING:
                        # Still mandatory down here. A dropped socket mid-
                        # extraction loses the message we came for.
                        await self._send_agent(
                            {
                                "type": "pong",
                                "event_id": (event.get("ping_event") or {}).get(
                                    "event_id"
                                ),
                            }
                        )
        except (TimeoutError, asyncio.TimeoutError):
            log.warning("call %s message extraction timed out", self._record.call_sid)
        except Exception as exc:  # noqa: BLE001
            # The record is updated before the acknowledgement goes out, so a
            # socket that closes on that last write has not actually lost
            # anything. Only say it failed if it did.
            if not self._record.message_taken:
                log.warning(
                    "call %s message extraction failed: %s",
                    self._record.call_sid,
                    type(exc).__name__,
                )

        if not self._record.message_taken:
            log.warning(
                "call %s: extraction produced no message", self._record.call_sid
            )

    # -- plumbing ----------------------------------------------------------

    async def _send_agent(self, payload: dict) -> None:
        if self._agent is None:
            return
        await self._agent.send(json.dumps(payload))

"""Everything a call bridge does that is not specific to a voice provider.

One phone call, from answer to hangup. The parts that differ between OpenAI
Realtime and ElevenLabs Agents are exactly three: how the socket is opened, how
the session is configured, and how each side's events are named. Everything else
- talking to Twilio, keeping playback in step, turning a tool call into a
`CallRecord`, and the guards that stop a call running forever - is identical, so
it lives here and each provider subclasses it.

The fiddly part is barge-in. Twilio buffers whatever we send it, so when the
caller starts talking over the agent there is already queued audio that will keep
playing. Handling it needs three things to happen together: tell the provider to
truncate the item at the point the caller actually heard, tell Twilio to drop its
buffer, and reset our own playback bookkeeping. Miss any one and the agent talks
over the caller or loses its place in the conversation. The middle two are here;
the truncate is provider-specific, because ElevenLabs does it for us and OpenAI
does not.

## Where the privilege boundary sits

A bridge is the component exposed to whatever a caller says down the phone, so it
is deliberately the least-privileged thing in the service. It is handed finished
strings - a rendered prompt, a rendered caller history - and never a handle to
the call log, the contact book, the supervisor or a notifier. Keep it that way:
see `tools.py` for the same argument applied to the tool surface.
"""

from __future__ import annotations

import abc
import asyncio
import base64
import json
import logging
import time
from typing import Any

from . import dtmf
from .callrecord import CallRecord
from .config import Config
from .persona import Persona
from .tools import CALLBACK_OUTCOMES
from .ulaw import mean_level

log = logging.getLogger(__name__)

# Dropped into Twilio's playback queue when the agent has said its closing line.
# Twilio echoes a mark back only once everything queued ahead of it has actually
# played out to the caller, which is what makes it a safe moment to hang up.
FINAL_MARK = "receptionist-final"

# Said to the agent mid-call when it will not stop circling. Shared so both
# providers nudge with the same words; only the delivery mechanism differs.
WRAP_UP_INSTRUCTION = (
    "This call is going round in circles and no message has been recorded. "
    "Stop asking questions. Say one short closing line, call take_message "
    "with whatever you have, and then end_call."
)

# Said to the agent after the caller has gone, to recover a message it never
# got round to recording. Shared for the same reason.
EXTRACT_INSTRUCTION = (
    "The call has ended and the caller has gone. Record the message now by "
    "calling take_message, using only what the caller actually said. Leave a "
    "field empty if they never gave it - do not invent anything. If they gave "
    "nothing usable, still describe what happened in the summary field."
)

# The same, for a callback. There the thing most worth recovering is not a
# message but who actually answered: a summary that assumes the person it rang
# picked up tells the owner a message was delivered when it was not.
EXTRACT_CALLBACK_INSTRUCTION = (
    "The callback has ended and the line is closed. Record how it went now by "
    "calling record_callback_outcome, using only what actually happened: who "
    "really answered, and whether the message reached the person it was for. "
    "If they said anything back for the owner, also call take_message. Do not "
    "invent anything."
)

# Far-end audio louder than this counts as activity for the silence guard, on a
# 16-bit mean-absolute scale (see `ulaw.mean_level`). A quiet line reads in the
# tens; speech and hold music in the thousands. Without it the guard only saw
# *finished* turns, and on 2026-09-10 hung up on a caller twelve seconds into a
# sentence because nothing had finished in the last twenty-five.
CALLER_AUDIO_ACTIVE_LEVEL = 500.0

# Callback tools that navigate a phone system rather than talk to anyone. After
# one of these the agent has to listen: prompting a spoken turn would have it
# talk over the menu, or over the hold music it is waiting through.
LISTENING_TOOLS = frozenset({"press_keys", "wait_on_hold"})

# Longest key sequence in one press. An extension and a `#` fit; a card number
# or a dial-out string does not, which is the point.
MAX_KEYS_PER_PRESS = 12


class BaseBridge(abc.ABC):
    """Owns one phone call from answer to hangup, minus the provider wire format."""

    #: Shown in logs and reported by `/health`. Overridden by each subclass.
    provider_name = "base"

    # The public surface, declared here rather than only assigned in __init__:
    # this is the whole of what `server.py` reads off a bridge once `run()`
    # returns, and a provider subclass that stopped setting one of them would
    # silently strand a caller waiting for a transfer.
    transfer_requested: bool = False
    transfer_reason: str = ""

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
        outbound_to_company: str = "",
        outbound_about: str = "",
        outbound_extension: str = "",
        outbound_category: str = "",
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
        # What a callback has to get past a switchboard to that person with.
        self._outbound_to_company = outbound_to_company
        self._outbound_about = outbound_about
        self._outbound_extension = outbound_extension
        self._outbound_category = outbound_category

        # The provider socket, once open. Typed loosely because the two providers
        # hand back different connection objects.
        self._provider: Any = None

        # Known up front: the server reads Twilio's `start` frame to authenticate
        # the stream before handing the socket over.
        self._stream_sid: str = stream_sid
        self._started = time.monotonic()

        # Playback bookkeeping, all in Twilio's media-timestamp milliseconds.
        self._latest_media_ts: int = 0
        self._response_start_ts: int | None = None
        self._mark_queue: list[str] = []

        self._ending = False
        # True only during end-of-call extraction, when the caller has already
        # gone and there is nobody left to speak to.
        self._extracting = False
        # Set when the agent asks to put the caller through. The bridge then stops
        # driving the call and hands control to Twilio's <Dial>.
        self.transfer_requested = False
        self.transfer_reason = ""
        self._closed = asyncio.Event()
        self._last_activity = time.monotonic()
        # Set when the provider acknowledges the session configuration. The
        # greeting waits on this where the provider needs it to.
        self._session_ready = asyncio.Event()

        # Loop detection: caller turns taken, and whether we have already nudged.
        self._caller_turns = 0
        self._nudged_wrap_up = False
        # Set the first time the far end takes a turn. A callback waits on it
        # before speaking: whoever answers a phone speaks first, and a
        # switchboard's recorded greeting does not stop for anyone.
        self._remote_spoke = asyncio.Event()

        # Callbacks only: working through a switchboard.
        self._navigated = False
        self._keys_pressed: list[str] = []
        self._pending_keys = ""
        self._keypad_busy_until = 0.0
        self._hold_until = 0.0

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> CallRecord:
        """Answer, converse, hang up, and return what was learned.

        A template method: the hooks below are the only places a provider
        differs. Every failure path still returns a record, because a call that
        went wrong is one the owner most needs to be told about.
        """
        try:
            async with await self._open_provider() as conn:
                self._provider = conn
                await self._configure_session()

                # The greeting is NOT sent inline here. Where a provider applies
                # session configuration asynchronously, a greeting racing ahead
                # of it is spoken in the *default* voice - the caller hears one
                # voice say hello and another continue the conversation. Observed
                # on a real call. `_greet_when_ready` waits for the
                # acknowledgement, which only arrives once the pump below is
                # reading events.
                #
                # Deliberately not in the wait set below: it finishes in about a
                # second, and FIRST_COMPLETED would tear the call down with it.
                greeter = asyncio.create_task(self._greet_when_ready())

                tasks = [
                    asyncio.create_task(self._pump_twilio()),
                    asyncio.create_task(self._pump_provider()),
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

            # The provider socket is closed from here on - which is exactly when
            # a provider that analyses its own conversations has one to hand back.
            if not self.transfer_requested and not self._extraction_complete():
                await self._recover_after_close()
        except Exception as exc:  # noqa: BLE001 - a failed call must still be recorded
            log.error(
                "call %s bridge error (%s): %s: %s",
                self._record.call_sid,
                self.provider_name,
                type(exc).__name__,
                exc,
            )
            if not self._record.end_reason:
                self._record.end_reason = "bridge_error"

        self._record.duration_s = time.monotonic() - self._started
        if (self.is_outbound and not self._record.callback_outcome
                and not self._caller_turns):
            # Nothing on the line ever spoke, so nobody can have been given the
            # message. Say so, rather than leave the summary to guess.
            self._record.callback_outcome = "no_one_answered"
        if self.transfer_requested:
            self._record.end_reason = "transferred"
        if not self._record.end_reason:
            self._record.end_reason = "caller_hung_up"
        return self._record

    @property
    def is_outbound(self) -> bool:
        """A callback, rather than someone ringing in."""
        return bool(self._outbound_script)

    # -- provider hooks ----------------------------------------------------

    @abc.abstractmethod
    async def _open_provider(self) -> Any:
        """Return an async context manager yielding the provider's socket.

        Async so a provider that has to fetch a short-lived credential first -
        ElevenLabs mints a signed URL - can do it here rather than in `run`.
        """

    @abc.abstractmethod
    async def _configure_session(self) -> None:
        """Send whatever the provider needs before the first word is spoken."""

    @abc.abstractmethod
    async def _pump_provider(self) -> None:
        """Read provider events until the conversation ends."""

    @abc.abstractmethod
    async def _send_caller_audio(self, payload_b64: str) -> None:
        """Hand one base64 G.711 u-law frame from the caller to the provider."""

    @abc.abstractmethod
    async def _greet_when_ready(self) -> None:
        """Speak first, rather than waiting for the caller to open."""

    async def _nudge_wrap_up_impl(self) -> None:
        """Tell the agent, mid-call, to stop circling and close.

        Optional: a provider with nothing suitable can leave this alone and rely
        on the watchdog's hard limits instead.
        """

    async def _extract_message_if_missing(self) -> None:
        """Recover the message when the agent never got round to recording it.

        Optional, and worth implementing for every provider that can: without it
        a call where the agent said "I'll pass that on" and then hung up having
        recorded nothing produces a notification saying a call happened and
        nothing else, which is the worst outcome for a screened call.
        """

    async def _recover_after_close(self) -> None:
        """A last chance to recover what a call was about, once it has closed.

        Optional. Runs only when extraction left nothing recorded, after the
        provider socket is shut - which is when a provider that analyses its own
        conversations has something to hand back.
        """

    def _extraction_complete(self) -> bool:
        """Whether what end-of-call extraction exists to recover is recorded.

        A message, on an inbound call. On a callback, how it went: who really
        answered matters more than anything they said back.
        """
        if self.is_outbound:
            return bool(self._record.callback_outcome)
        return self._record.message_taken

    def _extract_instruction(self) -> str:
        if self.is_outbound:
            return EXTRACT_CALLBACK_INSTRUCTION
        return EXTRACT_INSTRUCTION

    def _worth_extracting(self) -> bool:
        """Whether there is anything to go back and recover.

        Three ways there is not, and the last was found on a live silent call:
        the agent spent its whole extraction timeout being asked to summarise a
        conversation that never happened, turning a 12-second call into a
        32-second one and delaying the notification for nothing. Screening lines
        get a lot of silent and automated calls, so this is the common case, not
        an edge one.
        """
        if self._extraction_complete() or self._provider is None:
            return False
        # Nothing meaningful can have been said in a couple of seconds.
        if time.monotonic() - self._started < 8:
            return False
        if self._caller_turns == 0:
            # The caller never took a turn at all. Both providers count these off
            # the event that drives the conversation - OpenAI's VAD firing,
            # ElevenLabs completing a transcript - so a zero here means the agent
            # has nothing in context to recover.
            log.info(
                "call %s: caller never spoke, nothing to extract",
                self._record.call_sid,
            )
            return False
        return True

    # -- prompt assembly ---------------------------------------------------

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
                + self._callback_routing(who)
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

    def _callback_routing(self, who: str) -> str:
        """What a callback has to go on when a switchboard answers, not the person.

        All of it is what the caller told us about themselves - their company,
        their team, what they rang about - so repeating the routing parts to
        their own switchboard discloses nothing of the owner's.
        """
        facts = [f"- Name: {who}"]
        if self._outbound_to_company:
            facts.append(f"- Where they called from: {self._outbound_to_company}")
        if self._outbound_extension:
            facts.append(f"- Their extension: {self._outbound_extension}")
        if self._outbound_category:
            kind = self._outbound_category.replace("_", " ")
            facts.append(f"- Kind of call: {kind}")
        if self._outbound_about:
            facts.append(
                f"- What they rang about, in their words: {self._outbound_about}"
            )
        return (
            "\n\n## Who to ask for\n\n"
            "If a switchboard, a menu or a colleague answers instead of them, this "
            "is what you have to get through to the right person with. Follow "
            '"Switchboards, menus and hold" above for how to use it.\n\n'
            + "\n".join(facts)
        )

    def _opening_line(self) -> str:
        """The exact words the agent opens with.

        Given verbatim rather than left to the model's recall of the prompt -
        asked to "greet the caller", it improvises something chatty that never
        identifies who is speaking.
        """
        assistant = self._persona.assistant_name
        if self.is_outbound:
            who = self._outbound_to_name
            if who:
                return f"Hello, is that {who}? It's {assistant}, returning your call."
            return f"Hello, it's {assistant} — returning your call from earlier."

        # Asking "who am I speaking to?" when caller ID already matched a saved
        # contact is the single most obviously robotic thing this can do - it knew
        # who was ringing and asked anyway. Greet them by name instead.
        known = self._record.known_contact_name
        if known:
            return f"Hello {known}, it's {assistant} — what can I do for you?"
        return self._cfg.greeting

    def _transfer_available(self) -> bool:
        """Transfer is opt-in per deployment, and never during a callback."""
        return bool(
            self._cfg.transfer_enabled
            and self._cfg.transfer_to_number
            and not self.is_outbound
        )

    # -- Twilio ------------------------------------------------------------

    async def _pump_twilio(self) -> None:
        try:
            async for raw in self._twilio.iter_text():
                msg = json.loads(raw)
                event = msg.get("event")

                if event == "media":
                    self._latest_media_ts = int(msg["media"]["timestamp"])
                    payload = msg["media"]["payload"]
                    await self._send_caller_audio(payload)
                    # Sound from the far end keeps the silence guard off whether
                    # or not a turn has finished: someone mid-sentence, or a line
                    # playing hold music, is not dead air.
                    level = mean_level(base64.b64decode(payload))
                    if level > CALLER_AUDIO_ACTIVE_LEVEL:
                        self._last_activity = time.monotonic()

                elif event == "start":
                    self._stream_sid = msg["start"]["streamSid"]
                    log.info("call %s media stream started", self._record.call_sid)

                elif event == "mark":
                    name = msg.get("mark", {}).get("name", "")
                    if self._mark_queue:
                        self._mark_queue.pop(0)
                    if name == FINAL_MARK:
                        # The closing line has finished playing out to the caller.
                        log.info(
                            "call %s closing after final mark", self._record.call_sid
                        )
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

    async def _forward_audio(self, delta_b64: str) -> None:
        """Play one base64 u-law frame from the agent out to the caller."""
        if not delta_b64 or not self._stream_sid:
            return

        # Re-encoding through base64 rather than forwarding the string directly
        # normalises any padding differences between the two APIs.
        payload = base64.b64encode(base64.b64decode(delta_b64)).decode("ascii")
        await self._twilio.send_json(
            {
                "event": "media",
                "streamSid": self._stream_sid,
                "media": {"payload": payload},
            }
        )

        if self._response_start_ts is None:
            self._response_start_ts = self._latest_media_ts

        await self._send_mark()

    async def _clear_twilio_playback(self) -> None:
        """Drop whatever Twilio still has queued, and forget where we were.

        Called on barge-in. Twilio may hold seconds of audio the caller has not
        heard yet; without this the agent keeps talking over them long after it
        has stopped generating.
        """
        if time.monotonic() < self._keypad_busy_until:
            # Keypad tones are still playing out. A menu starts talking the
            # moment it hears the first one, and clearing now would wipe the
            # rest of the sequence out of Twilio's buffer mid-extension.
            return
        if self._stream_sid:
            await self._twilio.send_json(
                {"event": "clear", "streamSid": self._stream_sid}
            )
        self._mark_queue.clear()
        self._response_start_ts = None

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

    async def _send_final_mark(self) -> None:
        """Ask Twilio to tell us when the closing line has finished playing."""
        if not self._stream_sid:
            return
        await self._twilio.send_json(
            {
                "event": "mark",
                "streamSid": self._stream_sid,
                "mark": {"name": FINAL_MARK},
            }
        )

    async def _play_pending_keys(self) -> None:
        """Play the keys `press_keys` accepted into the call, as tones."""
        keys, self._pending_keys = self._pending_keys, ""
        if not keys or not self._stream_sid:
            return
        # Anything the agent still has queued would play first and push the tones
        # past the moment the menu is listening for them.
        await self._clear_twilio_playback()
        for frame in dtmf.frames(dtmf.tones(keys)):
            await self._twilio.send_json(
                {
                    "event": "media",
                    "streamSid": self._stream_sid,
                    "media": {"payload": frame},
                }
            )
        now = time.monotonic()
        self._keypad_busy_until = now + dtmf.duration_s(keys) + 0.5
        self._last_activity = now

    # -- tools -------------------------------------------------------------

    def _note_caller_turn(self) -> None:
        """The caller started speaking: keep the idle and loop guards honest."""
        self._last_activity = time.monotonic()
        self._caller_turns += 1
        self._remote_spoke.set()

    async def _run_tool(self, name: str, args: dict) -> dict:
        """Apply a tool call, then do whatever it needs doing on the line.

        `_apply_tool` stays pure bookkeeping. The one tool with an effect on the
        call itself - pressing keys - is played out here, once the bookkeeping
        has accepted it.
        """
        result = self._apply_tool(name, args)
        if self._pending_keys:
            try:
                await self._play_pending_keys()
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "call %s could not play keypad tones: %s",
                    self._record.call_sid,
                    type(exc).__name__,
                )
                return {"ok": False, "error": "the keys could not be pressed"}
        return result

    def _apply_tool(self, name: str, args: dict) -> dict:
        """Fold one tool call into the call record. Returns the result to send back.

        Pure bookkeeping, identical for every provider: nothing here reads
        anything, opens anything, or reaches the network. That is the whole point
        of the tool surface - see `tools.py`.
        """
        log.info("call %s tool=%s", self._record.call_sid, name)

        if name == "classify_call":
            self._record.category = args.get("category", self._record.category)
            self._record.urgency = args.get("urgency", self._record.urgency)
            return {"ok": True}

        if name == "take_message":
            # Refuse an empty message rather than recording one. Observed calling
            # this with every field blank when it was prompted to continue and had
            # nothing gathered yet - which produced a notification saying
            # nothing at all, the worst possible outcome for a screened call.
            if not (args.get("caller_name") or args.get("reason")):
                log.warning(
                    "call %s take_message had no name or reason - rejected",
                    self._record.call_sid,
                )
                return {
                    "ok": False,
                    "error": (
                        "You have not gathered anything to pass on yet. Ask the "
                        "caller who they are and what they need, then call this "
                        "again with what they told you."
                    ),
                }
            for field_name in (
                "caller_name",
                "company_or_relationship",
                "callback_number",
                "extension",
                "reason",
                "requested_action",
                "summary",
            ):
                if args.get(field_name):
                    setattr(self._record, field_name, args[field_name])
            self._record.message_taken = True
            return {"ok": True}

        if name == "flag_urgent":
            self._record.notify_flagged = True
            self._record.notify_why = args.get("why", "")
            self._record.urgency = args.get("urgency", self._record.urgency)
            return {"ok": True}

        if name == "transfer_call":
            if not (self._cfg.transfer_enabled and self._cfg.transfer_to_number):
                return {"ok": False, "error": "transfers are not available"}
            if self.is_outbound:
                return {"ok": False, "error": "cannot transfer during a callback"}
            if self._extracting:
                # Extraction runs after the caller has hung up. Accepting this
                # would have the server redirect a call that no longer exists,
                # and ring the owner about a caller who has already gone.
                return {"ok": False, "error": "the call has already ended"}
            self.transfer_requested = True
            self.transfer_reason = args.get("reason", "")
            self._record.transfer_attempted = True
            self._record.transfer_reason = self.transfer_reason
            log.info(
                "call %s transfer requested: %s",
                self._record.call_sid,
                self.transfer_reason,
            )
            return {"ok": True}

        if name in ("press_keys", "wait_on_hold", "record_callback_outcome"):
            if not self.is_outbound:
                return {"ok": False, "error": "only available on a callback"}
            return self._apply_callback_tool(name, args)

        if name == "end_call":
            self._record.end_reason = args.get("reason", "other")
            self._ending = True
            return {"ok": True}

        log.warning("call %s unknown tool %s", self._record.call_sid, name)
        return {"ok": False, "error": "unknown tool"}

    def _apply_callback_tool(self, name: str, args: dict) -> dict:
        """The three tools a callback gets for working through a switchboard."""
        sid = self._record.call_sid

        if name == "record_callback_outcome":
            outcome = str(args.get("outcome", "") or "")
            if outcome not in CALLBACK_OUTCOMES:
                return {
                    "ok": False,
                    "error": "outcome must be one of: " + ", ".join(CALLBACK_OUTCOMES),
                }
            self._record.callback_outcome = outcome
            self._record.spoke_to = str(args.get("spoke_to", "") or "")[:120]
            self._record.callback_notes = str(args.get("notes", "") or "")[:300]
            log.info("call %s callback outcome=%s", sid, outcome)
            return {"ok": True}

        # The other two act on a live line, and during extraction there is none.
        if self._extracting:
            return {"ok": False, "error": "the call has already ended"}

        if name == "wait_on_hold":
            self._navigated = True
            now = time.monotonic()
            self._hold_until = now + self._cfg.callback_hold_max_s
            self._last_activity = now
            log.info("call %s on hold", sid)
            return {
                "ok": True,
                "note": "Waiting. Say nothing until a person speaks to you.",
            }

        keys = "".join(str(args.get("keys", "") or "").split())
        if (not keys or len(keys) > MAX_KEYS_PER_PRESS
                or not dtmf.VALID_KEYS.match(keys)):
            return {
                "ok": False,
                "error": f"keys must be 1-{MAX_KEYS_PER_PRESS} of 0-9, * and #",
            }
        if len(self._keys_pressed) >= self._cfg.callback_max_key_presses:
            # A real menu tree is a handful of presses. Past the ceiling this is
            # a model going round a menu - or being talked into dialling.
            log.warning("call %s keypad limit reached", sid)
            return {
                "ok": False,
                "error": (
                    "You have pressed keys too many times on this call. Stop "
                    "navigating: record the outcome as automated_dead_end and "
                    "end the call, unless a person is already speaking to you."
                ),
            }
        self._navigated = True
        self._keys_pressed.append(keys)
        self._record.keys_pressed = " ".join(self._keys_pressed)
        self._pending_keys = keys
        log.info("call %s pressing %s", sid, keys)
        return {
            "ok": True,
            "note": "Pressed. Stay silent and listen for what comes next.",
        }

    @staticmethod
    def _parse_arguments(raw: Any) -> dict:
        """Normalise tool arguments.

        One provider sends them as a JSON string, the other as an object, and
        neither is guaranteed to be well formed - this is model output.
        """
        if isinstance(raw, dict):
            return raw
        try:
            parsed = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    # -- guards ------------------------------------------------------------

    async def _maybe_nudge_wrap_up(self) -> None:
        """Push a stalled call towards a close.

        A caller who wants something the agent will not give asks the same thing
        several ways, and the agent politely declines each time without ever
        taking a message or hanging up. Prompt wording helps but does not reliably
        stop it, so past a threshold the service says so directly.
        """
        if self._nudged_wrap_up or self._ending or self._extracting:
            return
        if self._navigated:
            # Getting through a switchboard is slow and records no message on
            # the way. Told to wrap up, the agent would abandon the menu it is
            # halfway through; the callback's own time limits bound it instead.
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
            await self._nudge_wrap_up_impl()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "call %s wrap-up nudge failed: %s",
                self._record.call_sid,
                type(exc).__name__,
            )

    def _silence_limit(self) -> float:
        """How long a quiet line is tolerated right now.

        Longer while the agent has said it is on hold: a queue that plays
        nothing for half a minute is still a queue.
        """
        limit = float(self._cfg.silence_hangup_seconds)
        if time.monotonic() < self._hold_until:
            limit = max(limit, float(self._cfg.callback_hold_max_s))
        return limit

    async def _watchdog(self) -> None:
        """Guards against calls that never end: dead air, or a caller who won't stop."""
        # A callback gets longer: a switchboard, a menu and a queue take time.
        max_seconds = (
            self._cfg.callback_max_call_seconds
            if self.is_outbound
            else self._cfg.max_call_seconds
        )
        while not self._closed.is_set():
            await asyncio.sleep(1)

            if time.monotonic() - self._started > max_seconds:
                log.info("call %s hit max duration", self._record.call_sid)
                self._record.end_reason = "max_duration"
                return

            await self._maybe_nudge_wrap_up()

            idle = time.monotonic() - self._last_activity
            if idle > self._silence_limit():
                log.info("call %s silent for %.0fs", self._record.call_sid, idle)
                self._record.end_reason = "silent_or_bot"
                return

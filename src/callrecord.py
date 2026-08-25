"""The only thing this service keeps about a call.

Deliberately narrow: the fields listed in the project's security model and nothing
else. Full transcripts are not retained unless RETAIN_TRANSCRIPTS is explicitly
turned on, which is a policy decision, not a default.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def _redact_number(number: str) -> str:
    """Keep enough of a phone number to recognise it, not enough to leak it in logs."""
    if not number:
        return ""
    digits = "".join(c for c in number if c.isdigit())
    if len(digits) <= 4:
        return "*" * len(digits)
    return f"{number[:3]}...{digits[-3:]}"


@dataclass
class CallRecord:
    call_sid: str
    # "inbound" for someone ringing in, "outbound" for a callback we placed.
    direction: str = "inbound"
    from_number: str = ""
    to_number: str = ""
    # Set when the call arrived via carrier forwarding from the owner's mobile.
    forwarded_from: str = ""
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    ended_at: str = ""
    duration_s: float = 0.0

    # Collected by the agent via tools.
    # Resolved from caller ID against the local contacts file, before the agent
    # speaks. Treated as "probably who this is", never as proof of identity.
    known_contact_name: str = ""
    # The rest of what the owner saved about them, when filled in.
    known_contact_full_name: str = ""
    known_contact_relationship: str = ""
    known_contact_notes: str = ""
    caller_name: str = ""
    company_or_relationship: str = ""
    callback_number: str = ""
    reason: str = ""
    requested_action: str = ""
    summary: str = ""
    category: str = "unknown"
    urgency: str = "normal"

    # Operational.
    end_reason: str = ""
    notify_flagged: bool = False
    notify_why: str = ""
    message_taken: bool = False
    transfer_attempted: bool = False
    transfer_connected: bool = False
    # The summary the supervisor composed. Kept here so the call is still legible
    # even when the supervisor has no messaging tool wired up to deliver it.
    supervisor_summary: str = ""

    # Only populated when RETAIN_TRANSCRIPTS is on.
    transcript: list[dict] = field(default_factory=list)

    def resolved_callback(self) -> str:
        """The number to actually ring back.

        The agent writes whatever the caller said into `callback_number`, which is
        sometimes a phrase rather than digits - "use this number", "same as before".
        Anything without a plausible run of digits is discarded in favour of the
        caller ID, which Twilio gave us and which is what they meant anyway.

        Caller ID also wins over a *mistranscription*. This has happened in
        production: the agent wrote a number with one extra trailing digit and
        the callback dialled it, connecting only because the carrier discarded
        the overdialled digit. On a network without that tolerance it reaches a
        stranger. A number the agent heard down a phone line is strictly less
        trustworthy than the one Twilio handed us, so it has to clear two checks
        before it is preferred.
        """
        spoken = "".join(c for c in self.callback_number if c.isdigit())
        if len(spoken) < 7:
            return self.from_number

        caller_id = "".join(c for c in self.from_number if c.isdigit())
        if caller_id:
            # One is a prefix of the other: same number, misheard length. This is
            # the overdialling case, and the verified one wins.
            if spoken.startswith(caller_id) or caller_id.startswith(spoken):
                return self.from_number

            # Implausible length for the caller's own country is a transcription
            # error too. UK E.164 mobiles and landlines are 12 digits (44 + 10).
            if caller_id.startswith("44") and spoken.startswith("44") and len(spoken) != 12:
                return self.from_number

        # E.164 tops out at 15 digits; anything longer cannot be dialled anyway.
        if len(spoken) > 15:
            return self.from_number

        return self.callback_number

    def to_supervisor_payload(self) -> dict:
        """The sanitized structure handed to the supervisor.

        Note what is absent: no audio, no raw caller utterances, no stream ids, no
        credentials. The supervisor sees a summary written by the receptionist, not
        anything the caller authored directly.
        """
        payload = {
            "call_sid": self.call_sid,
            "direction": self.direction,
            "from_number": self.from_number,
            "started_at": self.started_at,
            "duration_s": round(self.duration_s, 1),
            "known_contact_name": self.known_contact_name,
            "caller_name": self.caller_name,
            "company_or_relationship": self.company_or_relationship,
            "callback_number": self.resolved_callback(),
            "category": self.category,
            "reason": self.reason,
            "requested_action": self.requested_action,
            "claimed_urgency": self.urgency,
            "summary": self.summary,
            "end_reason": self.end_reason,
            "flagged_urgent": self.notify_flagged,
            "flagged_why": self.notify_why,
            "message_taken": self.message_taken,
        }
        # Drop empty values rather than sending a skeleton of blanks. The
        # supervisor is asked to write prose and mention only what is known;
        # handing it a form full of "" invites it to echo the form back.
        return {
            k: v for k, v in payload.items() if v not in ("", None)
        }

    def log_line(self) -> str:
        """A one-line operational summary safe to print to container logs."""
        return (
            f"call={self.call_sid} from={_redact_number(self.from_number)} "
            f"dur={self.duration_s:.0f}s category={self.category} "
            f"urgency={self.urgency} end={self.end_reason} "
            f"message={'yes' if self.message_taken else 'no'}"
        )


def append_record(log_dir: Path, record: CallRecord, retain_transcripts: bool) -> Path:
    """Append one JSON line to today's call log. Returns the file written."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"calls-{datetime.now(timezone.utc):%Y-%m}.jsonl"

    data = asdict(record)
    if not retain_transcripts:
        data.pop("transcript", None)

    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(data, ensure_ascii=False) + "\n")
    return path

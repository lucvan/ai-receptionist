"""Remembering which Telegram message belongs to which call.

When someone replies to a summary, we need to know who they are replying
*about* - the callback number, who the caller said they were, and what the call
was about, so the outbound agent has something to open with.

Telegram gives us `reply_to_message.message_id`, so the mapping is simply the id of
the message we sent when the call ended. Kept in a small JSON file rather than
memory so a container restart does not orphan every outstanding message.

Only the fields needed to place and open a callback are stored, and entries expire -
this is a working set, not an archive. The call log remains the record of what
happened.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# How long a summary stays repliable. Long enough to reply the next morning,
# short enough that the file does not grow without bound.
DEFAULT_TTL_S = 7 * 24 * 3600
MAX_ENTRIES = 500


@dataclass
class PendingCall:
    call_sid: str
    number: str
    caller_name: str
    category: str
    summary: str
    created_at: float

    @property
    def display_name(self) -> str:
        return self.caller_name or "the caller"


class PendingStore:
    def __init__(self, path: Path, ttl_s: int = DEFAULT_TTL_S):
        self._path = path
        self._ttl = ttl_s
        self._entries: dict[str, PendingCall] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for key, value in (raw or {}).items():
            try:
                self._entries[str(key)] = PendingCall(**value)
            except TypeError:
                continue
        self._prune()

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({k: asdict(v) for k, v in self._entries.items()}),
                encoding="utf-8",
            )
        except OSError as exc:
            log.error("could not persist pending calls: %s", exc)

    def _prune(self) -> None:
        cutoff = time.time() - self._ttl
        self._entries = {
            k: v for k, v in self._entries.items() if v.created_at >= cutoff
        }
        if len(self._entries) > MAX_ENTRIES:
            newest = sorted(
                self._entries.items(), key=lambda kv: kv[1].created_at, reverse=True
            )[:MAX_ENTRIES]
            self._entries = dict(newest)

    def remember(self, message_id: int, call: PendingCall) -> None:
        self._entries[str(message_id)] = call
        self._prune()
        self._save()

    def get(self, message_id: int) -> PendingCall | None:
        self._prune()
        return self._entries.get(str(message_id))

    def forget(self, message_id: int) -> None:
        if self._entries.pop(str(message_id), None) is not None:
            self._save()

    def most_recent(self) -> PendingCall | None:
        """For a reply that does not quote anything - assume the latest call."""
        self._prune()
        if not self._entries:
            return None
        return max(self._entries.values(), key=lambda c: c.created_at)


class CallbackStore:
    """Callbacks placed but not yet answered, keyed by their stream token.

    On disk rather than in memory because there are two round trips between
    placing a call and the agent speaking: Twilio fetches the TwiML, then opens
    the media socket. A container restart in that window would otherwise leave
    someone answering the phone to a service that has forgotten why it rang them.

    Entries are short-lived - if a call is not answered within a few minutes it
    never will be.
    """

    def __init__(self, path: Path, ttl_s: int = 600):
        self._path = path
        self._ttl = ttl_s

    def _read(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        cutoff = time.time() - self._ttl
        return {
            k: v
            for k, v in (data or {}).items()
            if isinstance(v, dict) and v.get("created_at", 0) >= cutoff
        }

    def _write(self, data: dict) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(data), encoding="utf-8")
        except OSError as exc:
            log.error("could not persist pending callbacks: %s", exc)

    def put(self, token: str, payload: dict) -> None:
        data = self._read()
        data[token] = {**payload, "created_at": time.time()}
        self._write(data)

    def peek(self, token: str) -> dict | None:
        return self._read().get(token)

    def take(self, token: str) -> dict | None:
        """Fetch and remove - a callback token is good for exactly one call."""
        data = self._read()
        entry = data.pop(token, None)
        if entry is not None:
            self._write(data)
        return entry

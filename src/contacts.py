"""Recognising a caller from their number.

Caller ID tells you who is *probably* ringing. It is trivially spoofable - faking
a CLI needs no special access and is standard practice for scam calls, precisely
because people trust the number on the screen. So a match here changes only the
*tone* of the call: the agent greets someone by the name the owner actually uses for them
and stops interrogating them for details we already have. It never unlocks
anything on the do-not-disclose list. That rule is enforced in the session
instructions, not here.

Why a local file rather than syncing from WhatsApp: a WhatsApp bridge returns
contacts as `@lid` JIDs (WhatsApp's linked-ID scheme), not phone numbers -
observed as 50 of 50 on a real account, including the account holder's own test
number. There is no number-to-name mapping to be had from it, so there is nothing
to sync.

## File format

Either form is accepted, per entry, so the original flat file still loads:

    {"+447700900123": "Dana"}

    {"+447700900123": {"name": "Dana Okoro", "nickname": "Dana",
                       "relationship": "partner", "notes": "..."}}
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Everything is normalised to E.164-ish digits so that "07700 900123",
# "+447700900123" and "0044 7700 900123" all match the same entry.
DEFAULT_COUNTRY_CODE = "44"


def normalise(number: str, country_code: str = DEFAULT_COUNTRY_CODE) -> str:
    """Reduce a number to bare international digits, or "" if unusable."""
    if not number:
        return ""

    raw = number.strip().lower()
    if raw in {"anonymous", "restricted", "unavailable", "private", "unknown"}:
        return ""

    digits = re.sub(r"[^\d+]", "", raw)
    if not digits:
        return ""

    if digits.startswith("+"):
        digits = digits[1:]
    elif digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        # National format: 07700 900123 -> 447700900123
        digits = country_code + digits[1:]

    return digits if digits.isdigit() else ""


def near_miss(a: str, b: str) -> bool:
    """True when two numbers are the same number, heard slightly wrong.

    Covers the two ways a number written down from speech goes wrong: a digit
    too many or too few (one string being a prefix of the other, which is how a
    real callback once got dialled to an overlong number), and a single wrong or
    transposed digit.

    Deliberately narrow. Two numbers differing by two or more digits are treated
    as genuinely different, because quietly redirecting a call to the wrong
    person is worse than failing to correct a typo.
    """
    if not a or not b or a == b:
        return bool(a and a == b)

    if abs(len(a) - len(b)) > 1:
        return False

    if len(a) != len(b):
        longer, shorter = (a, b) if len(a) > len(b) else (b, a)
        # One inserted or dropped digit, anywhere.
        for i in range(len(longer)):
            if longer[:i] + longer[i + 1 :] == shorter:
                return True
        return False

    diff = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    if len(diff) == 1:
        return True
    # Two adjacent digits swapped.
    if len(diff) == 2 and diff[1] == diff[0] + 1:
        i, j = diff
        return a[i] == b[j] and a[j] == b[i]
    return False


@dataclass(frozen=True)
class Contact:
    """What the owner has saved about someone who might ring."""

    number: str = ""
    name: str = ""
    nickname: str = ""
    relationship: str = ""
    notes: str = ""

    @property
    def display(self) -> str:
        """What the agent should call them out loud."""
        return self.nickname or self.name

    @property
    def label(self) -> str:
        """How they read in the UI: "Dana Okoro (Dana)"."""
        if self.nickname and self.name and self.nickname != self.name:
            return f"{self.name} ({self.nickname})"
        return self.name or self.nickname

    def to_json(self) -> dict | str:
        """Collapse back to a bare string when there is nothing else to say."""
        extra = {
            k: v
            for k, v in (
                ("nickname", self.nickname),
                ("relationship", self.relationship),
                ("notes", self.notes),
            )
            if v
        }
        if not extra:
            return self.name
        return {"name": self.name, **extra}


def _parse(number: str, value) -> Contact:
    if isinstance(value, dict):
        return Contact(
            number=number,
            name=str(value.get("name") or "").strip(),
            nickname=str(value.get("nickname") or "").strip(),
            relationship=str(value.get("relationship") or "").strip(),
            notes=str(value.get("notes") or "").strip(),
        )
    return Contact(number=number, name=str(value or "").strip())


class ContactBook:
    """A number -> contact map, reloaded when the file changes on disk."""

    def __init__(self, path: Path, country_code: str = DEFAULT_COUNTRY_CODE):
        self._path = path
        self._country_code = country_code
        self._entries: dict[str, Contact] = {}
        self._mtime: float | None = None
        self.reload()

    def reload(self) -> None:
        try:
            stat = self._path.stat()
        except OSError:
            if self._entries:
                log.warning("contacts file disappeared: %s", self._path)
            self._entries = {}
            self._mtime = None
            return

        if self._mtime == stat.st_mtime:
            return

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Keep whatever we already had rather than silently losing recognition
            # because someone left a trailing comma in the file.
            log.error("could not read contacts file: %s", exc)
            return

        entries: dict[str, Contact] = {}
        for number, value in (raw or {}).items():
            key = normalise(str(number), self._country_code)
            contact = _parse(str(number), value)
            if key and (contact.name or contact.nickname):
                entries[key] = contact

        self._entries = entries
        self._mtime = stat.st_mtime
        log.info("loaded %d contacts", len(entries))

    def get(self, number: str) -> Contact | None:
        self.reload()
        key = normalise(number, self._country_code)
        return self._entries.get(key) if key else None

    def get_by_key(self, key: str) -> Contact | None:
        """Look up by an already-resolved profile key.

        Used when a number has been merged into another: the caller rings on a
        second number, the history index resolves it to the profile, and the
        contact saved against that profile is the right one to greet them by.
        """
        self.reload()
        return self._entries.get(key) if key else None

    def lookup(self, number: str) -> str:
        """The name to greet this caller by, or "" if they are not saved."""
        contact = self.get(number)
        return contact.display if contact else ""

    def numbers(self) -> list[str]:
        """Every saved number, normalised - the authoritative set."""
        self.reload()
        return list(self._entries)

    def canonical_key(self, key: str) -> str:
        """Fold a near-miss number onto the saved contact it clearly belongs to.

        Used when indexing call history, so that a call recorded against a
        mistyped number still shows up under the right person instead of
        splitting them across two profiles. The log itself is left alone - it is
        an accurate record of what was actually dialled.
        """
        if not key or key in self._entries:
            return key
        self.reload()
        if key in self._entries:
            return key
        matches = [k for k in self._entries if near_miss(k, key)]
        return matches[0] if len(matches) == 1 else key

    def correct(self, number: str) -> str:
        """Snap a nearly-right number onto the saved one it is clearly meant to be.

        Returns the number unchanged unless exactly one saved contact is a
        near miss. Ambiguity is left alone: if two saved numbers are both one
        digit away, we do not know which was meant.
        """
        key = normalise(number, self._country_code)
        if not key:
            return number
        self.reload()
        if key in self._entries:
            return number

        matches = [k for k in self._entries if near_miss(k, key)]
        if len(matches) != 1:
            return number
        return self._entries[matches[0]].number or f"+{matches[0]}"

    def __len__(self) -> int:
        # Reload first, or /health reports the count from process start and looks
        # like edits to the file were ignored. The check is mtime-gated, so this is
        # a stat() in the common case.
        self.reload()
        return len(self._entries)

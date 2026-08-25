"""Prior calls, indexed by the number they came from.

Every call has always been appended to `logs/calls-YYYY-MM.jsonl`; this just reads
those back and groups them by caller. Nothing new is collected and nothing extra is
retained - the fields here are the same ones the security model already allows.

Why this is worth having where a mid-call supervisor consult was not: it is a
signal the agent cannot otherwise have, and it costs a file read rather than a
5-16 second network round trip. Knowing that this is the third time someone has
rung this week is the difference between a useful screening and a fresh
interrogation every time.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .contacts import normalise

log = logging.getLogger(__name__)

# How far back to read. Call volume here is a handful a day, so this is about
# keeping the agent's context small and relevant, not about disk.
DEFAULT_LOOKBACK_DAYS = 180


def _humanise_age(iso_ts: str) -> str:
    """"yesterday", "3 days ago", "last month" - what a person would say."""
    try:
        then = datetime.fromisoformat(iso_ts)
    except ValueError:
        return ""
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)

    days = (datetime.now(timezone.utc) - then).days
    if days <= 0:
        return "earlier today"
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        return f"{days // 7} weeks ago"
    return f"{days // 30} months ago"


class CallHistory:
    """Reads the call log and answers "who is this and have they rung before?"."""

    def __init__(self, log_dir: Path, country_code: str = "44", canonical=None):
        self._log_dir = log_dir
        self._country_code = country_code
        # Optional (key -> key) that folds a mistyped number onto the saved
        # contact it belongs to, so one person is not split across two profiles
        # by a single misheard digit. See ContactBook.canonical_key.
        self._canonical = canonical or (lambda key: key)
        self._by_number: dict[str, list[dict]] = {}
        self._all: list[dict] = []
        # (name, mtime, size) per file, so an unchanged log is not re-parsed.
        self._stamps: dict[str, tuple[float, int]] = {}
        # Numbers merged into another profile. An alias map rather
        # than a log rewrite, so a merge is reversible and the log stays true.
        self._alias_path = log_dir / "number-aliases.json"
        self._aliases: dict[str, str] = {}
        # The owner's own replies, filed against the caller they were about.
        self._notes_path = log_dir / "caller-notes.jsonl"
        self._notes: dict[str, list[dict]] = {}
        self._notes_stamp: tuple[float, int] | None = None

    # -- merging -----------------------------------------------------------

    def _load_aliases(self) -> dict[str, str]:
        """number key -> the key it has been merged into.

        Self-references are dropped on read as well as on write, so a file
        written by an older build heals itself.
        """
        try:
            raw = json.loads(self._alias_path.read_text(encoding="utf-8"))
            return {
                str(k): str(v) for k, v in (raw or {}).items() if str(k) != str(v)
            }
        except (OSError, json.JSONDecodeError):
            return {}

    def _resolve_alias(self, key: str) -> str:
        """Follow the merge chain, defensively bounded against a cycle."""
        seen = {key}
        for _ in range(5):
            target = self._aliases.get(key)
            if not target or target in seen:
                break
            key = target
            seen.add(key)
        return key

    def merge(self, source: str, target: str) -> bool:
        """Fold one number's calls into another's profile.

        An alias rather than a rewrite: the call log keeps saying what actually
        happened, and the merge can be undone. Same principle as the contact-book
        fold, just chosen by hand rather than inferred.
        """
        source, target = source.strip(), target.strip()
        if not source or not target or source == target:
            return False
        aliases = self._load_aliases()
        aliases[source] = target
        # Anything already pointing at the source follows it, so a chain cannot
        # strand a profile behind two hops.
        for key, value in list(aliases.items()):
            if value == source:
                aliases[key] = target
        # Merging a pair one way and then the other makes that rule point the
        # target at itself. Harmless to resolve, but it shows up in the UI as a
        # profile listing its own number as merged in, with a Separate button.
        aliases = {k: v for k, v in aliases.items() if k != v}
        return self._write_aliases(aliases)

    def unmerge(self, source: str) -> bool:
        aliases = self._load_aliases()
        if aliases.pop(source, None) is None:
            return False
        return self._write_aliases(aliases)

    def merged_into(self, target: str) -> list[str]:
        """Numbers that have been folded into this profile."""
        self._aliases = self._load_aliases()
        return [k for k, v in self._aliases.items() if v == target]

    def _write_aliases(self, aliases: dict[str, str]) -> bool:
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            self._alias_path.write_text(
                json.dumps(aliases, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            log.error("could not save number aliases: %s", exc)
            return False
        self._aliases = aliases
        self.reload(force=True)
        self._notes_stamp = None
        return True

    # -- deleting ----------------------------------------------------------

    def _rewrite_calls(self, keep) -> int:
        """Rewrite every call log, keeping only records `keep(rec)` accepts.

        Returns how many were removed. Written to a temp file and moved into
        place, so an interrupted delete cannot truncate the log.
        """
        removed = 0
        for name in sorted(self._stamps) or [p.name for p in
                                             self._log_dir.glob("calls-*.jsonl")]:
            path = self._log_dir / name
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            kept = []
            dropped = 0
            for line in lines:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    # Unparseable lines are left alone rather than quietly
                    # discarded by an unrelated delete.
                    kept.append(line)
                    continue
                if keep(rec):
                    kept.append(line)
                else:
                    dropped += 1
            if not dropped:
                continue
            removed += dropped
            tmp = path.with_suffix(".jsonl.tmp")
            try:
                tmp.write_text(
                    "".join(l + "\n" for l in kept), encoding="utf-8"
                )
                tmp.replace(path)
            except OSError as exc:
                log.error("could not rewrite %s: %s", name, exc)
        if removed:
            self.reload(force=True)
        return removed

    def delete_call(self, call_sid: str) -> int:
        if not call_sid:
            return 0
        removed = self._rewrite_calls(lambda r: r.get("call_sid") != call_sid)
        log.info("deleted %d call record(s) by sid", removed)
        return removed

    def delete_calls_for(self, key: str) -> int:
        """Every call recorded against one number, including merged-in ones."""
        if not key:
            return 0
        removed = self._rewrite_calls(
            lambda r: self._key(r.get("from_number", "")) != key
        )
        log.info("deleted %d call record(s) for one caller", removed)
        return removed

    def delete_note(self, key: str, at: str) -> bool:
        """Remove one of the owner's notes, identified by its timestamp."""
        try:
            lines = self._notes_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        kept, hit = [], False
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)
                continue
            if entry.get("at") == at and self._key(entry.get("number", "")) == key:
                hit = True
                continue
            kept.append(line)
        if not hit:
            return False
        try:
            self._notes_path.write_text(
                "".join(l + "\n" for l in kept), encoding="utf-8"
            )
        except OSError as exc:
            log.error("could not rewrite notes: %s", exc)
            return False
        self._notes_stamp = None
        return True

    # -- the owner's notes -------------------------------------------------

    def add_note(self, number: str, text: str, call_sid: str = "") -> bool:
        """File something the owner said about a caller, so the next call knows it.

        Appended to its own file rather than the call log: these are not calls,
        and mixing them in would corrupt every count and summary that reads it.
        """
        key = self._key(number)
        text = " ".join(str(text or "").split())
        if not key or not text:
            return False
        entry = {
            "number": number,
            "at": datetime.now(timezone.utc).isoformat(),
            "text": text,
            "call_sid": call_sid,
        }
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            with self._notes_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.error("could not save caller note: %s", exc)
            return False
        # Force a re-read next time rather than trusting our own in-memory copy.
        self._notes_stamp = None
        log.info("noted a reply against %s", key[-4:].rjust(len(key), "*"))
        return True

    def _load_notes(self) -> None:
        try:
            stat = self._notes_path.stat()
        except OSError:
            self._notes = {}
            self._notes_stamp = None
            return
        stamp = (stat.st_mtime, stat.st_size)
        if stamp == self._notes_stamp:
            return

        notes: dict[str, list[dict]] = {}
        try:
            lines = self._notes_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = self._key(entry.get("number", ""))
            if key:
                notes.setdefault(key, []).append(entry)
        for entries in notes.values():
            entries.sort(key=lambda e: e.get("at", ""), reverse=True)
        self._notes = notes
        self._notes_stamp = stamp

    def notes_for(self, number: str, limit: int = 5) -> list[dict]:
        self._load_notes()
        key = self._key(number)
        if not key:
            return []
        return self._notes.get(key, [])[:limit]

    # -- loading -----------------------------------------------------------

    def _changed(self) -> bool:
        try:
            files = sorted(self._log_dir.glob("calls-*.jsonl"))
        except OSError:
            return False
        stamps = {}
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            stamps[path.name] = (stat.st_mtime, stat.st_size)
        if stamps == self._stamps:
            return False
        self._stamps = stamps
        return True

    def reload(self, force: bool = False) -> None:
        # Always run _changed(): it is what discovers the log files and fills in
        # self._stamps, so skipping it on a forced reload would index nothing.
        changed = self._changed()
        if not force and not changed:
            return
        self._aliases = self._load_aliases()

        by_number: dict[str, list[dict]] = {}
        every: list[dict] = []

        for name in sorted(self._stamps):
            path = self._log_dir / name
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                log.warning("could not read %s: %s", name, exc)
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    # A half-written final line is normal if we read mid-append.
                    continue
                every.append(rec)
                key = self._key(rec.get("from_number", ""))
                if key:
                    by_number.setdefault(key, []).append(rec)

        # Most recent first, which is the order both the agent and the UI want.
        for records in by_number.values():
            records.sort(key=lambda r: r.get("started_at", ""), reverse=True)
        every.sort(key=lambda r: r.get("started_at", ""), reverse=True)

        self._by_number = by_number
        self._all = every
        log.info("indexed %d calls from %d numbers", len(every), len(by_number))

    # -- queries -----------------------------------------------------------

    def key_for(self, number: str) -> str:
        """Public form of _key: which profile a number belongs to."""
        self.reload()
        return self._key(number)

    def numbers_for(self, key: str) -> list[str]:
        """Every number that reaches this profile - primary plus merged-in."""
        self.reload()
        # Deduplicated on the digits, not the string: the same number turns up
        # as "+447...", "447..." and again via the merge list, and listing one
        # number three times in three formats is worse than not listing it.
        out: list[str] = []
        seen: set[str] = set()

        def add(number: str) -> None:
            digits = normalise(number, self._country_code)
            if not digits or digits in seen:
                return
            seen.add(digits)
            out.append(number if number.startswith("+") else f"+{digits}")

        add(f"+{key}")
        for rec in self._by_number.get(key) or []:
            add(rec.get("from_number", ""))
        for merged in self.merged_into(key):
            add(f"+{merged}")
        return out

    def _key(self, number: str) -> str:
        """The profile a number belongs to.

        Three steps, in order: normalise the digits, fold a near-miss onto the
        saved contact it belongs to, then follow any merge made by hand.
        His merge is last because it must win over the inferred fold.
        """
        key = self._canonical(normalise(number, self._country_code))
        return self._resolve_alias(key) if self._aliases else key

    def for_number(self, number: str, limit: int = 3) -> list[dict]:
        """Previous calls from this number, most recent first."""
        self.reload()
        key = self._key(number)
        if not key:
            return []
        return self._by_number.get(key, [])[:limit]

    def recent(self, limit: int = 100) -> list[dict]:
        self.reload()
        return self._all[:limit]

    def callers(self) -> list[dict]:
        """One row per number, for the admin UI's overview."""
        self.reload()
        rows = []
        for key, records in self._by_number.items():
            latest = records[0]
            rows.append(
                {
                    "number": latest.get("from_number", "") or f"+{key}",
                    "key": key,
                    "calls": len(records),
                    "last_at": latest.get("started_at", ""),
                    "last_age": _humanise_age(latest.get("started_at", "")),
                    "known_contact_name": next(
                        (r.get("known_contact_name") for r in records
                         if r.get("known_contact_name")), ""
                    ),
                    "caller_name": next(
                        (r.get("caller_name") for r in records if r.get("caller_name")),
                        "",
                    ),
                    "last_category": latest.get("category", ""),
                    "last_summary": latest.get("summary", ""),
                }
            )
        rows.sort(key=lambda r: r["last_at"], reverse=True)
        return rows

    def stats(self) -> dict:
        self.reload()
        return {"calls": len(self._all), "numbers": len(self._by_number)}

    def profile(self, key: str) -> dict | None:
        """Everything known about one number, for the admin UI's caller page."""
        self.reload()
        records = self._by_number.get(key)
        if not records:
            return None

        latest = records[0]
        oldest = records[-1]

        # Categories they have rung about, commonest first - a quick read on
        # whether this is the plumber or the same PPI outfit for the ninth time.
        counts: dict[str, int] = {}
        for rec in records:
            cat = rec.get("category") or "unknown"
            counts[cat] = counts.get(cat, 0) + 1

        return {
            "key": key,
            "number": latest.get("from_number", "") or f"+{key}",
            "suggested_name": self.suggested_name(key),
            "company": next(
                (r.get("company_or_relationship") for r in records
                 if r.get("company_or_relationship")), ""
            ),
            "calls": records,
            "total": len(records),
            "first_at": oldest.get("started_at", ""),
            "first_age": _humanise_age(oldest.get("started_at", "")),
            "last_at": latest.get("started_at", ""),
            "last_age": _humanise_age(latest.get("started_at", "")),
            "notes": self.notes_for(latest.get("from_number", "") or f"+{key}", 20),
            "categories": sorted(counts.items(), key=lambda kv: -kv[1]),
            "flagged_urgent": any(r.get("flagged_urgent") for r in records),
            "spam": counts.get("spam_telesales", 0),
        }

    def suggested_name(self, key: str) -> str:
        """The best guess at who this number is, for pre-filling "save contact".

        Prefers a name they gave recently over one they gave once months ago, and
        falls back to a company name - "Kwik Fit" is a more useful contact entry
        than a bare number, even when nobody gave a personal name.
        """
        self.reload()
        records = self._by_number.get(key) or []
        for field_name in ("known_contact_name", "caller_name", "company_or_relationship"):
            for rec in records:
                value = str(rec.get(field_name) or "").strip()
                # The agent writes placeholders when a caller would not say.
                if value and value.lower() not in {
                    "unknown", "unnamed", "not given", "caller", "n/a", "none",
                    "anonymous", "withheld",
                }:
                    return value
        return ""

    # -- prompt ------------------------------------------------------------

    def prompt_section(self, number: str, limit: int = 3) -> str:
        """What the agent is told about this caller's previous calls.

        Deliberately short: a couple of lines each. The point is to stop it asking
        questions it already has answers to, not to hand it a case file to read
        out.
        """
        previous = self.for_number(number, limit)
        notes = self.notes_for(number)
        if not previous and not notes:
            return ""

        lines: list[str] = []
        if notes:
            # The owner's own words about this caller outrank anything else here, so
            # they go first - this is the one part of the context he wrote.
            lines += ["", "### What the owner has said about this caller", ""]
            for note in notes:
                age = _humanise_age(note.get("at", "")) or "previously"
                lines.append(f"- **{age}**: {note.get('text', '')}")
            lines += [
                "",
                "This is from the owner. Most of it is a record of something "
                "already settled or already passed on, so treat it as background "
                "you know rather than a task waiting to be done - do not open the "
                "call by announcing it, and do not repeat something they were told "
                "last time as though it were news.",
                "",
                "You may draw on it when it answers what they are ringing about. It "
                "is still a message you are relaying, never a commitment you make "
                "on his behalf, and it does not license anything else on the "
                "do-not-disclose list.",
            ]

        if not previous:
            return "\n".join(lines)

        lines += [
            "",
            f"### They have called before ({len(previous)} recent "
            f"{'call' if len(previous) == 1 else 'calls'})",
            "",
        ]
        for rec in previous:
            age = _humanise_age(rec.get("started_at", "")) or "previously"
            name = rec.get("caller_name") or rec.get("known_contact_name") or "unnamed"
            gist = (
                rec.get("summary")
                or rec.get("reason")
                or rec.get("requested_action")
                or "no message taken"
            )
            gist = " ".join(str(gist).split())
            if len(gist) > 220:
                gist = gist[:217] + "..."
            bits = [f"- **{age}** ({name}): {gist}"]
            if rec.get("category") and rec["category"] != "unknown":
                bits.append(f" [{rec['category']}]")
            lines.append("".join(bits))

        lines += [
            "",
            "Use this so you are not starting from nothing. Do not re-ask what you "
            "already know, and if this looks like the same matter as last time, say "
            "so naturally - \"is this about the MOT again?\" is what a person who "
            "remembered would say.",
            "",
            "If they have rung repeatedly about the same thing and nothing has "
            "happened, that is worth flagging as more urgent than the caller's own "
            "tone suggests - a third chase is a real signal.",
            "",
            "This is history, not identity. It still does not unlock anything on "
            "the do-not-disclose list.",
        ]
        return "\n".join(lines)

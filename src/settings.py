"""Runtime settings the admin UI is allowed to change.

Configuration comes from two places, and the split is deliberate:

- **`.env`** holds secrets and anything whose misuse costs money or safety:
  every token and key, `STREAM_TOKEN_SECRET`, `ADMIN_PASSWORD`,
  `CALLBACKS_ENABLED`, `TRANSFER_ENABLED`, `TRANSFER_TO_NUMBER`,
  `VALIDATE_TWILIO_SIGNATURE`, `RETAIN_TRANSCRIPTS`. None of it is reachable
  from the UI. The admin password is one lock on a port that is meant to be
  loopback-only; a stolen session that could retarget a transfer number or
  switch off signature checking would turn that into phone fraud.
- **`config/settings.json`** holds routing and behaviour: which channels are on,
  where they send, which call categories go where, and how the agent conducts a
  call. Losing control of these is annoying, not dangerous.

Every key here falls back to its environment variable, so a deployment with no
`settings.json` behaves exactly as it did before this file existed, and a
headless install can still be configured entirely through the environment.

The file is re-read when its mtime changes, the same way the contact book and
the call history already work, so a change in the UI applies to the next call
without a restart.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Behaviour settings the UI may write. The value is the type to coerce to; the
# default always comes from the matching Config attribute. Anything not listed
# here is env-only by design - read the module docstring before adding to it.
BEHAVIOUR_KEYS: dict[str, type] = {
    "greeting": str,
    "openai_voice": str,
    "vad_eagerness": str,
    "wrap_up_after_s": int,
    "wrap_up_after_turns": int,
    "history_enabled": bool,
    "history_max_calls": int,
    "max_call_seconds": int,
    "silence_hangup_seconds": int,
}

CHANNEL_NAMES = ("telegram", "email", "webhook", "whatsapp")

# Call categories as classified by the agent. "default" catches anything not
# listed, including any category added later.
ROUTING_CATEGORIES = (
    "spam_telesales",
    "tradesperson_admin",
    "delivery_appointment",
    "recruiter_job_business",
    "family_friend_personal",
    "urgent",
    "unknown",
)


class Settings:
    """The writable half of the configuration, reloaded when it changes."""

    def __init__(self, path: Path):
        self._path = path
        self._data: dict[str, Any] = {}
        self._mtime: float | None = None
        self.reload()

    # -- persistence -------------------------------------------------------

    def reload(self) -> None:
        try:
            stat = self._path.stat()
        except OSError:
            # No settings file is the normal state for a fresh or env-only
            # install, so this is not worth a log line.
            self._data = {}
            self._mtime = None
            return

        if self._mtime == stat.st_mtime:
            return

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Keep whatever we already had rather than silently reverting every
            # setting to its env default because of one malformed write.
            log.error("could not read settings: %s", exc)
            return

        self._data = raw if isinstance(raw, dict) else {}
        self._mtime = stat.st_mtime
        log.info("settings reloaded from %s", self._path.name)

    def save(self, data: dict) -> bool:
        """Write settings atomically. False if the volume is read-only."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Temp file plus atomic replace, so an interrupted write cannot
            # leave a half-written file that fails to parse on the next call.
            fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, self._path)
        except OSError as exc:
            # The likely cause is ./config still being mounted read-only.
            log.error("could not write settings: %s", exc)
            return False

        self._data = data
        try:
            self._mtime = self._path.stat().st_mtime
        except OSError:
            self._mtime = None
        return True

    # -- reads -------------------------------------------------------------

    def raw(self) -> dict:
        """A deep copy the caller may edit before handing back to save()."""
        self.reload()
        return json.loads(json.dumps(self._data))

    def behaviour(self, key: str, default):
        self.reload()
        value = (self._data.get("behaviour") or {}).get(key)
        if value is None:
            return default
        want = BEHAVIOUR_KEYS.get(key)
        try:
            if want is bool:
                return bool(value)
            if want is int:
                return int(value)
            if want is str:
                return str(value)
        except (TypeError, ValueError):
            log.warning("settings: %s is not a valid %s, using the default", key, want)
            return default
        return value

    def channel(self, name: str) -> dict:
        self.reload()
        entry = (self._data.get("channels") or {}).get(name)
        return dict(entry) if isinstance(entry, dict) else {}

    def routing(self, category: str) -> list[str] | None:
        """Channels for a category, or None when no routing is configured.

        An explicitly empty list is meaningful and different from None: it is
        how "never tell me about spam" is expressed.
        """
        self.reload()
        table = self._data.get("routing")
        if not isinstance(table, dict):
            return None
        for key in (category, "default"):
            value = table.get(key)
            if isinstance(value, list):
                return [str(v) for v in value if str(v) in CHANNEL_NAMES]
        return None


class LiveConfig:
    """The frozen env config, with UI-editable keys read live on each access.

    Every existing call site does `config.some_attribute`, so this stands in for
    the old module-level dataclass and forwards everything it does not override.
    That keeps the change to this file rather than spreading a settings lookup
    across twenty call sites - and it means values that are already read once
    per call (`realtime.py` reads `self._cfg.greeting` when it answers) become
    live for free.
    """

    def __init__(self, base, settings: Settings):
        # Bypass __setattr__ so these two do not recurse through __getattr__.
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_settings", settings)

    @property
    def settings(self) -> Settings:
        return object.__getattribute__(self, "_settings")

    @property
    def base(self):
        return object.__getattribute__(self, "_base")

    def __getattr__(self, name: str):
        base = object.__getattribute__(self, "_base")
        value = getattr(base, name)
        if name in BEHAVIOUR_KEYS:
            return object.__getattribute__(self, "_settings").behaviour(name, value)
        return value

    def __setattr__(self, name: str, value) -> None:
        raise AttributeError("config is read-only; write settings via Settings.save()")

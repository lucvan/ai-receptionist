"""Runtime settings the admin UI is allowed to change.

Configuration comes from three places, and the split is deliberate:

- **`.env`** holds anything whose misuse changes the *security posture* rather
  than a destination: `ADMIN_PASSWORD`, `VALIDATE_TWILIO_SIGNATURE`,
  `TRANSFER_ENABLED`, `TRANSFER_TO_NUMBER`, `CALLBACKS_ENABLED`,
  `RETAIN_TRANSCRIPTS`. None of it is reachable from the UI. A stolen session
  that could retarget a transfer number or switch off signature checking would
  turn that into phone fraud.
- **`config/settings.json`** holds routing and behaviour: which channels are on,
  where they send, which call categories go where, and how the agent conducts a
  call. Losing control of these is annoying, not dangerous. **No secrets** - this
  file stays safe to read, diff and paste into a bug report.
- **`config/secrets.json`** holds credentials the setup wizard writes: the voice
  provider key, Twilio, one per notification channel. Write-only from the UI -
  a value that goes in never comes back out. See `secrets_store.py` for the full
  argument, including why the list above stayed in `.env`.

Precedence is the same for both overlay files: a value set in the UI wins over
the environment, and clearing it falls back. So a deployment configured entirely
through `.env` behaves exactly as it did before either file existed, and a
headless install never needs the UI.

Both files are re-read when their mtime changes, the same way the contact book
and the call history already work, so a change in the UI applies to the next call
without a restart.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from .catalogs import OPENAI_VOICES
from .secrets_store import SECRET_KEYS

log = logging.getLogger(__name__)

# Values offered as a dropdown rather than a text box, so a typo cannot reach a
# live call. Anything with a knowably finite set belongs here: the cost of a bad
# value is a caller hearing silence, and the cost of listing them is one line.
#
# `openai_voice` is not free text at the API either - an unknown name is rejected
# and the call fails to configure. `vad_eagerness` silently falls back. Both are
# better as a list.
CHOICES: dict[str, list[tuple[str, str]]] = {
    "voice_provider": [
        ("openai", "OpenAI Realtime"),
        ("elevenlabs", "ElevenLabs Agents"),
    ],
    # OpenAI publishes its voice list in documentation and nowhere
    # machine-readable, so this one genuinely has to be maintained by hand. It is
    # the only dropdown in the wizard that is not read from the live account -
    # see `catalogs.py`, which does the rest.
    "openai_voice": [(v, v.capitalize()) for v in OPENAI_VOICES],
    # A fallback for the pre-key state only. Once a key is saved the wizard lists
    # what the account actually has, which is how it should be: this literal was
    # already two models out of date against a real account the day it was
    # written.
    "openai_realtime_model": [
        ("gpt-realtime-mini", "gpt-realtime-mini (cheaper)"),
        ("gpt-realtime", "gpt-realtime (better)"),
    ],
    "vad_eagerness": [
        ("low", "Low - waits longer, interrupts less"),
        ("auto", "Auto"),
        ("high", "High - replies faster"),
    ],
    "elevenlabs_language": [
        ("en", "English"), ("de", "German"), ("es", "Spanish"),
        ("fr", "French"), ("it", "Italian"), ("nl", "Dutch"),
        ("pl", "Polish"), ("pt", "Portuguese"),
    ],
    "whatsapp_flavour": [("waha", "WAHA"), ("evolution", "Evolution API")],
}

# Behaviour settings the UI may write. The value is the type to coerce to; the
# default always comes from the matching Config attribute. Anything not listed
# here is env-only by design - read the module docstring before adding to it.
BEHAVIOUR_KEYS: dict[str, type] = {
    "greeting": str,
    "openai_voice": str,
    "openai_realtime_model": str,
    "vad_eagerness": str,
    # ElevenLabs equivalents. The rest of what shapes a call there - turn-taking,
    # the LLM, the system voice settings - belongs to the agent object and is
    # edited in the ElevenLabs dashboard.
    "elevenlabs_voice_id": str,
    "elevenlabs_language": str,
    # The agent id *is* UI-writable, unlike the transfer number it was previously
    # compared to. The difference is what a wrong value does: a bad agent id
    # fails loudly on the next call and is fixed by picking another from the
    # dropdown, where a retargeted transfer number silently connects a caller to
    # a stranger. The wizard has to be able to write it or it cannot finish.
    "elevenlabs_agent_id": str,
    # Which provider answers the phone. Env-only until the wizard existed, on the
    # grounds that a mid-flight swap would leave the next caller talking to
    # whichever one happened to be configured. That is still true, so the UI
    # refuses to select a provider whose credentials are not in place - see
    # `_provider_ready` in admin.py. Being unable to finish setup without editing
    # a dotfile is the worse failure.
    "voice_provider": str,
    # Picked from the numbers on the Twilio account, never typed: it is the
    # caller ID for callbacks and transfers, and E.164 is a format people get
    # wrong in several different ways. The wizard validates it against the live
    # account before writing it.
    "twilio_phone_number": str,
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

    def __init__(self, base, settings: Settings, secrets=None):
        # Bypass __setattr__ so these do not recurse through __getattr__.
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_settings", settings)
        object.__setattr__(self, "_secrets", secrets)

    @property
    def settings(self) -> Settings:
        return object.__getattribute__(self, "_settings")

    @property
    def secrets(self):
        return object.__getattribute__(self, "_secrets")

    @property
    def base(self):
        return object.__getattribute__(self, "_base")

    def __getattr__(self, name: str):
        base = object.__getattribute__(self, "_base")
        value = getattr(base, name)
        if name in BEHAVIOUR_KEYS:
            return object.__getattribute__(self, "_settings").behaviour(name, value)

        store = object.__getattribute__(self, "_secrets")
        if store is not None and name in SECRET_KEYS:
            # Only when something is actually stored. An absent entry falls
            # through to the environment, so adding this file cannot break an
            # install that was configured entirely through `.env`.
            stored = store.get(name)
            if stored:
                return stored
        return value

    def __setattr__(self, name: str, value) -> None:
        raise AttributeError("config is read-only; write settings via Settings.save()")

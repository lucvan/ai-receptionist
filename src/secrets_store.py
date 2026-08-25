"""Credentials the admin UI is allowed to set, and never allowed to read back.

## Why this file exists at all

`settings.py` draws a hard line: secrets live in `.env` and nothing in the UI can
touch them. That line was drawn for a good reason, quoted here so it is not lost:
*a stolen admin session that could retarget a transfer number or switch off
signature checking would turn that into phone fraud.*

That reasoning is still right, and this file does not overturn it. It moves the
line rather than erasing it, because "clone the repo and hand-edit a dotfile
before anything works" is its own kind of failure - the setup that never gets
finished, or gets finished wrongly with a key pasted into the wrong variable.

## Where the line is now

**Writable from the UI:** credentials for *outbound* services - the voice
provider, the Twilio API, one per notification channel. The worst a stolen
session can do with these is point the service at an attacker's account, which
breaks it loudly and immediately.

**Still env-only, deliberately:** anything that changes the security posture
rather than a destination.

| env-only | why |
|---|---|
| `ADMIN_PASSWORD` | the lock on this UI; a session must not be able to change its own lock |
| `VALIDATE_TWILIO_SIGNATURE` | turning it off opens the webhook to anyone |
| `TRANSFER_ENABLED` / `TRANSFER_TO_NUMBER` | retargeting where a caller is patched through is the phone-fraud case above |
| `CALLBACKS_ENABLED` | spends money and rings real people |
| `RETAIN_TRANSCRIPTS` | a data-retention policy decision, not a setting |
| `STREAM_TOKEN_SECRET` | *settable* here, but only by generating one - see below |

## Write-only, which is the whole mitigation

A value that goes in never comes back out. `get()` is for the service; the UI has
only `has()`, which returns a boolean. So a stolen session can *replace* a
credential - noisy, and recoverable by setting it again - but cannot *read* one,
which would be silent and permanent. The existing test that the settings page
never renders a secret still holds, and now covers this file too.

`STREAM_TOKEN_SECRET` is a special case: the UI can generate one, but there is no
field to type one into. Nobody should be inventing that value by hand, and a
weak one silently weakens the media-socket authentication described in the README.
"""

from __future__ import annotations

import json
import logging
import os
import secrets as _secrets
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# Config attribute name -> the environment variable it shadows. The env var name
# is what the UI shows in its "set / missing" pill, so a deployer reading the
# page can find the same thing in `.env.example`.
SECRET_KEYS: dict[str, str] = {
    "openai_api_key": "OPENAI_API_KEY",
    "elevenlabs_api_key": "ELEVENLABS_API_KEY",
    "twilio_auth_token": "TWILIO_AUTH_TOKEN",
    "twilio_account_sid": "TWILIO_ACCOUNT_SID",
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
    "smtp_password": "SMTP_PASSWORD",
    "webhook_auth_header": "WEBHOOK_AUTH_HEADER",
    "whatsapp_bridge_key": "WHATSAPP_BRIDGE_KEY",
    "supervisor_key": "SUPERVISOR_KEY",
    # Generated, never typed. See the module docstring.
    "stream_secret": "STREAM_TOKEN_SECRET",
}


class SecretStore:
    """The UI-writable credential overlay. Reads live; writes atomically."""

    def __init__(self, path: Path):
        self._path = path
        self._data: dict[str, str] = {}
        self._mtime: float | None = None
        self.reload()

    # -- persistence -------------------------------------------------------

    def reload(self) -> None:
        try:
            stat = self._path.stat()
        except OSError:
            # No file is the normal state for an env-only install.
            self._data = {}
            self._mtime = None
            return

        if self._mtime == stat.st_mtime:
            return

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Keep whatever we had rather than dropping every credential over one
            # malformed write - that would take the service off the air.
            log.error("could not read secrets: %s", exc)
            return

        self._data = (
            {k: str(v) for k, v in raw.items() if isinstance(v, (str, int))}
            if isinstance(raw, dict)
            else {}
        )
        self._mtime = stat.st_mtime
        log.info("credential overlay reloaded (%d set)", len(self._data))

    def _save(self) -> bool:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2)
                fh.write("\n")
            # Before the rename, so the file is never briefly world-readable.
            # A no-op on Windows, which is fine: the container is where this
            # matters, and there it is a real restriction.
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self._path)
        except OSError as exc:
            log.error("could not write secrets: %s", exc)
            return False

        try:
            self._mtime = self._path.stat().st_mtime
        except OSError:
            self._mtime = None
        return True

    # -- reads (service only) ----------------------------------------------

    def get(self, key: str) -> str:
        """The stored value, or "" if this key is not overridden here.

        For the service. The UI must use `has()` - see the module docstring.
        """
        self.reload()
        return self._data.get(key, "")

    def has(self, key: str) -> bool:
        """Whether a value is stored. The only read the UI is given."""
        self.reload()
        return bool(self._data.get(key))

    def stored_keys(self) -> set[str]:
        self.reload()
        return {k for k, v in self._data.items() if v}

    # -- writes ------------------------------------------------------------

    def put(self, key: str, value: str) -> bool:
        """Store one credential. An empty value clears it, falling back to env."""
        if key not in SECRET_KEYS:
            log.warning("refusing to store unknown secret %r", key)
            return False
        self.reload()
        value = value.strip()
        if value:
            self._data[key] = value
        else:
            self._data.pop(key, None)
        return self._save()

    def put_many(self, values: dict[str, str]) -> bool:
        """Store several at once.

        A blank field means "leave alone", NOT "clear" - the UI never renders a
        stored value, so every field it draws starts empty, and treating that as
        a deletion would wipe a working credential on every save. Clearing is a
        separate, explicit action.
        """
        self.reload()
        changed = False
        for key, value in values.items():
            if key not in SECRET_KEYS:
                log.warning("refusing to store unknown secret %r", key)
                continue
            value = (value or "").strip()
            if not value:
                continue
            self._data[key] = value
            changed = True
        return self._save() if changed else True

    def clear(self, key: str) -> bool:
        self.reload()
        if self._data.pop(key, None) is None:
            return True
        return self._save()

    def generate_stream_secret(self) -> bool:
        """Mint a fresh media-socket signing key.

        Generated rather than typed: this value is what binds a media WebSocket
        to the call that created it, and a memorable one would quietly weaken
        that to nothing.
        """
        self.reload()
        self._data["stream_secret"] = _secrets.token_hex(32)
        return self._save()

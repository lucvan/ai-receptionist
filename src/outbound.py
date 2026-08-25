"""Placing a callback.

This is the one part of the service that reaches out and rings a real person, so
it is the one part where a bug costs money and annoys someone who did nothing
wrong. The guards are therefore deliberate rather than defensive garnish:

- **Only numbers that rang us.** A callback target must come from the pending-call
  store, so a bug or an injected string cannot dial an arbitrary number.
- **Cooldown per number**, so a double-tap in Telegram cannot ring someone twice.
- **An hourly ceiling**, so a loop anywhere upstream cannot dial a hundred times.

The TwiML for the call is not inline: Twilio fetches it from `/outbound-call`,
which mints and verifies the same per-call stream token the inbound path uses.
"""

from __future__ import annotations

import logging
import time
from collections import deque

import httpx

log = logging.getLogger(__name__)

TWILIO_API = "https://api.twilio.com/2010-04-01"


class OutboundCaller:
    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        public_base_url: str,
        cooldown_s: int = 300,
        max_per_hour: int = 6,
    ):
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._from = from_number
        self._base = public_base_url.rstrip("/")
        self._cooldown_s = cooldown_s
        self._max_per_hour = max_per_hour

        self._last_called: dict[str, float] = {}
        self._recent: deque[float] = deque()

    @property
    def enabled(self) -> bool:
        return bool(self._account_sid and self._auth_token and self._from and self._base)

    def _rate_check(self, number: str) -> str | None:
        """Return a human-readable refusal, or None if the call may proceed."""
        now = time.time()

        while self._recent and now - self._recent[0] > 3600:
            self._recent.popleft()
        if len(self._recent) >= self._max_per_hour:
            return f"already placed {len(self._recent)} callbacks in the last hour"

        last = self._last_called.get(number)
        if last and now - last < self._cooldown_s:
            wait = int(self._cooldown_s - (now - last))
            return f"called that number {int(now - last)}s ago - waiting {wait}s"

        return None

    async def place_call(self, to_number: str, token: str) -> tuple[bool, str]:
        """Ring a number. Returns (ok, human-readable detail)."""
        if not self.enabled:
            return False, "outbound calling is not configured"

        refusal = self._rate_check(to_number)
        if refusal:
            log.warning("outbound call refused: %s", refusal)
            return False, refusal

        url = f"{self._base}/outbound-call?token={token}"
        data = {
            "To": to_number,
            "From": self._from,
            "Url": url,
            "Method": "POST",
            # If they do not pick up, do not leave the line hanging. Twilio's own
            # machine detection is not needed here - the agent handles voicemail
            # badly either way, and a short timeout keeps cost predictable.
            "Timeout": "25",
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{TWILIO_API}/Accounts/{self._account_sid}/Calls.json",
                    data=data,
                    auth=(self._account_sid, self._auth_token),
                )
        except Exception as exc:  # noqa: BLE001
            log.error("outbound call failed: %s", type(exc).__name__)
            return False, f"could not reach Twilio ({type(exc).__name__})"

        if resp.status_code not in (200, 201):
            # Twilio's error messages are safe to relay and genuinely useful
            # ("not a valid phone number", "geo permissions"), but never echo the
            # whole body, which repeats the request including credentials context.
            try:
                message = resp.json().get("message", "")[:200]
            except ValueError:
                message = ""
            log.error("outbound call rejected: HTTP %s %s", resp.status_code, message)
            return False, message or f"Twilio returned HTTP {resp.status_code}"

        now = time.time()
        self._last_called[to_number] = now
        self._recent.append(now)

        sid = ""
        try:
            sid = resp.json().get("sid", "")
        except ValueError:
            pass
        log.info("placed outbound call %s", sid)
        return True, sid

    async def redirect_call(self, call_sid: str, url: str) -> tuple[bool, str]:
        """Point a live call at new TwiML.

        Used to hand a screened caller over to <Dial>. No rate limiting here: this
        redirects a call already in progress rather than originating a new one, and
        the caller is on the line waiting.
        """
        if not (self._account_sid and self._auth_token):
            return False, "Twilio is not configured"
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    f"{TWILIO_API}/Accounts/{self._account_sid}/Calls/{call_sid}.json",
                    data={"Url": url, "Method": "POST"},
                    auth=(self._account_sid, self._auth_token),
                )
        except Exception as exc:  # noqa: BLE001
            return False, type(exc).__name__
        if resp.status_code not in (200, 201):
            try:
                return False, resp.json().get("message", "")[:200]
            except ValueError:
                return False, f"HTTP {resp.status_code}"
        return True, ""

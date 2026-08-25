"""Sending WhatsApp through a self-hosted bridge you pair from the admin UI.

There is no official WhatsApp API that will let a service send arbitrary prose to
its owner. The Business Platform requires a pre-approved template for anything
business-initiated outside a 24-hour window opened by the *user*, and template
parameters cannot carry newlines - which a call summary does, deliberately, so
the callback number lands on its own tappable line. A summary is exactly the
shape of message the template system exists to prevent.

So this talks to a bridge instead: one of the self-hosted WhatsApp Web gateways
(WAHA, Evolution API, or anything speaking a similar shape), running as a
separate service and paired by scanning a QR code from the settings page.

## Pair a dedicated number, not your personal account

The bridge is logged in as a real WhatsApp account and can message anyone that
account can reach. Pointing it at the owner's personal account would make this
container's worst case "can impersonate you to all your contacts", which is a
far larger credential than the outbound-only Telegram bot it sits beside.

Pair a second number. The receptionist only ever needs to message one person.

## On these adapters

The bridges are third-party projects with moving APIs, and the two mappings
below are best-effort against their current shapes. `custom` exists because
that will eventually be wrong for somebody: it takes a URL and a JSON body
template with `{to}` and `{text}` placeholders and posts exactly that, which
works against any bridge, including one written in an afternoon.
"""

from __future__ import annotations

import base64
import json
import logging
import re

import httpx

log = logging.getLogger(__name__)

FLAVOURS = ("waha", "evolution", "custom")

# Bridges want bare international digits; some also want a WhatsApp JID suffix.
_DIGITS = re.compile(r"[^\d]")


def to_digits(number: str) -> str:
    return _DIGITS.sub("", number or "")


class WhatsAppBridge:
    """A thin client over whichever bridge is configured.

    Every method degrades to a reported failure rather than raising, because all
    three are reachable from a web request handler and a bridge that is down
    must not take the settings page with it.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        flavour: str = "waha",
        session: str = "default",
        custom_send_path: str = "",
        custom_body: str = "",
    ):
        self._base = (base_url or "").rstrip("/")
        self._key = api_key or ""
        self._flavour = flavour if flavour in FLAVOURS else "waha"
        self._session = session or "default"
        self._custom_path = custom_send_path or ""
        self._custom_body = custom_body or ""

    @property
    def configured(self) -> bool:
        return bool(self._base)

    @property
    def flavour(self) -> str:
        return self._flavour

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if not self._key:
            return headers
        # Each bridge names its own auth header. Never log either.
        if self._flavour == "evolution":
            headers["apikey"] = self._key
        else:
            headers["X-Api-Key"] = self._key
        return headers

    # -- sending -----------------------------------------------------------

    def _send_request(self, to: str, text: str) -> tuple[str, dict]:
        """(url, json body) for the configured flavour."""
        digits = to_digits(to)

        if self._flavour == "evolution":
            return (
                f"{self._base}/message/sendText/{self._session}",
                {"number": digits, "text": text},
            )

        if self._flavour == "custom":
            path = self._custom_path or "/send"
            url = path if path.startswith("http") else f"{self._base}{path}"
            template = self._custom_body or '{"to": "{to}", "text": "{text}"}'
            # json.dumps then strip the quotes, so newlines and quotes in the
            # summary are escaped correctly rather than breaking the JSON.
            filled = template.replace("{to}", digits).replace(
                "{text}", json.dumps(text)[1:-1]
            )
            try:
                return url, json.loads(filled)
            except json.JSONDecodeError:
                log.error("whatsapp custom body template is not valid JSON")
                raise ValueError("custom body template is not valid JSON")

        # WAHA
        return (
            f"{self._base}/api/sendText",
            {"session": self._session, "chatId": f"{digits}@c.us", "text": text},
        )

    async def send(self, to: str, text: str, timeout: float = 20.0) -> tuple[bool, str]:
        if not self.configured:
            return False, "no bridge URL configured"
        if not to_digits(to):
            return False, "no recipient number configured"

        try:
            url, body = self._send_request(to, text)
        except ValueError as exc:
            return False, str(exc)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=self._headers(), json=body)
        except Exception as exc:  # noqa: BLE001
            log.error("whatsapp send failed: %s", type(exc).__name__)
            return False, f"could not reach the bridge ({type(exc).__name__})"

        if resp.status_code in (200, 201, 202):
            return True, ""

        # Bridge error bodies are useful ("session not connected") and do not
        # echo the api key, which travels in a header rather than the body.
        detail = ""
        try:
            detail = str(resp.json())[:200]
        except ValueError:
            detail = resp.text[:200]
        log.error("whatsapp send rejected: HTTP %s", resp.status_code)
        return False, detail or f"bridge returned HTTP {resp.status_code}"

    # -- pairing -----------------------------------------------------------

    async def status(self, timeout: float = 10.0) -> tuple[str, str]:
        """(state, detail) where state is connected | pairing | offline | unknown."""
        if not self.configured:
            return "offline", "no bridge URL configured"

        if self._flavour == "evolution":
            url = f"{self._base}/instance/connectionState/{self._session}"
        elif self._flavour == "custom":
            # Nothing to probe generically; a send is the only real test.
            return "unknown", "custom bridge - use Send test to check"
        else:
            url = f"{self._base}/api/sessions/{self._session}"

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, headers=self._headers())
        except Exception as exc:  # noqa: BLE001
            return "offline", f"could not reach the bridge ({type(exc).__name__})"

        if resp.status_code == 404:
            return "offline", "no session on the bridge yet - press Pair"
        if resp.status_code != 200:
            return "offline", f"bridge returned HTTP {resp.status_code}"

        try:
            data = resp.json()
        except ValueError:
            return "unknown", "bridge returned a non-JSON status"

        if self._flavour == "evolution":
            state = str((data.get("instance") or {}).get("state") or "")
            if state == "open":
                return "connected", "paired"
            if state in ("connecting", "close"):
                return "pairing", state
            return "unknown", state or "no state reported"

        status = str(data.get("status") or "")
        if status == "WORKING":
            return "connected", "paired"
        if status in ("SCAN_QR_CODE", "STARTING"):
            return "pairing", status
        if status in ("STOPPED", "FAILED"):
            return "offline", status
        return "unknown", status or "no status reported"

    async def start(self, timeout: float = 15.0) -> tuple[bool, str]:
        """Ask the bridge to bring the session up so a QR can be issued."""
        if not self.configured:
            return False, "no bridge URL configured"

        if self._flavour == "evolution":
            # Evolution's connect endpoint both starts the session and returns
            # the QR, so starting is the same call as fetching.
            return True, ""
        if self._flavour == "custom":
            return False, "pairing a custom bridge is done on the bridge itself"

        url = f"{self._base}/api/sessions/{self._session}/start"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=self._headers(), json={})
        except Exception as exc:  # noqa: BLE001
            return False, f"could not reach the bridge ({type(exc).__name__})"

        # 422 here usually means "already started", which is not a failure.
        if resp.status_code in (200, 201, 202, 422):
            return True, ""
        return False, f"bridge returned HTTP {resp.status_code}"

    async def qr(self, timeout: float = 15.0) -> tuple[str, str]:
        """(data-url, error). The data URL goes straight into an <img src>."""
        if not self.configured:
            return "", "no bridge URL configured"
        if self._flavour == "custom":
            return "", "pairing a custom bridge is done on the bridge itself"

        if self._flavour == "evolution":
            url = f"{self._base}/instance/connect/{self._session}"
        else:
            url = f"{self._base}/api/{self._session}/auth/qr?format=image"

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, headers=self._headers())
        except Exception as exc:  # noqa: BLE001
            return "", f"could not reach the bridge ({type(exc).__name__})"

        if resp.status_code != 200:
            return "", f"bridge returned HTTP {resp.status_code}"

        content_type = resp.headers.get("content-type", "")
        if content_type.startswith("image/"):
            encoded = base64.b64encode(resp.content).decode()
            return f"data:{content_type.split(';')[0]};base64,{encoded}", ""

        try:
            data = resp.json()
        except ValueError:
            return "", "bridge did not return an image or JSON"

        # Evolution returns {"base64": "data:image/png;base64,..."}; some builds
        # omit the data: prefix, and some return only a pairing code.
        raw = data.get("base64") or data.get("qr") or data.get("code") or ""
        if not isinstance(raw, str) or not raw:
            return "", "bridge returned no QR code"
        if raw.startswith("data:"):
            return raw, ""
        if len(raw) > 120:  # long enough to be base64 image data, not a code
            return f"data:image/png;base64,{raw}", ""
        return "", f"bridge returned a pairing code rather than a QR: {raw}"

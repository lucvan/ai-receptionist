"""Authenticating that a request really came from Twilio.

Two separate mechanisms, because Twilio only signs one of the two hops:

1. ``POST /incoming-call`` carries an ``X-Twilio-Signature`` header. Twilio computes
   HMAC-SHA1 over the full request URL with the POST parameters appended in
   alphabetical order, keyed on the account auth token.
2. The media WebSocket upgrade is **not** signed by Twilio. So ``/incoming-call``
   mints a short-lived token bound to the CallSid, embeds it in the TwiML
   ``<Stream>`` element as a custom parameter, and ``/media-stream`` verifies it.
   Without this, anyone who learns the public hostname could open a media socket
   and burn OpenAI credit.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from urllib.parse import urlparse, urlunparse


def validate_twilio_signature(
    auth_token: str, signature: str, url: str, params: dict[str, str]
) -> bool:
    """Verify Twilio's X-Twilio-Signature over a form-encoded POST."""
    if not auth_token or not signature:
        return False

    payload = url
    for key in sorted(params):
        payload += key + params[key]

    digest = hmac.new(
        auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def public_request_url(public_base_url: str, path: str, query: str = "") -> str:
    """Rebuild the URL Twilio signed.

    Twilio signs the URL *it* dialled, which is the public one. Behind the Synology
    reverse proxy the app sees an internal host and http scheme, so reconstructing
    from the request would produce a different string and every signature would
    fail. Always rebuild from PUBLIC_BASE_URL.
    """
    parsed = urlparse(public_base_url)
    return urlunparse((parsed.scheme, parsed.netloc, path, "", query, ""))


def mint_stream_token(secret: str, call_sid: str, issued_at: int | None = None) -> str:
    """Create a ``<issued_at>.<mac>`` token binding a media stream to one call."""
    issued_at = int(time.time()) if issued_at is None else issued_at
    message = f"{call_sid}.{issued_at}".encode("utf-8")
    mac = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"{issued_at}.{mac}"


def verify_stream_token(secret: str, call_sid: str, token: str, ttl_s: int) -> bool:
    if not secret or not call_sid or not token:
        return False
    try:
        issued_raw, mac = token.split(".", 1)
        issued_at = int(issued_raw)
    except (ValueError, AttributeError):
        return False

    age = time.time() - issued_at
    # Reject future-dated tokens too - a clock-skewed or forged issue time
    # should not buy an attacker a longer window.
    if age < -30 or age > ttl_s:
        return False

    expected = mint_stream_token(secret, call_sid, issued_at)
    return hmac.compare_digest(expected.split(".", 1)[1], mac)

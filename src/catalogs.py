"""Asking each provider what it actually offers, instead of hardcoding a guess.

Every dropdown in the setup wizard would rather be built from the account it is
configured against than from a list written here. A hardcoded list is wrong the
moment the vendor ships something, and it is wrong *silently* - the option simply
is not there, and nobody knows what they are missing.

That is not a hypothetical. The first version of this wizard offered two OpenAI
realtime models from a literal in `settings.CHOICES`. The account it was built
against had ten, including two newer than either of the hardcoded pair.

## What can and cannot be queried

| dropdown | source |
|---|---|
| ElevenLabs voices | live, `/v1/voices` (see `elevenlabs_provision.py`) |
| ElevenLabs agents | live, `/v1/convai/agents` |
| OpenAI realtime models | live, `/v1/models` filtered for realtime |
| Twilio phone numbers | live, `/2010-04-01/.../IncomingPhoneNumbers.json` |
| **OpenAI voices** | **static** - there is no endpoint that lists them |

The last row is the honest exception, and the UI says so rather than implying the
list is authoritative.

Fetching the list doubles as a credential check, which is the other reason to
prefer it: a key that cannot list models cannot answer a call either, and finding
that out on the settings page beats finding out from a caller.

**Stdlib only**, matching `elevenlabs_provision.py` - the CLI has to run before
anything is installed, and the UI calls these from a thread.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

OPENAI_API = "https://api.openai.com"
TWILIO_API = "https://api.twilio.com"

# Realtime models that exist but are not conversational agents. Excluded by name
# rather than by an allowlist: a denylist of two lets a genuinely new model
# through, where an allowlist would repeat the mistake this module exists to fix.
NOT_CONVERSATIONAL = ("-whisper", "-translate")

# OpenAI publishes these in its docs and nowhere machine-readable. Static, and
# labelled as such in the UI - see the module docstring.
OPENAI_VOICES = (
    "alloy", "ash", "ballad", "coral", "echo",
    "sage", "shimmer", "verse", "marin", "cedar",
)


def _get(url: str, headers: dict[str, str], timeout: int = 20) -> tuple[int, str]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    except Exception as exc:  # noqa: BLE001 - network, DNS, TLS, all the same here
        log.warning("catalogue fetch failed for %s: %s", url, type(exc).__name__)
        return 0, str(exc)


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


def openai_realtime_models(key: str) -> list[str]:
    """Realtime-capable model ids on this account, best first. Never raises.

    Sorted so the plain names come before dated snapshots: `gpt-realtime-mini` is
    what most people want, and `gpt-realtime-mini-2025-12-15` is a pin for
    somebody who already knows why they need it.
    """
    if not key.strip():
        return []
    status, body = _get(
        f"{OPENAI_API}/v1/models", {"Authorization": f"Bearer {key}"}
    )
    if status != 200:
        log.info("could not list OpenAI models (HTTP %s)", status)
        return []

    try:
        data = json.loads(body).get("data", [])
    except json.JSONDecodeError:
        return []

    models = [
        m["id"]
        for m in data
        if "realtime" in m.get("id", "")
        and not any(bad in m["id"] for bad in NOT_CONVERSATIONAL)
    ]

    def rank(model_id: str) -> tuple:
        # A trailing date makes it a pinned snapshot; those go last.
        dated = any(part.isdigit() and len(part) == 4 for part in model_id.split("-"))
        return (dated, "mini" not in model_id, model_id)

    return sorted(models, key=rank)


def openai_check_key(key: str) -> tuple[bool, str]:
    """Can this key answer a call? (ok, human explanation).

    Realtime access is not on every account, and a key without it fails at the
    WebSocket handshake - by which point a caller is already on the line hearing
    silence. Listing the models is the cheapest way to know in advance.
    """
    if not key.strip():
        return False, "No key given."
    status, body = _get(f"{OPENAI_API}/v1/models", {"Authorization": f"Bearer {key}"})
    if status == 401:
        return False, "That key was rejected by OpenAI."
    if status != 200:
        detail = ""
        try:
            detail = json.loads(body).get("error", {}).get("message", "")
        except (json.JSONDecodeError, AttributeError):
            pass
        return False, detail or f"OpenAI returned HTTP {status}."

    if not openai_realtime_models(key):
        return False, (
            "The key works, but this account has no Realtime models. Realtime "
            "access is not enabled on every account, and without it the call "
            "connects and then hears nothing."
        )
    return True, "Key works, with Realtime access."


# ---------------------------------------------------------------------------
# Twilio
# ---------------------------------------------------------------------------


def _twilio_auth(sid: str, token: str) -> dict[str, str]:
    raw = base64.b64encode(f"{sid}:{token}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def twilio_numbers(sid: str, token: str) -> list[tuple[str, str]]:
    """(number, friendly name) for every number on the account. Never raises.

    Makes the phone number a dropdown rather than something typed in E.164 by
    hand, which is a format people get wrong in three different ways.
    """
    if not (sid.strip() and token.strip()):
        return []
    status, body = _get(
        f"{TWILIO_API}/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers.json"
        "?PageSize=50",
        _twilio_auth(sid, token),
    )
    if status != 200:
        log.info("could not list Twilio numbers (HTTP %s)", status)
        return []
    try:
        entries = json.loads(body).get("incoming_phone_numbers", []) or []
    except json.JSONDecodeError:
        return []
    return [
        (e.get("phone_number", ""), e.get("friendly_name", ""))
        for e in entries
        if e.get("phone_number")
    ]


def twilio_check(sid: str, token: str) -> tuple[bool, str]:
    """Are these credentials the ones that sign webhooks? (ok, explanation).

    The common mistake is pasting an API Key secret instead of the account Auth
    Token. Both look like credentials, only one validates webhook signatures, and
    the wrong one fails every inbound call with a 403 - which reads like a proxy
    problem, not a credential problem.
    """
    if not (sid.strip() and token.strip()):
        return False, "Both the Account SID and the Auth Token are needed."
    status, _ = _get(
        f"{TWILIO_API}/2010-04-01/Accounts/{sid}.json", _twilio_auth(sid, token)
    )
    if status == 401:
        return False, (
            "Twilio rejected those. The Auth Token is the one on Console → "
            "Account → API keys & tokens - an API Key secret will not work, "
            "because only the auth token signs webhooks."
        )
    if status != 200:
        return False, f"Twilio returned HTTP {status}."
    return True, "Credentials work."

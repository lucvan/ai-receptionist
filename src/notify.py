"""Delivering the call summary to the owner.

Composition and delivery are separate concerns and always have been. The
supervisor - when one is configured - *writes* the message and returns it as
plain text; if it is slow, unreachable, or produces nothing usable,
`format_fallback()` composes one locally from the call record with no network
call at all. Either way, this module is what puts the words in front of a human,
and it has never needed to know what wrote them.

That is why adding channels here is cheap: the summary arrives as a plain string
with no channel-specific markup (note that Telegram is sent with no `parse_mode`,
so a caller called "M&S_Delivery" cannot break the send), and every channel is
handed the same string.

## Channels versus the reply channel

Sending fans out: any number of channels can be enabled at once, and a routing
table decides which ones a given call category reaches, so spam can go nowhere
while an urgent call goes everywhere.

Replying is different and stays deliberately singular. `ReplyChannel` is the one
that can trigger a callback - i.e. make the service ring a real person - so it is
opt-in, authenticated, and there is at most one. Telegram is currently the only
implementation, because it is the only one of these with both a reliable
message-to-call correlation (`reply_to_message.message_id`) and a sender identity
that is not trivially forged.
"""

from __future__ import annotations

import asyncio
import logging
import re
import smtplib
from email.message import EmailMessage

import httpx

from .whatsapp import WhatsAppBridge

log = logging.getLogger(__name__)

CATEGORY_LABELS = {
    "spam_telesales": "spam / telesales",
    "tradesperson_admin": "tradesperson / admin",
    "delivery_appointment": "delivery / appointment",
    "recruiter_job_business": "recruiter / job / business",
    "family_friend_personal": "family / friend / personal",
    "unknown": "unknown",
    "urgent": "urgent",
}

SUGGESTED_ACTION = {
    "spam_telesales": "ignore",
    "tradesperson_admin": "call back",
    "delivery_appointment": "call back",
    "recruiter_job_business": "call back",
    "family_friend_personal": "call back",
    "unknown": "ask for more info",
    "urgent": "call back",
}


def format_fallback(payload: dict) -> str:
    """Build a message locally, for when the supervisor could not produce one.

    Same principle as the supervisor's version: prose, and nothing is mentioned
    unless it is actually known. A line reading "Company: not given" is worse than
    no line at all - it costs a glance and carries no information.
    """
    category = payload.get("category", "unknown")
    urgent = payload.get("flagged_urgent") or payload.get("claimed_urgency") == "high"

    if category == "spam_telesales":
        marker = "🚫"
    elif urgent:
        marker = "⚠️"
    else:
        marker = "📞"

    # Who rang.
    who = payload.get("caller_name") or ""
    known = payload.get("known_contact_name") or ""
    if known and not who:
        who = known
    elif known and who and known.lower() not in who.lower():
        who = f"{who} (saved as {known})"
    company = payload.get("company_or_relationship") or ""
    if who and company:
        subject = f"{who} from {company}"
    elif who:
        subject = who
    elif company:
        subject = f"Someone from {company}"
    else:
        subject = "Someone"

    label = CATEGORY_LABELS.get(category, category)
    parts = [f"{marker} {subject} called — {label}."]

    reason = payload.get("reason") or payload.get("summary") or ""
    if reason:
        parts.append(reason if reason.endswith((".", "!", "?")) else reason + ".")

    action = payload.get("requested_action")
    if action:
        parts.append(f"They'd like: {action}")

    if payload.get("flagged_urgent") and payload.get("flagged_why"):
        parts.append(f"Flagged urgent: {payload['flagged_why']}")

    if not payload.get("message_taken") and not reason:
        parts.append("They rang off before leaving anything useful.")

    suggestion = SUGGESTED_ACTION.get(category)
    if suggestion and suggestion != "ignore":
        parts.append(f"Suggest you {suggestion}.")

    text = " ".join(parts)

    callback = payload.get("callback_number")
    if callback and category != "spam_telesales":
        text += f"\n\n{callback}"

    return text


def subject_line(text: str, limit: int = 90) -> str:
    """First sentence of a summary, for channels that want a subject.

    The summary opens with a triage emoji and a "who rang and why" sentence,
    which is exactly what belongs in a subject line, so there is nothing to
    invent here.

    Note it is the first *sentence*, not the first line: `format_fallback`
    writes the whole summary as one paragraph with the callback number after a
    blank line, so taking the first line yields the entire message and a subject
    truncated mid-word.
    """
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if not first:
        return "Call"
    # Long enough to be a real sentence rather than an abbreviation like "Mr."
    sentence = re.match(r"(.+?[.!?])(?:\s|$)", first)
    if sentence and len(sentence.group(1)) >= 12:
        first = sentence.group(1)
    return first if len(first) <= limit else first[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


class Notifier:
    """One delivery channel.

    `send` returns a channel-owned reference string, or None on failure. It is
    opaque to everything above: for Telegram it is the message id, which is what
    makes a reply resolvable back to a call. Channels with no equivalent return
    an empty string on success, which is truthy-checked as "delivered, but not
    repliable".
    """

    name = "notifier"

    @property
    def enabled(self) -> bool:
        raise NotImplementedError

    async def send(self, text: str, payload: dict | None = None) -> str | None:
        raise NotImplementedError


class TelegramNotifier(Notifier):
    """A bot dedicated to the receptionist, sending to one fixed chat."""

    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str):
        self._token = bot_token
        self._chat_id = str(chat_id or "")

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    async def send(self, text: str, payload: dict | None = None) -> str | None:
        """Send a message. Returns its Telegram message id, or None on failure.

        The id matters: replying to a summary is how a callback is triggered,
        and the reply carries the id of the message it answers.
        """
        if not self.enabled:
            return None
        if not text.strip():
            return None

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{self._token}/sendMessage",
                    json={
                        "chat_id": self._chat_id,
                        "text": text,
                        # No parse_mode: caller-supplied names and company names
                        # would otherwise need escaping, and a stray underscore
                        # or asterisk would fail the send or mangle the message.
                        "disable_web_page_preview": True,
                    },
                )
            if resp.status_code == 200:
                body = resp.json()
                if body.get("ok"):
                    return str(body.get("result", {}).get("message_id") or "")
            # Never log the response body verbatim - the bot token appears in the
            # request URL and some Telegram errors echo request context back.
            log.error("telegram send failed with HTTP %s", resp.status_code)
            return None
        except Exception as exc:  # noqa: BLE001
            log.error("telegram send failed: %s", type(exc).__name__)
            return None


class EmailNotifier(Notifier):
    """SMTP, using the standard library rather than adding a dependency.

    One email per call is not a throughput problem, so a blocking `smtplib`
    session on a worker thread is the right amount of machinery. It also keeps
    the dependency list short, which matters more than usual for a service whose
    pitch is a small attack surface.
    """

    name = "email"

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        sender: str,
        recipient: str,
        starttls: bool = True,
    ):
        self._host = host or ""
        self._port = int(port or 587)
        self._username = username or ""
        self._password = password or ""
        self._sender = sender or username or ""
        self._recipient = recipient or ""
        self._starttls = bool(starttls)

    @property
    def enabled(self) -> bool:
        return bool(self._host and self._recipient and self._sender)

    def _deliver(self, text: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject_line(text)
        message["From"] = self._sender
        message["To"] = self._recipient
        message.set_content(text)

        # Port 465 is implicit TLS; 587 and 25 negotiate with STARTTLS.
        if self._port == 465:
            with smtplib.SMTP_SSL(self._host, self._port, timeout=30) as smtp:
                if self._username:
                    smtp.login(self._username, self._password)
                smtp.send_message(message)
            return

        with smtplib.SMTP(self._host, self._port, timeout=30) as smtp:
            if self._starttls:
                smtp.starttls()
            if self._username:
                smtp.login(self._username, self._password)
            smtp.send_message(message)

    async def send(self, text: str, payload: dict | None = None) -> str | None:
        if not self.enabled or not text.strip():
            return None
        try:
            await asyncio.to_thread(self._deliver, text)
        except Exception as exc:  # noqa: BLE001
            # smtplib exceptions can carry the server's response, which may echo
            # the envelope but never the password.
            log.error("email send failed: %s", type(exc).__name__)
            return None
        # Delivered, but there is no reply correlation to hand back: an emailed
        # summary cannot trigger a callback.
        return ""


class WebhookNotifier(Notifier):
    """POST the summary as JSON to any URL.

    The escape hatch that covers ntfy, Gotify, Discord, Home Assistant, n8n and
    anything else, without an adapter per service.
    """

    name = "webhook"

    def __init__(self, url: str, auth_header: str = ""):
        self._url = url or ""
        self._auth_header = auth_header or ""

    @property
    def enabled(self) -> bool:
        return self._url.startswith(("http://", "https://"))

    async def send(self, text: str, payload: dict | None = None) -> str | None:
        if not self.enabled or not text.strip():
            return None

        headers = {"Content-Type": "application/json"}
        if ":" in self._auth_header:
            name, _, value = self._auth_header.partition(":")
            headers[name.strip()] = value.strip()

        body = {
            "text": text,
            "title": subject_line(text),
            "category": (payload or {}).get("category", ""),
            "call": payload or {},
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(self._url, headers=headers, json=body)
        except Exception as exc:  # noqa: BLE001
            log.error("webhook send failed: %s", type(exc).__name__)
            return None

        if resp.status_code in (200, 201, 202, 204):
            return ""
        log.error("webhook send failed with HTTP %s", resp.status_code)
        return None


class WhatsAppNotifier(Notifier):
    """WhatsApp via a self-hosted bridge. See `whatsapp.py` for why."""

    name = "whatsapp"

    def __init__(self, bridge: WhatsAppBridge, recipient: str):
        self._bridge = bridge
        self._recipient = recipient or ""

    @property
    def enabled(self) -> bool:
        return self._bridge.configured and bool(self._recipient)

    async def send(self, text: str, payload: dict | None = None) -> str | None:
        if not self.enabled or not text.strip():
            return None
        ok, detail = await self._bridge.send(self._recipient, text)
        if ok:
            return ""
        log.error("whatsapp send failed: %s", detail[:120])
        return None


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------


class NotifierSet:
    """Every configured channel, plus the routing table that picks between them.

    Built fresh from settings on each send rather than held as long-lived
    objects, because the settings behind them are editable in the admin UI and a
    change should apply to the next call, not the next restart. The objects are
    config holders around a short-lived httpx client, so this costs nothing.
    """

    def __init__(self, cfg):
        self._cfg = cfg

    # -- construction ------------------------------------------------------

    def _build(self, name: str) -> Notifier | None:
        cfg = self._cfg
        entry = cfg.settings.channel(name)

        # A channel with no settings entry at all falls back to its env
        # configuration, so an install that predates the settings file - or one
        # that never opens the UI - keeps working untouched.
        if entry and not entry.get("enabled", False):
            return None

        if name == "telegram":
            return TelegramNotifier(
                cfg.telegram_bot_token,
                entry.get("chat_id") or cfg.telegram_chat_id,
            )
        if name == "email":
            return EmailNotifier(
                host=entry.get("host") or cfg.smtp_host,
                port=entry.get("port") or cfg.smtp_port,
                username=entry.get("username") or cfg.smtp_username,
                # Never from settings.json: secrets stay in the environment.
                password=cfg.smtp_password,
                sender=entry.get("sender") or cfg.smtp_sender,
                recipient=entry.get("to") or cfg.email_to,
                starttls=entry.get("starttls", cfg.smtp_starttls),
            )
        if name == "webhook":
            return WebhookNotifier(
                url=entry.get("url") or cfg.webhook_url,
                auth_header=cfg.webhook_auth_header,
            )
        if name == "whatsapp":
            return WhatsAppNotifier(
                bridge=build_bridge(cfg),
                recipient=entry.get("to") or cfg.whatsapp_to,
            )
        return None

    def channels(self) -> dict[str, Notifier]:
        """Every channel that is both configured and enabled."""
        out = {}
        for name in ("telegram", "email", "webhook", "whatsapp"):
            notifier = self._build(name)
            if notifier is not None and notifier.enabled:
                out[name] = notifier
        return out

    @property
    def any_enabled(self) -> bool:
        return bool(self.channels())

    # -- sending -----------------------------------------------------------

    def _targets(self, category: str | None) -> list[str] | None:
        """Channel names for a category, or None to mean 'everything enabled'."""
        if category is None:
            return None
        return self._cfg.settings.routing(category)

    async def send(
        self, text: str, category: str | None = None, payload: dict | None = None
    ) -> dict[str, str]:
        """Deliver to every routed channel. Returns {channel: reference}.

        An empty result is ambiguous on its own - it could be "routed nowhere on
        purpose" or "every channel failed" - so callers get `suppressed()` to
        tell the two apart before deciding whether a failure is worth an ERROR.
        """
        available = self.channels()
        if not available:
            log.warning("no notification channel is configured, summary not delivered")
            return {}

        routed = self._targets(category)
        if routed is None:
            chosen = available
        else:
            chosen = {n: c for n, c in available.items() if n in routed}
            if not chosen:
                log.info(
                    "call category %s is routed to no channel - not notifying",
                    category,
                )
                return {}

        results = await asyncio.gather(
            *(c.send(text, payload) for c in chosen.values()),
            return_exceptions=True,
        )

        refs: dict[str, str] = {}
        for name, result in zip(chosen.keys(), results):
            if isinstance(result, BaseException):
                log.error("%s send raised %s", name, type(result).__name__)
                continue
            if result is None:
                continue
            refs[name] = result

        if not refs:
            log.error("every notification channel failed for this message")
        return refs

    def suppressed(self, category: str | None) -> bool:
        """True when routing deliberately sends this category nowhere."""
        routed = self._targets(category)
        return routed is not None and not routed

    async def reply(self, text: str) -> str | None:
        """An operational message back to whoever triggered something.

        These are answers to an interaction in the reply channel - a callback
        confirmation, a "that reply doesn't match a call" - so they belong in
        that channel only. Fanning them out to email would mean an inbox full of
        half a conversation.
        """
        telegram = self._build("telegram")
        if telegram is None or not telegram.enabled:
            return None
        return await telegram.send(text)


def build_bridge(cfg) -> WhatsAppBridge:
    """The WhatsApp bridge described by settings, falling back to the env."""
    entry = cfg.settings.channel("whatsapp")
    return WhatsAppBridge(
        base_url=entry.get("bridge_url") or cfg.whatsapp_bridge_url,
        # Never from settings.json.
        api_key=cfg.whatsapp_bridge_key,
        flavour=entry.get("flavour") or cfg.whatsapp_flavour,
        session=entry.get("session") or cfg.whatsapp_session,
        custom_send_path=entry.get("custom_path", ""),
        custom_body=entry.get("custom_body", ""),
    )

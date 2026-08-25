"""HTTP + WebSocket surface. Three routes, and nothing else is exposed.

    GET  /health          liveness + configuration readiness, no secrets
    POST /incoming-call   Twilio voice webhook, returns TwiML
    WS   /media-stream    bidirectional call audio
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse

from .admin import build_admin_app
from .callrecord import CallRecord, append_record
from .config import config
from .contacts import ContactBook
from .history import CallHistory
from .notify import NotifierSet, format_fallback
from .outbound import OutboundCaller
from .pending import CallbackStore, PendingCall, PendingStore
from .persona import Persona, render
from .realtime import CallBridge
from .supervisor import SupervisorClient
from .telegram_listener import TelegramListener
from .twilio_auth import (
    mint_stream_token,
    public_request_url,
    validate_twilio_signature,
    verify_stream_token,
)

logging.basicConfig(
    level=getattr(logging, config.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

# httpx logs every request at INFO including the full URL. The Telegram Bot API
# carries the bot token *in the path*, so leaving this at INFO writes a live
# credential into the container logs on every single call.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

log = logging.getLogger("ai-receptionist")

app = FastAPI(title="ai-receptionist", docs_url=None, redoc_url=None, openapi_url=None)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
PROMPT_PATH = PROMPTS_DIR / "receptionist.md"
CALLBACK_PROMPT_PATH = PROMPTS_DIR / "callback.md"

# Who this receptionist works for. Read once at startup and threaded into the
# prompts and the per-call context, so `prompts/` ships with placeholders rather
# than somebody's name edited into it.
persona = Persona.from_env()

supervisor = SupervisorClient(
    url=config.supervisor_url,
    api_key=config.supervisor_key,
    model=config.supervisor_model,
    enabled=config.supervisor_enabled,
    persona=persona,
)

# Every configured channel, plus the routing table that picks between them.
# Reads settings live, so enabling a channel in the admin UI applies to the
# next call rather than the next restart.
notifier = NotifierSet(config)

contacts = ContactBook(config.contacts_path, config.contacts_country_code)

history = CallHistory(
    config.log_dir,
    config.contacts_country_code,
    # Folds a call logged against a mistyped number onto the saved contact
    # it belongs to, so one person is not split across two profiles.
    canonical=contacts.canonical_key,
)

pending = PendingStore(config.log_dir / "pending-calls.json")

outbound = OutboundCaller(
    account_sid=config.twilio_account_sid,
    auth_token=config.twilio_auth_token,
    from_number=config.twilio_phone_number,
    public_base_url=config.public_base_url,
    cooldown_s=config.callback_cooldown_s,
    max_per_hour=config.callback_max_per_hour,
)

# Callbacks in flight, keyed by the stream token minted when the call is placed.
# On disk, not in memory: Twilio fetches the TwiML and then opens the media socket,
# and a restart in that window would leave someone answering the phone to a service
# that has forgotten why it rang them.
callbacks = CallbackStore(config.log_dir / "pending-callbacks.json")

# Transfers awaiting Twilio's <Dial>, keyed by token. Same reasoning as callbacks:
# there is a round trip between asking Twilio to redirect and Twilio fetching the
# TwiML, and the caller is on the line for it.
transfers = CallbackStore(config.log_dir / "pending-transfers.json")


def load_instructions(path: Path = PROMPT_PATH) -> str:
    """Read and render the prompt fresh each call.

    Fresh so wording edits take effect without a rebuild; rendered so the shipped
    prompt can carry `{{owner_name}}` placeholders instead of a real name.
    """
    try:
        return render(path.read_text(encoding="utf-8"), persona)
    except OSError as exc:
        log.error("could not read prompt at %s: %s", path, exc)
        # A receptionist with no prompt would improvise, which is exactly the
        # failure mode the security model exists to prevent. Refuse instead.
        raise


listener = TelegramListener(
    bot_token=config.telegram_bot_token,
    chat_id=config.telegram_chat_id,
    allowed_user_ids=set(
        u.strip() for u in config.telegram_allowed_user_ids.split(",") if u.strip()
    ),
    offset_path=config.log_dir / "telegram-offset.json",
    on_reply=lambda reply_to, text: _handle_owner_reply(reply_to, text),
)


@app.on_event("startup")
async def _startup() -> None:
    missing = config.missing_required()
    if missing:
        log.warning("not ready to take calls, missing: %s", ", ".join(missing))
    else:
        log.info("ready, model=%s", config.openai_realtime_model)
    log.info("supervisor bridge %s", "enabled" if supervisor.enabled else "disabled")

    channels = sorted(notifier.channels())
    if channels:
        log.info("notifying via %s", ", ".join(channels))
    else:
        # Calls would still be answered and recorded, but nobody would hear
        # about them. Worth an ERROR at startup rather than a surprise later.
        log.error("no notification channel configured - calls will go unreported")

    if config.callbacks_enabled:
        if not outbound.enabled:
            log.error("callbacks enabled but outbound calling is not configured")
        elif not config.telegram_allowed_user_ids:
            # Without this, chat membership alone would be enough to place calls.
            log.error("callbacks enabled but TELEGRAM_ALLOWED_USER_IDS is empty")
        else:
            listener.start()
    else:
        log.info("callbacks disabled")

    if config.history_enabled:
        history.reload(force=True)
    else:
        log.info("caller history disabled")

    _start_admin()


def _start_admin() -> None:
    """Serve the admin UI on its own port, in this process.

    A separate port rather than a path on the main app, because the main app is
    published to the LAN and proxied to a public hostname for Twilio. A
    path there would put the contact book on the public internet behind nothing
    but a password; a port that is only ever published to 127.0.0.1 cannot be
    reached from outside the host at all.
    """
    if not config.admin_password:
        log.info("admin UI disabled (ADMIN_PASSWORD not set)")
        return
    if not config.stream_secret:
        log.error("admin UI needs STREAM_TOKEN_SECRET to sign sessions - not started")
        return

    import uvicorn

    admin_app = build_admin_app(
        password=config.admin_password,
        secret=config.stream_secret,
        contacts_path=config.contacts_path,
        history=history,
        country_code=config.contacts_country_code,
        cfg=config,
        notifier=notifier,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            admin_app,
            host=config.admin_bind,
            port=config.admin_port,
            log_level="warning",
            server_header=False,
        )
    )
    asyncio.create_task(server.serve())
    log.info(
        "admin UI on %s:%d (password required)", config.admin_bind, config.admin_port
    )
    if config.admin_bind == "0.0.0.0":
        # Correct in a container, where the compose publish is the boundary.
        # Wrong outside one, and the failure is silent - so say it either way
        # and let the reader check which case they are in.
        log.info(
            "admin UI is bound to all interfaces; outside Docker set "
            "ADMIN_BIND=127.0.0.1, and inside it keep the compose publish on "
            "127.0.0.1 - this port serves contacts, call history and settings"
        )


@app.on_event("shutdown")
async def _shutdown() -> None:
    await listener.stop()


@app.get("/health")
async def health() -> JSONResponse:
    missing = config.missing_required()
    channels = sorted(notifier.channels())
    return JSONResponse(
        {
            "status": "ok" if not missing and channels else "degraded",
            "service": "ai-receptionist",
            "model": config.openai_realtime_model,
            "supervisor": "enabled" if supervisor.enabled else "disabled",
            "transcript_retention": config.retain_transcripts,
            "contacts_loaded": len(contacts),
            # Which channels would hear about a call, by name. A service with
            # none is answering calls nobody is told about, which is degraded
            # even when every credential it holds is valid.
            "notification_channels": channels,
            # Names only. Never the values.
            "missing_config": missing,
        },
        status_code=200,
    )


@app.post("/incoming-call")
async def incoming_call(request: Request) -> PlainTextResponse:
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    call_sid = params.get("CallSid", "")

    if config.validate_twilio_signature:
        signature = request.headers.get("X-Twilio-Signature", "")
        url = public_request_url(config.public_base_url, "/incoming-call")
        if not validate_twilio_signature(
            config.twilio_auth_token, signature, url, params
        ):
            log.warning("rejected /incoming-call with bad or missing signature")
            return PlainTextResponse("forbidden", status_code=403)

    if not call_sid:
        return PlainTextResponse("missing CallSid", status_code=400)

    missing = config.missing_required()
    if missing:
        log.error("call %s arrived but service is unconfigured: %s", call_sid, missing)
        return PlainTextResponse(
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response><Say>Sorry, this number is not available right now."
            "</Say><Hangup/></Response>",
            media_type="application/xml",
        )

    token = mint_stream_token(config.stream_secret, call_sid)
    # ForwardedFrom is set when the call reached us via carrier call-forwarding
    # (i.e. the owner's mobile diverting to us). `From` should still be the original
    # caller; logging both makes it obvious if a carrier ever presents the
    # forwarding party instead, which would break contact recognition.
    forwarded_from = params.get("ForwardedFrom", "")
    log.info(
        "call %s inbound from %s%s",
        call_sid,
        params.get("From", "unknown"),
        f" (forwarded from {forwarded_from})" if forwarded_from else "",
    )

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Connect>"
        f'<Stream url="{config.wss_stream_url}">'
        f'<Parameter name="token" value="{token}"/>'
        f'<Parameter name="from" value="{params.get("From", "")}"/>'
        f'<Parameter name="to" value="{params.get("To", "")}"/>'
        f'<Parameter name="forwarded_from" value="{forwarded_from}"/>'
        "</Stream>"
        "</Connect>"
        "</Response>"
    )
    return PlainTextResponse(twiml, media_type="application/xml")


async def _handle_owner_reply(reply_to: int | None, text: str) -> None:
    """The owner replied: file what they said, and ring the caller back.

    Every reply is filed against the caller's number whether or not a call goes
    out, so that if they ring in again the agent already knows what was said. A
    reply starting with "note:" files it and stops there - context without a call.
    """
    call = pending.get(reply_to) if reply_to else pending.most_recent()
    if not call:
        await notifier.reply(
            "I don't have a call to match that reply to — reply directly to a call "
            "summary and I'll ring them back."
        )
        return

    stripped = text.strip()
    note_only = stripped[:5].lower() in {"note:", "note "} or stripped[:2].lower() == "n:"
    if note_only:
        stripped = stripped.split(":", 1)[-1].strip() if ":" in stripped[:5] else stripped[5:].strip()

    # A note-only reply is filed as written - that is deliberately a note.
    # A callback reply is not: "tell her friday works" is an instruction, and
    # filing that verbatim leaves the agent reading a stale to-do months later.
    # The record of what was actually settled is written below, once the
    # supervisor has turned his shorthand into it.
    if note_only and call.number:
        history.add_note(call.number, stripped, call.call_sid)

    if note_only:
        await notifier.reply(
            f"📝 Noted against {call.display_name}. I'll have that to hand if they "
            "call again, and it goes with any callback."
        )
        return

    if not call.number:
        await notifier.reply(
            f"No number for {call.display_name}, so I can't ring them back."
        )
        return

    script, _, note = await supervisor.build_callback_script(
        {
            "summary": call.summary,
            "caller_name": call.caller_name,
            "category": call.category,
        },
        text,
        config.supervisor_script_timeout_s,
    )

    token = mint_stream_token(config.stream_secret, f"callback:{call.call_sid}")
    callbacks.put(
        token,
        {
            "call_sid": call.call_sid,
            "number": call.number,
            "name": call.caller_name,
            "script": script,
        },
    )

    ok, detail = await outbound.place_call(call.number, token)
    if ok:
        # Filed as what was settled, not as what was asked for - the next call
        # should read "Confirmed Friday 2pm works", not "tell her friday works".
        history.add_note(
            call.number,
            note or f"Rang them back: {stripped}",
            call.call_sid,
        )
        await notifier.reply(
            f"📲 Ringing {call.display_name} on {call.number} now, saying:\n\n"
            f"“{script}”"
        )
    else:
        callbacks.take(token)
        # Still worth recording: a decision was made, even if the call failed.
        history.add_note(
            call.number,
            f"{note or stripped} (tried to ring them back, but the call failed)",
            call.call_sid,
        )
        await notifier.reply(
            f"Couldn't ring {call.display_name} back — {detail}"
        )


@app.post("/outbound-call")
async def outbound_call(request: Request) -> PlainTextResponse:
    """TwiML for a callback. Twilio fetches this once the person picks up."""
    token = request.query_params.get("token", "")

    if config.validate_twilio_signature:
        form = await request.form()
        params = {k: str(v) for k, v in form.items()}
        signature = request.headers.get("X-Twilio-Signature", "")
        url = public_request_url(
            config.public_base_url, "/outbound-call", f"token={token}"
        )
        if not validate_twilio_signature(
            config.twilio_auth_token, signature, url, params
        ):
            log.warning("rejected /outbound-call with bad or missing signature")
            return PlainTextResponse("forbidden", status_code=403)

    if callbacks.peek(token) is None:
        log.warning("rejected /outbound-call with an unknown token")
        return PlainTextResponse(
            '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
            media_type="application/xml",
        )

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Connect>"
        f'<Stream url="{config.wss_stream_url}">'
        f'<Parameter name="token" value="{token}"/>'
        f'<Parameter name="callback" value="1"/>'
        "</Stream>"
        "</Connect>"
        "</Response>"
    )
    return PlainTextResponse(twiml, media_type="application/xml")


@app.post("/transfer")
async def transfer(request: Request) -> PlainTextResponse:
    """Dial the owner, announcing who is calling before connecting them."""
    token = request.query_params.get("token", "")
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}

    if config.validate_twilio_signature:
        url = public_request_url(config.public_base_url, "/transfer", f"token={token}")
        if not validate_twilio_signature(
            config.twilio_auth_token, request.headers.get("X-Twilio-Signature", ""), url, params
        ):
            log.warning("rejected /transfer with bad or missing signature")
            return PlainTextResponse("forbidden", status_code=403)

    entry = transfers.peek(token)
    if not entry:
        return PlainTextResponse(
            '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
            media_type="application/xml",
        )

    action = f"{config.public_base_url}/transfer-result?token={token}"
    whisper = f"{config.public_base_url}/whisper?token={token}"
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Dial action="{action}" method="POST" timeout="{config.transfer_timeout_s}" '
        f'callerId="{config.twilio_phone_number}">'
        f'<Number url="{whisper}" method="POST">{config.transfer_to_number}</Number>'
        "</Dial>"
        "</Response>"
    )
    return PlainTextResponse(twiml, media_type="application/xml")


@app.post("/whisper")
async def whisper(request: Request) -> PlainTextResponse:
    """Played to the owner only, before the caller is connected."""
    token = request.query_params.get("token", "")
    entry = transfers.peek(token) or {}
    who = entry.get("caller_name") or "someone"
    why = entry.get("reason") or ""
    line = f"Call from {who}." + (f" {why}" if why else "") + " Connecting you now."
    return PlainTextResponse(
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Say voice="Polly.Amy" language="en-GB">{line}</Say></Response>',
        media_type="application/xml",
    )


@app.post("/transfer-result")
async def transfer_result(request: Request) -> PlainTextResponse:
    """After the <Dial> finishes - connected, or nobody picked up."""
    token = request.query_params.get("token", "")
    form = await request.form()
    status = str(form.get("DialCallStatus", ""))
    entry = transfers.take(token) or {}
    who = entry.get("caller_name") or "A caller"

    if status == "completed":
        log.info("transfer connected for %s", who)
        await notifier.send(f"✅ Put {who} through to you.")
        return PlainTextResponse(
            '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
            media_type="application/xml",
        )

    log.info("transfer not answered (%s) for %s", status, who)
    await notifier.send(
        f"📞 {who} asked to be put through and you didn't pick up "
        f"({status or 'no answer'}). Their message is in the summary above."
    )
    return PlainTextResponse(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response><Say voice="Polly.Amy" language="en-GB">'
        "Sorry, I couldn't reach him just now, but I've got your message and "
        "I'll make sure he gets it. Thanks for calling."
        "</Say><Hangup/></Response>",
        media_type="application/xml",
    )


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket) -> None:
    await websocket.accept()

    # Twilio sends `connected` then `start`. The custom parameters we need to
    # authenticate the socket only arrive in `start`, so read until we see it
    # rather than trusting anything before that point.
    start_msg = None
    try:
        for _ in range(5):
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
            msg = json.loads(raw)
            if msg.get("event") == "start":
                start_msg = msg
                break
    except (asyncio.TimeoutError, WebSocketDisconnect, json.JSONDecodeError):
        pass

    if not start_msg:
        log.warning("media stream closed: no start frame")
        await websocket.close(code=1008)
        return

    start = start_msg.get("start", {})
    stream_sid = start.get("streamSid", "")
    call_sid = start.get("callSid", "")
    custom = start.get("customParameters", {}) or {}

    token = custom.get("token", "")
    is_callback = custom.get("callback") == "1"
    callback = callbacks.take(token) if is_callback else None

    if is_callback:
        # The token was minted against the *original* call, not this new outbound
        # one, so it is verified against that identity rather than Twilio's CallSid.
        if not callback or not verify_stream_token(
            config.stream_secret,
            f"callback:{callback['call_sid']}",
            token,
            config.stream_token_ttl_s,
        ):
            log.warning("callback stream rejected: bad or unknown token")
            await websocket.close(code=1008)
            return
    elif not verify_stream_token(
        config.stream_secret, call_sid, token, config.stream_token_ttl_s
    ):
        log.warning("media stream rejected: bad stream token for call %s", call_sid)
        await websocket.close(code=1008)
        return

    record = CallRecord(
        call_sid=call_sid,
        from_number=custom.get("from", ""),
        to_number=custom.get("to", ""),
    )

    if callback:
        record.direction = "outbound"
        record.to_number = callback["number"]
        # The other party's number, which for a callback is the one we dialled.
        # Downstream (log redaction, callback resolution) reads from_number for
        # "whoever is on the other end", so keep that meaning consistent.
        record.from_number = callback["number"]
        record.caller_name = callback.get("name", "")
        record.known_contact_name = contacts.lookup(callback["number"])
        log.info(
            "callback %s ringing %s", call_sid, callback.get("name") or "caller"
        )
    else:
        # Resolved before the agent speaks, so it can open by name.
        record.forwarded_from = custom.get("forwarded_from", "")
        # Through the history index first, so a caller ringing on a second number
        # that has been merged into their profile is still recognised as them.
        contact = contacts.get(record.from_number) or contacts.get_by_key(
            history.key_for(record.from_number)
        )
        if contact:
            record.known_contact_name = contact.display
            record.known_contact_full_name = contact.name
            record.known_contact_relationship = contact.relationship
            record.known_contact_notes = contact.notes
        if record.known_contact_name:
            log.info(
                "call %s recognised as a saved contact: %s",
                call_sid,
                record.known_contact_name,
            )

    try:
        instructions = load_instructions(
            CALLBACK_PROMPT_PATH if callback else PROMPT_PATH
        )
    except OSError:
        await websocket.close(code=1011)
        return

    # Rendered here rather than in the bridge: the bridge is the caller-exposed
    # component and is handed a finished string, never a handle to the call log.
    # Callbacks get it too: ringing someone back knowing nothing about the call
    # that prompted it is exactly the gap this closes.
    caller_history = ""
    if config.history_enabled and record.from_number:
        caller_history = history.prompt_section(
            record.from_number, config.history_max_calls
        )
        if caller_history:
            log.info("call %s: caller has rung before", call_sid)

    bridge = CallBridge(
        twilio_ws=websocket,
        record=record,
        cfg=config,
        instructions=instructions,
        caller_history=caller_history,
        stream_sid=stream_sid,
        outbound_script=(callback or {}).get("script", ""),
        outbound_to_name=(callback or {}).get("name", ""),
        persona=persona,
    )

    try:
        await bridge.run()
    finally:
        if bridge.transfer_requested:
            # The caller is still on the line. Hand the live call over to Twilio's
            # <Dial> rather than finishing it, and let /transfer-result report back.
            await _start_transfer(record, bridge.transfer_reason)
        else:
            await _finish_call(record)
        try:
            await websocket.close()
        except RuntimeError:
            pass  # already closed by the caller hanging up


async def _start_transfer(record: CallRecord, reason: str) -> None:
    """Redirect the live call to <Dial>, so the owner's phone rings."""
    token = mint_stream_token(config.stream_secret, f"transfer:{record.call_sid}")
    transfers.put(
        token,
        {
            "call_sid": record.call_sid,
            "caller_name": record.caller_name or record.known_contact_name or "",
            "reason": reason,
        },
    )

    # The message is written and notified *before* the transfer is attempted, so a
    # missed call still leaves something to act on.
    await _finish_call(record, transferring=True)

    ok, detail = await outbound.redirect_call(
        record.call_sid, f"{config.public_base_url}/transfer?token={token}"
    )
    if not ok:
        transfers.take(token)
        log.error("could not transfer call %s: %s", record.call_sid, detail)
        await notifier.send(
            f"Tried to put {record.caller_name or 'a caller'} through but the "
            f"transfer failed — {detail}"
        )


async def _finish_call(record: CallRecord, transferring: bool = False) -> None:
    """Persist the minimised record and hand it to the supervisor."""
    log.info("call ended %s", record.log_line())

    # A number the agent heard down the phone is the least reliable thing on the
    # record. If it is a near miss of one we already hold - a digit too many, a
    # digit wrong - the saved one is authoritative and wins. Done before anything
    # reads the record, so the summary, the callback and the history index all
    # agree on one number and a caller does not end up split across two profiles.
    if record.callback_number:
        corrected = contacts.correct(record.callback_number)
        if corrected != record.callback_number:
            log.info("call %s: corrected callback number to a saved contact",
                     record.call_sid)
            record.callback_number = corrected

    payload = record.to_supervisor_payload()

    # The supervisor writes the summary, because it is the side with the context to
    # spot a useful follow-up. If it cannot, we compose one locally rather than let
    # the call go unreported.
    summary_text = ""
    if supervisor.enabled:
        _, summary_text = await supervisor.deliver(
            payload, config.supervisor_final_timeout_s
        )
        if summary_text:
            record.supervisor_summary = summary_text
        else:
            log.warning(
                "call %s: supervisor produced no summary, falling back to a "
                "locally composed one",
                record.call_sid,
            )

    if not summary_text:
        summary_text = format_fallback(payload)

    try:
        path = append_record(config.log_dir, record, config.retain_transcripts)
        log.info("call %s recorded in %s", record.call_sid, path.name)
    except OSError as exc:
        log.error("could not write call record: %s", exc)

    if transferring:
        # Sent before the transfer is attempted, so a missed call still leaves
        # the message rather than just a missed-call notification.
        summary_text = (
            "📞 Putting a caller through — details below in case you miss them.\n\n"
            + summary_text
        )

    refs = await notifier.send(summary_text, category=record.category, payload=payload)
    if refs:
        log.info(
            "call %s summary delivered via %s",
            record.call_sid,
            ", ".join(sorted(refs)),
        )
        # Remember what this message was about, so replying to it can ring them
        # back. Outbound calls are not repliable - that would let a callback
        # trigger another callback.
        #
        # Only Telegram hands back a usable reference: a callback needs to know
        # which call a reply answers, and an emailed summary carries nothing
        # that maps back. Other channels deliver the summary and stop there.
        message_id = refs.get("telegram")
        if message_id and config.callbacks_enabled and record.direction == "inbound":
            pending.remember(
                int(message_id),
                PendingCall(
                    call_sid=record.call_sid,
                    number=record.resolved_callback(),
                    caller_name=record.caller_name or record.known_contact_name,
                    category=record.category,
                    summary=record.summary or summary_text[:400],
                    created_at=time.time(),
                ),
            )
    elif notifier.suppressed(record.category):
        # Routed nowhere on purpose - "never tell me about spam" is a setting,
        # not a fault. The record is still written either way.
        log.info(
            "call %s: category %s is routed to no channel, not notified",
            record.call_sid,
            record.category,
        )
    else:
        # The record is still on disk, so nothing is lost - but nobody has been
        # told about a call that came in, which is worth an ERROR.
        log.error(
            "call %s: summary NOT delivered - it is in the call log only",
            record.call_sid,
        )

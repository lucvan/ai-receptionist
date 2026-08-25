"""The setup wizard: a clean install to a phone that answers, without a dotfile.

Mounted by `admin.py` under `/setup`. Everything here is reachable only behind
the same session cookie as the rest of the admin UI, on the same loopback-only
port - read the "Admin UI" section of the README before changing where it is
served from.

## What it is for

The old path from clone to working service was: read SETUP.md, hand-edit `.env`,
guess which of thirty variables matter, restart, read the logs, repeat. That is
fine for the person who wrote it and hostile to everyone else, and the failures
are quiet - a key in the wrong variable, a voice name with a typo, an ElevenLabs
agent that was never provisioned.

So this walks the same ground in five steps, and at each one:

- **Anything with a knowably finite set is a dropdown, never a text box.** Voice,
  model, language, provider, turn-taking, WhatsApp flavour. A free-text voice
  name is a caller hearing silence three days later; a `<select>` cannot be
  mistyped. The lists live in `settings.CHOICES`, except the ElevenLabs voice
  list which is fetched live from the account.
- **Credentials are checked against the real API before they are stored**, so
  "that key has no ConvAI permissions" is said here rather than discovered by a
  caller.
- **Nothing is offered that cannot work.** A provider with no credentials cannot
  be selected; the Finish step reports readiness rather than claiming success.

## Steps

1. **Identity** - who the receptionist answers for. Fills the prompt placeholders.
2. **Voice** - provider, key, voice. Includes the ElevenLabs provisioning button.
3. **Phone** - Twilio credentials and the public hostname.
4. **Notifications** - at least one channel, or nobody hears about a call.
5. **Finish** - live readiness check against the same rules `/health` uses.

Steps are independent and individually saveable: this is a checklist, not a
transaction. Someone who only wants to change the voice should not have to walk
past their Twilio credentials to do it.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .catalogs import (
    OPENAI_VOICES,
    openai_check_key,
    openai_realtime_models,
    twilio_check,
    twilio_numbers,
)
from .elevenlabs_provision import check_key, list_agents, list_voices, provision, verify
from .secrets_store import SECRET_KEYS
from .settings import CHOICES
from .tools import TOOL_SPECS, TRANSFER_TOOL

log = logging.getLogger(__name__)

STEPS = [
    ("identity", "Who it answers for"),
    ("voice", "Voice"),
    ("phone", "Phone line"),
    ("notify", "Notifications"),
    ("finish", "Finish"),
]

# Persona lives in the environment because `Persona.from_env()` is read once at
# startup and threaded into every prompt. The wizard can still collect it, but it
# has to be honest that these need a restart, unlike everything else here.
PERSONA_KEYS = {
    "OWNER_NAME": "Their name",
    "OWNER_PRONOUN_OBJECT": "Object pronoun",
    "OWNER_PRONOUN_POSSESSIVE": "Possessive pronoun",
    "ASSISTANT_NAME": "What the assistant calls itself",
}

PRONOUN_CHOICES = {
    "OWNER_PRONOUN_OBJECT": [("them", "them"), ("him", "him"), ("her", "her")],
    "OWNER_PRONOUN_POSSESSIVE": [("their", "their"), ("his", "his"), ("her", "her")],
}


def register(admin, deps) -> None:
    """Attach the wizard routes to the admin app.

    `deps` carries the handful of things the wizard needs from `admin.py` -
    passed rather than imported, so the two modules do not become circular and
    so the helpers (escaping, the page shell, auth) stay defined in one place.
    """
    cfg = deps["cfg"]
    secrets = deps["secrets"]
    persist = deps["persist"]
    authed = deps["authed"]
    page = deps["page"]
    esc = deps["esc"]

    # -- shared form controls ------------------------------------------------

    def select(label: str, name: str, value, options, hint: str = "") -> str:
        """A dropdown. The reason so much of this file is dropdowns.

        `options` is a list of (value, label). An unrecognised current value is
        kept as an extra option rather than silently dropped - otherwise saving
        an unrelated part of the form would rewrite it to whatever happened to
        be first in the list.
        """
        known = [str(v) for v, _ in options]
        current = "" if value is None else str(value)
        extra = (
            [(current, f"{current} (current, not a known value)")]
            if current and current not in known
            else []
        )
        rendered = "".join(
            f'<option value="{esc(v)}"{" selected" if str(v) == current else ""}>'
            f"{esc(text)}</option>"
            for v, text in list(options) + extra
        )
        return (
            f'<label class="field"><span>{esc(label)}</span>'
            f'<select name="{esc(name)}">{rendered}</select></label>'
            + (f'<p class="hint">{esc(hint)}</p>' if hint else "")
        )

    def text(label: str, name: str, value, hint: str = "", kind: str = "text") -> str:
        return (
            f'<label class="field"><span>{esc(label)}</span>'
            f'<input type="{kind}" name="{esc(name)}" value="{esc(value)}"></label>'
            + (f'<p class="hint">{esc(hint)}</p>' if hint else "")
        )

    def secret_field(label: str, name: str, hint: str = "") -> str:
        """A write-only credential box.

        Always renders empty, whatever is stored. That is the mitigation that
        makes UI-settable credentials defensible at all: a stolen session can
        replace a key, which is loud and recoverable, but cannot read one, which
        would be silent and permanent. See `secrets_store.py`.
        """
        stored = secrets.has(name)
        env_name = SECRET_KEYS.get(name, name.upper())
        state = (
            '<span class="pill on">set</span>'
            if stored or getattr(cfg, name, "")
            else '<span class="pill off">not set</span>'
        )
        where = " (from the UI)" if stored else (
            f" (from {env_name} in .env)" if getattr(cfg, name, "") else ""
        )
        return (
            f'<label class="field"><span>{esc(label)} {state}'
            f'<small>{esc(where)}</small></span>'
            f'<input type="password" name="{esc(name)}" value="" '
            f'autocomplete="new-password" placeholder="'
            + ("leave blank to keep the current one" if stored or getattr(cfg, name, "")
               else "paste it here")
            + '"></label>'
            + (f'<p class="hint">{esc(hint)}</p>' if hint else "")
        )

    def nav(here: str) -> str:
        out = ['<div class="wizsteps">']
        for index, (key, label) in enumerate(STEPS, start=1):
            on = " class=on" if key == here else ""
            out.append(f'<a href="/setup/{key}"{on}><b>{index}</b> {esc(label)}</a>')
        out.append("</div>")
        return "".join(out)

    def shell(step: str, title: str, body: str, note: str = "") -> HTMLResponse:
        banner = f'<div class="wiznote">{note}</div>' if note else ""
        return page(f"Setup — {title}", nav(step) + banner + body, here="settings")

    def flash(msg: str) -> str:
        """Escape a message that arrived back through the redirect query string.

        `shell` takes ready-made HTML, because the fixed notes in this file use
        markup. Anything coming off the URL has to be escaped before it gets
        there - it is attacker-controllable even on a loopback-only page, and
        reflecting it raw would be exactly the XSS the rest of this UI avoids by
        escaping everything.
        """
        return esc(msg)

    def redirect(step: str, msg: str = "") -> RedirectResponse:
        from urllib.parse import quote_plus

        suffix = f"?msg={quote_plus(msg)}" if msg else ""
        return RedirectResponse(f"/setup/{step}{suffix}", status_code=303)

    def guard(request: Request):
        return None if authed(request) else RedirectResponse("/login", status_code=303)

    # -- 0. entry ------------------------------------------------------------

    @admin.get("/setup")
    async def setup_home(request: Request) -> Response:
        if (bad := guard(request)) is not None:
            return bad
        return RedirectResponse("/setup/identity", status_code=303)

    # -- 1. identity ---------------------------------------------------------

    @admin.get("/setup/identity")
    async def identity(request: Request, msg: str = "") -> Response:
        if (bad := guard(request)) is not None:
            return bad
        import os

        fields = []
        for key, label in PERSONA_KEYS.items():
            current = os.environ.get(key, "")
            if key in PRONOUN_CHOICES:
                fields.append(select(label, key, current or None, PRONOUN_CHOICES[key]))
            else:
                fields.append(text(label, key, current))

        return shell(
            "identity",
            "Who it answers for",
            '<div class="card"><div class="head"><span class="title">'
            "Identity</span></div><div class=pad>"
            '<p class="hint">These fill the <code>{{owner_name}}</code> style '
            "placeholders in the prompts, so the agent introduces itself "
            "correctly. Pronouns default to they/them, which is right for an "
            "unset value rather than merely neutral — nothing about a name tells "
            "you what someone uses, and this is spoken aloud to strangers.</p>"
            + "".join(fields)
            + '<div class="actions"><a class="btn" href="/setup/voice">'
            "Next: voice →</a></div></div></div>",
            note=flash(msg)
            or (
                "Identity is read from the environment at startup, so unlike the "
                "rest of the wizard these are shown for reference and changed in "
                "<code>.env</code> — a restart is needed either way."
            ),
        )

    # -- 2. voice ------------------------------------------------------------

    async def _voice_catalogue() -> list[tuple[str, str]]:
        """Voices for the dropdown, fetched live from the ElevenLabs account.

        In a thread: the provisioning helpers are deliberately stdlib/urllib so
        the CLI needs no dependencies, and this is a rare human-triggered action
        rather than anything on the call path.
        """
        key = cfg.elevenlabs_api_key
        if not key:
            return []
        voices = await asyncio.to_thread(list_voices, key)
        return [
            (
                v["id"],
                f"{v['name']} — {v['accent'] or 'unknown accent'}"
                + (f", {v['gender']}" if v["gender"] else ""),
            )
            for v in voices
        ]

    @admin.get("/setup/voice")
    async def voice(request: Request, msg: str = "") -> Response:
        if (bad := guard(request)) is not None:
            return bad

        provider = cfg.voice_provider
        ready = {name: cfg.provider_ready(name) for name, _ in CHOICES["voice_provider"]}

        # A provider that cannot answer a call is shown, but labelled and
        # refused on save. Hiding it would be worse - the reason it is
        # unavailable is exactly what the person needs to see.
        provider_options = [
            (value, label + ("" if ready[value][0] else f" — {ready[value][1]}"))
            for value, label in CHOICES["voice_provider"]
        ]

        blocks = [
            '<form method="post" action="/setup/voice">'
            '<div class="card"><div class="head"><span class="title">'
            "Which service answers the phone</span></div><div class=pad>"
            + select("Voice provider", "voice_provider", provider, provider_options,
                     "Everything else — the tools, the summaries, the channels — "
                     "is identical either way.")
            + '<div class="actions"><button>Save provider</button></div>'
            "</div></div></form>"
        ]

        # --- OpenAI ---
        # Models come from the account, not from a literal in this repo: a
        # hardcoded list is wrong the moment OpenAI ships something, and wrong
        # silently. Falls back to the static list only when there is no key yet.
        live_models = (
            await asyncio.to_thread(openai_realtime_models, cfg.openai_api_key)
            if cfg.openai_api_key
            else []
        )
        model_options = (
            [(m, m) for m in live_models] if live_models
            else CHOICES["openai_realtime_model"]
        )
        model_hint = (
            f"{len(live_models)} Realtime models on this account."
            if live_models
            else "Save a key and this list is read from your account."
        )

        blocks.append(
            '<form method="post" action="/setup/voice/openai">'
            '<div class="card"><div class="head"><span class="title">OpenAI '
            "Realtime</span>"
            + ('<span class="pill on">ready</span>' if ready["openai"][0]
               else f'<span class="pill off">{esc(ready["openai"][1])}</span>')
            + "</div><div class=pad>"
            + secret_field("API key", "openai_api_key",
                           "Checked for Realtime access before it is stored - it "
                           "is not enabled on every account.")
            + select("Model", "openai_realtime_model", cfg.openai_realtime_model,
                     model_options, model_hint)
            + '<div class="two">'
            + select("Voice", "openai_voice", cfg.openai_voice,
                     [(v, v.capitalize()) for v in OPENAI_VOICES])
            + select("Turn-taking", "vad_eagerness", cfg.vad_eagerness,
                     CHOICES["vad_eagerness"])
            + "</div>"
            + '<p class="hint">Voices are the one list here that is not read '
            "from the account — OpenAI publishes them in its docs and nowhere "
            "machine-readable, so this set is maintained by hand and may lag.</p>"
            + '<div class="actions"><button>Save OpenAI settings</button></div>'
            "</div></div></form>"
        )

        # --- ElevenLabs ---
        voices = await _voice_catalogue()
        agents = (
            await asyncio.to_thread(list_agents, cfg.elevenlabs_api_key)
            if cfg.elevenlabs_api_key
            else []
        )
        agent_options = [(a["id"], f"{a['name']} ({a['id'][:20]}…)") for a in agents]

        if cfg.elevenlabs_api_key:
            voice_control = (
                select("Voice", "elevenlabs_voice_id", cfg.elevenlabs_voice_id,
                       [("", "Use whatever the agent is set to")] + voices,
                       f"{len(voices)} voices on this account, British first.")
                if voices
                else '<p class="hint">Could not list voices — the key may be '
                     "missing <code>voices_read</code>.</p>"
            )
            agent_control = (
                select("Agent", "elevenlabs_agent_id", cfg.elevenlabs_agent_id,
                       [("", "None yet — provision one below")] + agent_options)
                if agent_options
                else '<p class="hint">No agents on this account yet. Provision '
                     "one below.</p>"
            )
        else:
            voice_control = agent_control = (
                '<p class="hint">Save a valid API key first, then the voice and '
                "agent lists are read from your account.</p>"
            )

        blocks.append(
            '<form method="post" action="/setup/voice/elevenlabs">'
            '<div class="card"><div class="head"><span class="title">ElevenLabs '
            "Agents</span>"
            + ('<span class="pill on">ready</span>' if ready["elevenlabs"][0]
               else f'<span class="pill off">{esc(ready["elevenlabs"][1])}</span>')
            + "</div><div class=pad>"
            + secret_field("API key", "elevenlabs_api_key",
                           "Needs convai_read and convai_write. The signed-URL "
                           "endpoint requires convai_write even though it is a GET.")
            + agent_control
            + voice_control
            + select("Language", "elevenlabs_language", cfg.elevenlabs_language,
                     [("", "Use whatever the agent is set to")]
                     + CHOICES["elevenlabs_language"])
            + '<div class="actions"><button>Save ElevenLabs settings</button></div>'
            "</div></div></form>"
        )

        # --- provisioning ---
        transfer_note = (
            "transfer_call will be included, because TRANSFER_ENABLED is on."
            if cfg.transfer_enabled and cfg.transfer_to_number
            else "transfer_call will be left out, because transfers are off. An "
                 "agent that cannot see the tool cannot be talked into using it."
        )
        blocks.append(
            '<form method="post" action="/setup/voice/provision">'
            '<div class="card"><div class="head"><span class="title">'
            "Provision the ElevenLabs agent</span></div><div class=pad>"
            '<p class="hint">An ElevenLabs agent carries its own tools, audio '
            "format and override permissions, and all three fail <em>silently</em> "
            "when wrong — a normal-sounding call that records nothing, an agent "
            "ignoring the caller's context, or white noise. This creates or "
            "updates them, then reads the agent back and checks. Safe to re-run, "
            "and you must re-run it whenever the tool list changes.</p>"
            f'<p class="hint">{esc(transfer_note)}</p>'
            + text("Agent name", "name", "ai-receptionist")
            + '<div class="actions"><button>'
            + ("Update the agent" if cfg.elevenlabs_agent_id else "Create the agent")
            + "</button></div></div></div></form>"
        )

        blocks.append(
            '<div class="actions"><a class="btn" href="/setup/phone">'
            "Next: phone line →</a></div>"
        )
        return shell("voice", "Voice", "".join(blocks), note=flash(msg))

    @admin.post("/setup/voice")
    async def save_provider(request: Request) -> Response:
        if (bad := guard(request)) is not None:
            return bad
        form = await request.form()
        chosen = str(form.get("voice_provider") or "").strip().lower()
        if chosen not in dict(CHOICES["voice_provider"]):
            return redirect("voice", "That is not a provider I know about.")

        ok, why = cfg.provider_ready(chosen)
        if not ok:
            # Refused rather than saved-with-a-warning: the failure would land on
            # a real caller's phone, not on this page.
            return redirect(
                "voice",
                f"Not switching to {chosen} — {why}. Fix that first and the "
                "option becomes selectable.",
            )

        error = persist(lambda data: data.setdefault("behaviour", {}).__setitem__(
            "voice_provider", chosen))
        log.info("admin set voice provider to %s", chosen)
        return redirect("voice", error or f"{chosen} will answer the next call.")

    @admin.post("/setup/voice/openai")
    async def save_openai(request: Request) -> Response:
        if (bad := guard(request)) is not None:
            return bad
        form = await request.form()

        supplied = str(form.get("openai_api_key") or "").strip()
        if supplied:
            # Checked before it is stored. A key without Realtime access fails at
            # the WebSocket handshake, by which point a caller is on the line.
            ok, message = await asyncio.to_thread(openai_check_key, supplied)
            if not ok:
                return redirect("voice", f"Key not saved — {message}")
            secrets.put("openai_api_key", supplied)

        values: dict = {}
        voice = str(form.get("openai_voice") or "").strip()
        if voice in OPENAI_VOICES:
            values["openai_voice"] = voice
        eagerness = str(form.get("vad_eagerness") or "").strip()
        if eagerness in dict(CHOICES["vad_eagerness"]):
            values["vad_eagerness"] = eagerness

        # Validated against the account rather than a literal, so a model that
        # exists but is not in this repo's static list is still accepted.
        model = str(form.get("openai_realtime_model") or "").strip()
        if model:
            allowed = await asyncio.to_thread(
                openai_realtime_models, cfg.openai_api_key
            )
            if model in allowed or model in dict(CHOICES["openai_realtime_model"]):
                values["openai_realtime_model"] = model
            else:
                return redirect(
                    "voice", f"{model} is not a Realtime model on this account."
                )

        error = persist(lambda data: data.setdefault("behaviour", {}).update(values))
        return redirect("voice", error or "OpenAI settings saved.")

    @admin.post("/setup/voice/elevenlabs")
    async def save_elevenlabs(request: Request) -> Response:
        if (bad := guard(request)) is not None:
            return bad
        form = await request.form()

        supplied = str(form.get("elevenlabs_api_key") or "").strip()
        if supplied:
            # Checked against the live API before it is stored, so a scope
            # problem is reported here rather than discovered by a caller.
            ok, message = await asyncio.to_thread(check_key, supplied)
            if not ok:
                return redirect("voice", f"Key not saved — {message}")
            secrets.put("elevenlabs_api_key", supplied)

        values: dict = {}
        agent_id = str(form.get("elevenlabs_agent_id") or "").strip()
        if agent_id or "elevenlabs_agent_id" in form:
            values["elevenlabs_agent_id"] = agent_id
        if "elevenlabs_voice_id" in form:
            values["elevenlabs_voice_id"] = str(
                form.get("elevenlabs_voice_id") or ""
            ).strip()
        language = str(form.get("elevenlabs_language") or "").strip()
        if language in dict(CHOICES["elevenlabs_language"]) or language == "":
            values["elevenlabs_language"] = language

        error = persist(lambda data: data.setdefault("behaviour", {}).update(values))
        return redirect("voice", error or "ElevenLabs settings saved.")

    @admin.post("/setup/voice/provision")
    async def run_provision(request: Request) -> Response:
        if (bad := guard(request)) is not None:
            return bad
        key = cfg.elevenlabs_api_key
        if not key:
            return redirect("voice", "Save an ElevenLabs API key first.")

        form = await request.form()
        name = str(form.get("name") or "ai-receptionist").strip() or "ai-receptionist"

        specs = list(TOOL_SPECS)
        if cfg.transfer_enabled and cfg.transfer_to_number:
            specs.append(TRANSFER_TOOL)

        lines: list[str] = []
        try:
            agent_id = await asyncio.to_thread(
                provision,
                key,
                specs,
                name=name,
                agent_id=cfg.elevenlabs_agent_id,
                language=cfg.elevenlabs_language or "en",
                voice_id=cfg.elevenlabs_voice_id,
                log_line=lines.append,
            )
        except Exception as exc:  # noqa: BLE001 - shown to a human, not swallowed
            detail = getattr(exc, "friendly", None) or f"{type(exc).__name__}: {exc}"
            log.error("provisioning failed: %s", detail)
            return redirect("voice", f"Provisioning failed — {detail}")

        persist(lambda data: data.setdefault("behaviour", {}).__setitem__(
            "elevenlabs_agent_id", agent_id))

        ok, checks = await asyncio.to_thread(verify, key, agent_id)
        summary = "; ".join(
            f"{'ok' if passed else 'FAILED'}: {label}" for label, passed, _ in checks
        )
        log.info("provisioned ElevenLabs agent %s (%s)", agent_id, summary)
        return redirect(
            "voice",
            f"Agent {agent_id} {'created' if not cfg.elevenlabs_agent_id else 'updated'} "
            f"and verified — {summary}."
            if ok
            else f"Agent {agent_id} written, but verification found a problem — "
                 f"{summary}.",
        )

    # -- 3. phone ------------------------------------------------------------

    @admin.get("/setup/phone")
    async def phone(request: Request, msg: str = "") -> Response:
        if (bad := guard(request)) is not None:
            return bad
        # Read from the account so the number is picked, not typed - E.164 is a
        # format people get wrong in several different ways, and a wrong number
        # here is a call that never reaches the service at all.
        numbers = await asyncio.to_thread(
            twilio_numbers, cfg.twilio_account_sid, cfg.twilio_auth_token
        )
        if numbers:
            number_control = select(
                "Phone number", "twilio_phone_number", cfg.twilio_phone_number,
                [("", "Not set")]
                + [(n, f"{n}{f'  —  {name}' if name else ''}") for n, name in numbers],
                f"{len(numbers)} number(s) on this account. This is the caller ID "
                "for callbacks and transfers.",
            )
        else:
            number_control = (
                '<p class="hint">Save working credentials above and the numbers '
                "on your account become a dropdown here.</p>"
            )

        body = (
            '<form method="post" action="/setup/phone">'
            '<div class="card"><div class="head"><span class="title">Twilio</span>'
            + ('<span class="pill on">configured</span>' if cfg.twilio_auth_token
               else '<span class="pill off">incomplete</span>')
            + "</div><div class=pad>"
            + secret_field("Account SID", "twilio_account_sid")
            + secret_field("Auth token", "twilio_auth_token",
                           "The account Auth Token, not an API key secret — only "
                           "the auth token signs webhooks, and the wrong one 403s "
                           "every call in a way that looks like a proxy fault.")
            + number_control
            + '<div class="actions"><button>Save Twilio settings</button></div>'
            "</div></div></form>"
            '<div class="card"><div class="head"><span class="title">'
            "Public hostname</span></div><div class=pad>"
            f"<p><code>{esc(cfg.public_base_url or 'not set')}</code></p>"
            '<p class="hint">Set in <code>.env</code> as '
            "<code>PUBLIC_BASE_URL</code>, and it must match the Twilio webhook "
            "character for character or every call fails signature validation. "
            "Not editable here for that reason — a trailing slash typed into a "
            "text box would break every call.</p></div></div>"
            + '<div class="actions"><a class="btn" href="/setup/notify">'
            "Next: notifications →</a></div>"
        )
        return shell("phone", "Phone line", body, note=flash(msg))

    @admin.post("/setup/phone")
    async def save_phone(request: Request) -> Response:
        if (bad := guard(request)) is not None:
            return bad
        form = await request.form()
        sid = str(form.get("twilio_account_sid") or "").strip()
        token = str(form.get("twilio_auth_token") or "").strip()

        if sid or token:
            # Verified together against the account, using whichever half is
            # already stored when only one was supplied.
            ok, message = await asyncio.to_thread(
                twilio_check,
                sid or cfg.twilio_account_sid,
                token or cfg.twilio_auth_token,
            )
            if not ok:
                return redirect("phone", f"Not saved — {message}")
            secrets.put_many(
                {"twilio_account_sid": sid, "twilio_auth_token": token}
            )

        number = str(form.get("twilio_phone_number") or "").strip()
        if "twilio_phone_number" in form:
            allowed = {
                n for n, _ in await asyncio.to_thread(
                    twilio_numbers, cfg.twilio_account_sid, cfg.twilio_auth_token
                )
            }
            if number and number not in allowed:
                return redirect(
                    "phone", f"{number} is not a number on this Twilio account."
                )
            persist(lambda data: data.setdefault("behaviour", {}).__setitem__(
                "twilio_phone_number", number))

        return redirect("phone", "Twilio settings saved.")

    # -- 4. notifications ----------------------------------------------------

    @admin.get("/setup/notify")
    async def notify(request: Request, msg: str = "") -> Response:
        if (bad := guard(request)) is not None:
            return bad
        live = sorted(deps["notifier"].channels()) if deps["notifier"] else []
        state = (
            f'<span class="pill on">{esc(", ".join(live))}</span>'
            if live
            else '<span class="pill off">nothing configured</span>'
        )
        body = (
            '<form method="post" action="/setup/notify">'
            '<div class="card"><div class="head"><span class="title">'
            f"Where call summaries go</span>{state}</div><div class=pad>"
            '<p class="hint">At least one, or the service answers calls that '
            "nobody is ever told about. Telegram is the only channel that can "
            "trigger a callback, because a reply carries the id of the message "
            "it answers.</p>"
            + secret_field("Telegram bot token", "telegram_bot_token")
            + secret_field("SMTP password", "smtp_password")
            + secret_field("Webhook auth header", "webhook_auth_header",
                           'Whole header, e.g. "Authorization: Bearer abc123".')
            + secret_field("WhatsApp bridge key", "whatsapp_bridge_key")
            + '<div class="actions"><button>Save credentials</button></div>'
            '<p class="hint">Addresses, chat ids and routing live on the '
            '<a href="/settings">Settings</a> page — this step is only the '
            "credentials.</p>"
            "</div></div></form>"
            + '<div class="actions"><a class="btn" href="/setup/finish">'
            "Next: finish →</a></div>"
        )
        return shell("notify", "Notifications", body, note=flash(msg))

    @admin.post("/setup/notify")
    async def save_notify(request: Request) -> Response:
        if (bad := guard(request)) is not None:
            return bad
        form = await request.form()
        secrets.put_many(
            {
                key: str(form.get(key) or "")
                for key in ("telegram_bot_token", "smtp_password",
                            "webhook_auth_header", "whatsapp_bridge_key")
            }
        )
        return redirect("notify", "Notification credentials saved.")

    # -- 5. finish -----------------------------------------------------------

    @admin.get("/setup/finish")
    async def finish(request: Request, msg: str = "") -> Response:
        if (bad := guard(request)) is not None:
            return bad

        provider = cfg.voice_provider
        provider_ok, provider_why = cfg.provider_ready(provider)
        channels = sorted(deps["notifier"].channels()) if deps["notifier"] else []
        missing = cfg.missing_required()

        checks = [
            ("A voice provider that can answer", provider_ok,
             f"{provider}" if provider_ok else f"{provider} — {provider_why}"),
            ("Public hostname set", bool(cfg.public_base_url),
             cfg.public_base_url or "PUBLIC_BASE_URL is empty"),
            ("Twilio webhook signing", bool(cfg.twilio_auth_token)
             or not cfg.validate_twilio_signature,
             "auth token present" if cfg.twilio_auth_token
             else "signature validation is off — only safe for local testing"),
            ("Media socket signing key", bool(cfg.stream_secret),
             "set" if cfg.stream_secret else "not set — generate one below"),
            ("Somewhere to send summaries", bool(channels),
             ", ".join(channels) or "no channel is delivering"),
        ]

        rows = "".join(
            f'<tr><td>{"✅" if ok else "❌"}</td><td>{esc(label)}</td>'
            f"<td><small>{esc(detail)}</small></td></tr>"
            for label, ok, detail in checks
        )
        everything = all(ok for _, ok, _ in checks) and not missing

        generate = (
            ""
            if cfg.stream_secret
            else '<form method="post" action="/setup/generate-secret">'
                 '<div class="actions"><button>Generate a signing key</button>'
                 "</div></form>"
        )

        body = (
            '<div class="card"><div class="head"><span class="title">Readiness'
            "</span>"
            + ('<span class="pill on">ready to take calls</span>' if everything
               else '<span class="pill off">not ready</span>')
            + "</div><div class=pad>"
            f'<table class="matrix"><tbody>{rows}</tbody></table>'
            + (f'<p class="hint">Still missing: <code>{esc(", ".join(missing))}'
               "</code></p>" if missing else "")
            + generate
            + '<p class="hint">Settings written here apply to the <em>next '
            "call</em> with no restart. The identity fields in step 1 are the "
            "exception — they are read from the environment at startup.</p>"
            '<div class="actions"><a class="btn" href="/settings">'
            "Go to full settings</a></div>"
            "</div></div>"
        )
        return shell("finish", "Finish", body, note=flash(msg))

    @admin.post("/setup/generate-secret")
    async def generate_secret(request: Request) -> Response:
        if (bad := guard(request)) is not None:
            return bad
        if cfg.stream_secret:
            # Rotating this mid-flight invalidates the token of any call
            # currently being set up, so it is only offered when there is none.
            return redirect("finish", "A signing key is already set.")
        if not secrets.generate_stream_secret():
            return redirect("finish", "Could not write it — is ./config read-only?")
        log.info("admin generated a new stream token secret")
        return redirect("finish", "Signing key generated.")

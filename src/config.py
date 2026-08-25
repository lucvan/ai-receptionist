"""Environment-driven configuration.

Every environment-specific value lives here and comes from the environment, so the
image itself is generic and the repo carries no real values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .secrets_store import SecretStore
from .settings import LiveConfig, Settings


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Config:
    # --- Service -------------------------------------------------------------
    port: int = field(default_factory=lambda: _int("PORT", 5050))
    public_base_url: str = field(
        default_factory=lambda: os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    )
    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO"))
    log_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("LOG_DIR", "/app/logs"))
    )

    # --- Voice provider ------------------------------------------------------
    # Which service actually answers the phone: "openai" (Realtime) or
    # "elevenlabs" (Agents). Everything downstream of the call - the tools, the
    # record, the supervisor, the notification channels, callbacks, transfers -
    # is identical either way, so this is a swap of the voice, the turn-taking
    # and the billing, not of what the receptionist does.
    #
    # Settable from the setup wizard, but only to a provider whose credentials
    # are already in place - the UI will not offer one that cannot answer a call.
    # That guard is the whole reason this is safe to expose: the risk was never
    # the swap itself, it was swapping to something unconfigured.
    voice_provider: str = field(
        default_factory=lambda: os.environ.get("VOICE_PROVIDER", "openai")
        .strip()
        .lower()
    )

    # --- OpenAI --------------------------------------------------------------
    openai_api_key: str = field(
        default_factory=lambda: os.environ.get("OPENAI_API_KEY", "")
    )
    openai_realtime_model: str = field(
        default_factory=lambda: os.environ.get(
            "OPENAI_REALTIME_MODEL", "gpt-realtime-mini"
        )
    )
    openai_voice: str = field(
        default_factory=lambda: os.environ.get("OPENAI_VOICE", "marin")
    )
    # How readily the agent decides the caller has finished speaking.
    # "low" waits longer and interrupts less; "high" replies faster.
    vad_eagerness: str = field(
        default_factory=lambda: os.environ.get("VAD_EAGERNESS", "auto")
    )
    # Passed to the model verbatim. Left to its own recall of the prompt it opens
    # with whatever it likes - observed "Hi there! How's your day going so far?",
    # which neither identifies the assistant nor asks anything useful.
    greeting: str = field(
        default_factory=lambda: os.environ.get(
            "GREETING", "Hello, you've reached the assistant — who am I speaking to?"
        )
    )

    # --- ElevenLabs ----------------------------------------------------------
    # Unlike a Realtime session, an ElevenLabs agent is a persistent object that
    # already carries its prompt, voice, LLM, turn-taking and - crucially - its
    # tools. This service overrides the prompt and the opening line per call and
    # leaves the rest to the agent, so most of the tuning that lives in env vars
    # for OpenAI lives in the ElevenLabs dashboard instead.
    #
    # `scripts/elevenlabs_setup.py` creates a correctly configured agent,
    # including the client tools and the override allowlist, and prints the id.
    # The admin UI's setup wizard runs the same code and writes the result here.
    elevenlabs_api_key: str = field(  # secret: env, or set write-only from the setup wizard
        default_factory=lambda: os.environ.get("ELEVENLABS_API_KEY", "")
    )
    elevenlabs_agent_id: str = field(
        default_factory=lambda: os.environ.get("ELEVENLABS_AGENT_ID", "")
    )
    # Optional per-call overrides. Empty means "whatever the agent is set to",
    # which is the sane default: the agent is the source of truth for its voice.
    elevenlabs_voice_id: str = field(
        default_factory=lambda: os.environ.get("ELEVENLABS_VOICE_ID", "")
    )
    elevenlabs_language: str = field(
        default_factory=lambda: os.environ.get("ELEVENLABS_LANGUAGE", "")
    )

    # --- Twilio --------------------------------------------------------------
    twilio_auth_token: str = field(
        default_factory=lambda: os.environ.get("TWILIO_AUTH_TOKEN", "")
    )
    twilio_account_sid: str = field(
        default_factory=lambda: os.environ.get("TWILIO_ACCOUNT_SID", "")
    )
    twilio_phone_number: str = field(
        default_factory=lambda: os.environ.get("TWILIO_PHONE_NUMBER", "")
    )
    # When false, /incoming-call accepts unsigned requests. Only for local testing.
    validate_twilio_signature: bool = field(
        default_factory=lambda: _bool("VALIDATE_TWILIO_SIGNATURE", True)
    )

    # --- Stream authentication ----------------------------------------------
    # Shared secret used to mint the per-call token handed to Twilio in the TwiML
    # <Stream> element and verified when the media WebSocket connects.
    stream_secret: str = field(
        default_factory=lambda: os.environ.get("STREAM_TOKEN_SECRET", "")
    )
    stream_token_ttl_s: int = field(
        default_factory=lambda: _int("STREAM_TOKEN_TTL_S", 120)
    )

    # --- Supervisor bridge ---------------------------------------------------
    # Any endpoint speaking the OpenAI chat-completions API. The HERMES_* names
    # are the originals, kept working because that is what existing installs
    # have in their .env; the generic names win when both are set. Same pattern
    # as SUPERVISOR_MIDCALL_TIMEOUT_S below.
    supervisor_url: str = field(
        default_factory=lambda: (
            os.environ.get("SUPERVISOR_URL")
            or os.environ.get("HERMES_SUPERVISOR_URL", "")
        ).rstrip("/")
    )
    supervisor_key: str = field(
        default_factory=lambda: os.environ.get("SUPERVISOR_KEY")
        or os.environ.get("HERMES_SUPERVISOR_KEY", "")
    )
    supervisor_model: str = field(
        default_factory=lambda: os.environ.get("SUPERVISOR_MODEL")
        or os.environ.get("HERMES_SUPERVISOR_MODEL", "")
        or "gpt-4o-mini"
    )
    # Turning a reply into a spoken callback script. Nobody is on a
    # line waiting for this, so it is not latency-critical. Falls back to the old
    # SUPERVISOR_MIDCALL_TIMEOUT_S name, which is all this value was ever used for
    # once mid-call consults were removed.
    supervisor_script_timeout_s: float = field(
        default_factory=lambda: float(
            os.environ.get("SUPERVISOR_SCRIPT_TIMEOUT_S")
            or os.environ.get("SUPERVISOR_MIDCALL_TIMEOUT_S")
            or "24"
        )
    )
    supervisor_final_timeout_s: float = field(
        default_factory=lambda: float(os.environ.get("SUPERVISOR_FINAL_TIMEOUT_S", "150"))
    )
    supervisor_enabled: bool = field(
        default_factory=lambda: _bool("SUPERVISOR_ENABLED", True)
    )

    # --- Caller recognition --------------------------------------------------
    # Number -> name map. Changes the agent's tone only; never its disclosure
    # rules, because caller ID is spoofable.
    contacts_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("CONTACTS_PATH", "/app/config/contacts.json")
        )
    )
    contacts_country_code: str = field(
        default_factory=lambda: os.environ.get("CONTACTS_COUNTRY_CODE", "44")
    )

    # Settings the admin UI may write. Lives beside contacts on the same
    # writable volume; absent on an env-only install, which is a supported state.
    settings_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("SETTINGS_PATH", "/app/config/settings.json")
        )
    )
    # Credentials the setup wizard writes. Separate file from settings.json so
    # that one stays safe to read, diff and paste into a bug report. Written
    # 0600; see secrets_store.py for why this exists and what it deliberately
    # does not cover.
    secrets_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("SECRETS_PATH", "/app/config/secrets.json")
        )
    )

    # --- Notification --------------------------------------------------------
    # Every value here is a *default*, overridable per channel from the admin UI
    # via config/settings.json - except the secrets, which are env-only and are
    # marked as such. See settings.py for why the line is drawn there.
    #
    # A bot dedicated to the receptionist, sending to one fixed chat. The
    # supervisor writes the summary; these are only delivery mechanisms.
    telegram_bot_token: str = field(  # secret: env, or set write-only from the setup wizard
        default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN", "")
    )
    telegram_chat_id: str = field(
        default_factory=lambda: os.environ.get("TELEGRAM_CHAT_ID", "")
    )

    # Email. Plain SMTP via the standard library - no extra dependency.
    smtp_host: str = field(default_factory=lambda: os.environ.get("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: _int("SMTP_PORT", 587))
    smtp_username: str = field(
        default_factory=lambda: os.environ.get("SMTP_USERNAME", "")
    )
    smtp_password: str = field(  # secret: env, or set write-only from the setup wizard
        default_factory=lambda: os.environ.get("SMTP_PASSWORD", "")
    )
    smtp_sender: str = field(default_factory=lambda: os.environ.get("SMTP_SENDER", ""))
    smtp_starttls: bool = field(default_factory=lambda: _bool("SMTP_STARTTLS", True))
    email_to: str = field(default_factory=lambda: os.environ.get("EMAIL_TO", ""))

    # Generic webhook. Covers ntfy, Gotify, Discord, Home Assistant, n8n and
    # anything else without an adapter per service.
    webhook_url: str = field(default_factory=lambda: os.environ.get("WEBHOOK_URL", ""))
    # "Header-Name: value", e.g. "Authorization: Bearer abc123".
    webhook_auth_header: str = field(  # secret: env, or set write-only from the setup wizard
        default_factory=lambda: os.environ.get("WEBHOOK_AUTH_HEADER", "")
    )

    # WhatsApp, through a self-hosted bridge paired from the admin UI. Pair a
    # dedicated number - see whatsapp.py for why that matters.
    whatsapp_bridge_url: str = field(
        default_factory=lambda: os.environ.get("WHATSAPP_BRIDGE_URL", "")
    )
    whatsapp_bridge_key: str = field(  # secret: env, or set write-only from the setup wizard
        default_factory=lambda: os.environ.get("WHATSAPP_BRIDGE_KEY", "")
    )
    whatsapp_flavour: str = field(
        default_factory=lambda: os.environ.get("WHATSAPP_FLAVOUR", "waha")
    )
    whatsapp_session: str = field(
        default_factory=lambda: os.environ.get("WHATSAPP_SESSION", "default")
    )
    whatsapp_to: str = field(
        default_factory=lambda: os.environ.get("WHATSAPP_TO", "")
    )

    # --- Callbacks -----------------------------------------------------------
    # Replying to a summary rings the caller back. This spends money
    # and disturbs a real person, so it is off unless explicitly enabled.
    callbacks_enabled: bool = field(
        default_factory=lambda: _bool("CALLBACKS_ENABLED", False)
    )
    # Telegram user ids allowed to trigger a callback. Chat id alone is not
    # sufficient - if the chat ever becomes a group, everyone in it could dial.
    telegram_allowed_user_ids: str = field(
        default_factory=lambda: os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")
    )
    callback_cooldown_s: int = field(
        default_factory=lambda: _int("CALLBACK_COOLDOWN_S", 300)
    )
    callback_max_per_hour: int = field(
        default_factory=lambda: _int("CALLBACK_MAX_PER_HOUR", 6)
    )

    # --- Transfer ------------------------------------------------------------
    # Putting a caller through to the owner's mobile. Off by default; when off the
    # agent is not given the tool at all, so it cannot be talked into using it.
    transfer_enabled: bool = field(
        default_factory=lambda: _bool("TRANSFER_ENABLED", False)
    )
    transfer_to_number: str = field(
        default_factory=lambda: os.environ.get("TRANSFER_TO_NUMBER", "")
    )
    # How long to ring before giving up and handing the caller back.
    transfer_timeout_s: int = field(
        default_factory=lambda: _int("TRANSFER_TIMEOUT_S", 20)
    )

    # --- Loop breaking -------------------------------------------------------
    # A caller who wants something the agent cannot give will ask several ways,
    # and prompt wording alone does not reliably make it stop. After this long,
    # or this many caller turns, with still no message recorded, the service
    # nudges it to wrap up. Belt and braces to the prompt, not a replacement.
    wrap_up_after_s: int = field(default_factory=lambda: _int("WRAP_UP_AFTER_S", 120))
    wrap_up_after_turns: int = field(
        default_factory=lambda: _int("WRAP_UP_AFTER_TURNS", 8)
    )

    # --- Caller history ------------------------------------------------------
    # Previous calls from the same number, read back out of the call log and put
    # in front of the agent so it does not start from nothing every time.
    history_enabled: bool = field(default_factory=lambda: _bool("HISTORY_ENABLED", True))
    history_max_calls: int = field(default_factory=lambda: _int("HISTORY_MAX_CALLS", 3))

    # --- Admin UI ------------------------------------------------------------
    # Contacts and call history. Served on its own port, published to 127.0.0.1
    # only, and reached over Tailscale - it must NEVER be proxied on the public
    # hostname, which has to stay open for Twilio's webhooks. Empty password
    # disables the UI entirely rather than serving it unauthenticated.
    admin_password: str = field(
        default_factory=lambda: os.environ.get("ADMIN_PASSWORD", "")
    )
    admin_port: int = field(default_factory=lambda: _int("ADMIN_PORT", 5051))
    # Must stay 0.0.0.0 *inside a container*: Docker publishes a port by
    # forwarding to the container's own interface, not its loopback, so binding
    # 127.0.0.1 here would make the published port unreachable rather than more
    # secure. The boundary for the Docker case is the compose publish
    # (`127.0.0.1:5051:5051`), which must not be widened.
    #
    # This exists for the deployment that is *not* in a container - run under
    # systemd or straight from uvicorn, where the process binds the host's
    # interfaces directly and loopback is both correct and necessary.
    admin_bind: str = field(
        default_factory=lambda: os.environ.get("ADMIN_BIND", "0.0.0.0")
    )

    # --- Call guards ---------------------------------------------------------
    max_call_seconds: int = field(default_factory=lambda: _int("MAX_CALL_SECONDS", 300))
    silence_hangup_seconds: int = field(
        default_factory=lambda: _int("SILENCE_HANGUP_SECONDS", 25)
    )

    # --- Data retention ------------------------------------------------------
    # Off by default and deliberately so: see README "Data minimisation".
    retain_transcripts: bool = field(
        default_factory=lambda: _bool("RETAIN_TRANSCRIPTS", False)
    )

    def provider_ready(self, name: str) -> tuple[bool, str]:
        """Could this provider answer a call right now? (ok, what is missing).

        The wizard uses this to decide whether a provider may be *selected* at
        all. Switching to one that has no credentials would leave the next real
        caller hearing "this number is not available", and that failure happens
        on someone else's phone rather than on the settings page - which is
        exactly the sort of thing a UI should refuse rather than allow and warn
        about.
        """
        if name == "elevenlabs":
            if not self.elevenlabs_agent_id:
                return False, "no agent yet - run the setup wizard"
            if not self.elevenlabs_api_key:
                return False, "no API key"
            return True, ""
        if name == "openai":
            if not self.openai_api_key:
                return False, "no API key"
            return True, ""
        return False, "unknown provider"

    @property
    def voice_model(self) -> str:
        """What is actually answering the phone, for logs and `/health`.

        One string rather than a provider field and a model field, because the
        two providers do not name the same thing: OpenAI has a model id, and an
        ElevenLabs deployment has an agent whose model is its own business.
        """
        if self.voice_provider == "elevenlabs":
            return f"elevenlabs:{self.elevenlabs_agent_id or 'unset'}"
        return self.openai_realtime_model

    @property
    def wss_stream_url(self) -> str:
        base = self.public_base_url
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        return f"{base}/media-stream"

    def missing_required(self) -> list[str]:
        """Names of settings the service cannot serve calls without.

        Notification used to be checked here, naming Telegram specifically. That
        left an email-only deployment permanently `degraded` and answering every
        inbound call with "not available right now", which is the wrong outcome
        for a correctly configured service.

        The rule behind that check is still right - answering calls nobody is
        told about is not a state to run in quietly - so it moved rather than
        went away. `/health` reports the live channel count from the notifier
        set, which is the honest form of the question: not "is Telegram set up"
        but "will anyone hear about this call".
        """
        missing = []
        if self.voice_provider == "elevenlabs":
            # The API key is not listed: an agent set to public can be reached
            # without one, which is a legitimate (if short-lived) state while
            # someone is proving the plumbing works. The agent id is not
            # optional - there is nothing to connect to without it.
            if not self.elevenlabs_agent_id:
                missing.append("ELEVENLABS_AGENT_ID")
        elif not self.openai_api_key:
            missing.append("OPENAI_API_KEY")
        if not self.public_base_url:
            missing.append("PUBLIC_BASE_URL")
        if not self.stream_secret:
            missing.append("STREAM_TOKEN_SECRET")
        if self.validate_twilio_signature and not self.twilio_auth_token:
            missing.append("TWILIO_AUTH_TOKEN")
        return missing


# The environment baseline, wrapped so that UI-editable keys are read live on
# every access. Call sites keep doing `config.greeting` and get the current
# value without knowing a settings file exists.
_base = Config()
settings = Settings(_base.settings_path)
secrets = SecretStore(_base.secrets_path)
config = LiveConfig(_base, settings, secrets)

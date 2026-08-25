"""Settings, channel selection, routing, and the admin settings page.

These cover the parts where a mistake is silent rather than loud: a call that is
answered and recorded but never delivered, a secret that leaks into a file meant
to be safe to edit, or a bad value in the settings file taking the service down
on the next call instead of falling back.

Run with `pytest` from the repo root. Nothing here touches the network, Twilio,
OpenAI, or a real SMTP server.
"""

from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient

BASE_ENV = {
    "OPENAI_API_KEY": "sk-test",
    "PUBLIC_BASE_URL": "https://example.test",
    "STREAM_TOKEN_SECRET": "0" * 64,
    "VALIDATE_TWILIO_SIGNATURE": "false",
    "ADMIN_PASSWORD": "hunter2",
}


@pytest.fixture()
def anyio_backend():
    """asyncio is the only backend in use; anyio needs this to run the async test."""
    return "asyncio"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A freshly configured service, isolated to a temp directory."""

    def configure(**extra):
        for key, value in {**BASE_ENV, **extra}.items():
            monkeypatch.setenv(key, str(value))
        monkeypatch.setenv("SETTINGS_PATH", str(tmp_path / "settings.json"))
        monkeypatch.setenv("CONTACTS_PATH", str(tmp_path / "contacts.json"))
        monkeypatch.setenv("LOG_DIR", str(tmp_path))

        import src.config

        # The env baseline is read once at import, so a test that changes the
        # environment has to rebuild it.
        importlib.reload(src.config)
        return src.config

    return configure


# --- the environment-only install keeps working ---------------------------


def test_no_settings_file_falls_back_to_the_environment(env):
    cfg = env(GREETING="Hello from the environment.")
    assert cfg.config.greeting == "Hello from the environment."
    assert cfg.config.wrap_up_after_turns == 8


def test_telegram_is_no_longer_required_to_serve_calls(env):
    """An email-only deployment must not sit permanently degraded.

    This used to list TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID, which made every
    inbound call answer "not available right now" on a correctly configured
    service that simply did not use Telegram.
    """
    cfg = env()
    assert cfg.config.missing_required() == []


def test_genuinely_missing_config_is_still_reported(env):
    cfg = env(OPENAI_API_KEY="", PUBLIC_BASE_URL="")
    missing = cfg.config.missing_required()
    assert "OPENAI_API_KEY" in missing
    assert "PUBLIC_BASE_URL" in missing


# --- settings override the environment, live ------------------------------


def test_settings_override_env_without_a_restart(env):
    cfg = env(GREETING="From the environment.")
    cfg.settings.save({"behaviour": {"greeting": "From the UI.", "history_max_calls": 5}})
    assert cfg.config.greeting == "From the UI."
    assert cfg.config.history_max_calls == 5
    # A key the UI has not written still comes from the environment.
    assert cfg.config.max_call_seconds == 300


def test_a_bad_value_falls_back_instead_of_breaking_a_call(env):
    cfg = env()
    cfg.settings.save({"behaviour": {"wrap_up_after_turns": "not-a-number"}})
    assert cfg.config.wrap_up_after_turns == 8


def test_config_is_read_only(env):
    cfg = env()
    with pytest.raises(AttributeError):
        cfg.config.greeting = "no"


# --- persona and prompt templating ----------------------------------------


def test_no_deployment_identifiers_are_shipped():
    """Nothing in the tracked tree names a real person, host or domain.

    The regression test for making this publishable. It was generalised from one
    household's deployment, and the failure mode is subtle: a hand-edit that puts
    a name back into a prompt, or a worked example that quotes a real hostname,
    reads perfectly fine to the person who wrote it and leaks to everyone else.

    Extend the list rather than deleting entries.
    """
    import pathlib
    import re
    import subprocess

    root = pathlib.Path(__file__).resolve().parent.parent

    # Only git-tracked files, because those are exactly what a publish ships.
    # A walk of the working tree would flag the operator's own contacts.json and
    # .env, which are gitignored and never leave the machine.
    try:
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")

    # Assembled from fragments so this file does not match its own denylist.
    banned = re.compile(
        "|".join(
            [
                r"\bl" + "uc\\b",
                r"\bl" + r"ucva\b",
                "sab" + "ina",
                "e49" + "ta",
                "taildbe" + "031",
                "lu" + "c-pc",
                "/opt/" + "vault",
                "8814" + "822367",
            ]
        ),
        re.IGNORECASE,
    )

    offenders = []
    for rel in tracked:
        path = root / rel
        if path.suffix in {".pyc", ".png", ".jpg"} or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for match in banned.finditer(text):
            line = text[: match.start()].count("\n") + 1
            offenders.append(f"{rel}:{line} {match.group(0)!r}")

    assert len(tracked) > 10, "git ls-files returned almost nothing"
    assert not offenders, "deployment identifiers found:\n  " + "\n  ".join(offenders)


def test_shipped_prompts_use_placeholders_not_names():
    """The two caller-facing prompts must be templated, not hand-edited."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    for name in ("receptionist.md", "callback.md"):
        text = (root / "prompts" / name).read_text(encoding="utf-8")
        assert "{{owner_name}}" in text or "{{assistant_name}}" in text, name


def test_prompt_does_not_ask_the_agent_to_greet():
    """The service speaks the opening line; the prompt must not also order one.

    Regression test for a real call: the prompt told the agent to "open with the
    greeting", which it cannot see and which the service had already delivered.
    The caller was greeted by name and then immediately greeted again with the
    generic line.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    text = (root / "prompts" / "receptionist.md").read_text(encoding="utf-8")

    opening = text.split("## Opening", 1)[1].split("\n## ", 1)[0].lower()
    # It must say the greeting is already done...
    assert "already been spoken" in opening
    # ...and must not instruct the agent to produce one itself.
    assert not re.search(r"^open with", opening, re.M), opening[:200]


def test_prompt_does_not_ask_the_agent_to_say_goodbye():
    """The service speaks the sign-off; the prompt must not also order one.

    Same shape as the doubled greeting, at the other end of the call. The prompt
    said "close warmly and briefly ... then call end_call", and the dispatcher
    then requested its own closing line - so callers heard two farewells stacked
    on whatever had already been said after take_message.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    text = (root / "prompts" / "receptionist.md").read_text(encoding="utf-8")
    ending = text.split("## Ending", 1)[1].lower()

    assert "do not say goodbye yourself" in ending
    # No instruction to compose a farewell before hanging up.
    assert not re.search(r"close warmly|say the closing line", ending), ending[:200]


def test_the_signoff_request_is_prescriptive():
    """A conditional in this instruction gets read as licence for a full farewell."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    source = (root / "src" / "realtime.py").read_text(encoding="utf-8")
    # The instruction is built from adjacent string literals across several
    # lines, so join them before matching on the sentence it actually sends.
    joined = re.sub(r'"\s*\n\s*"', "", source)

    assert "at most eight words" in joined
    assert "do not add a second sentence" in joined.lower()
    # The old conditional phrasing is what produced the rambling.
    assert "If you have already said goodbye" not in joined


def test_greeting_carries_the_configured_locale_not_a_hardcoded_accent():
    """`response.instructions` replaces the session prompt for that response.

    So the delivery guidance has to be repeated in the greeting call. It used to
    say "in your British accent" literally, which silently overrode LOCALE_NOTE
    for the one line every caller definitely hears.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    source = (root / "src" / "realtime.py").read_text(encoding="utf-8")
    assert "British accent" not in source
    assert "self._persona.locale_note" in source


def test_greeting_waits_for_the_session_to_be_applied():
    """Guards the fix for the voice changing mid-call.

    session.update is acknowledged asynchronously; a response.create that races
    it is generated with the default voice. The greeter must wait on
    session.updated rather than firing immediately after configure.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    source = (root / "src" / "realtime.py").read_text(encoding="utf-8")
    assert "_session_ready" in source
    assert "_greet_when_ready" in source
    # configure must not be immediately followed by the greeting call again.
    assert "await self._configure_session()\n                await self._greet()" not in source


def test_placeholders_are_substituted():
    from src.persona import Persona, render

    persona = Persona(
        owner_name="Sam",
        owner_them="them",
        owner_their="their",
        assistant_name="Sam's assistant",
        locale_note="Speak plainly.",
    )
    out = render(
        "You are {{owner_name}}'s receptionist. Pass it to {{owner_them}}.", persona
    )
    assert out == "You are Sam's receptionist. Pass it to them."


def test_an_unknown_placeholder_is_left_alone_not_blanked():
    """A typo must be loud. Blanking it would silently delete a sentence."""
    from src.persona import Persona, render

    persona = Persona("Sam", "them", "their", "Sam's assistant", "note")
    assert "{{ownr_name}}" in render("hello {{ownr_name}}", persona)


def test_pronouns_default_to_they_them(monkeypatch):
    for key in ("OWNER_PRONOUN_OBJECT", "OWNER_PRONOUN_POSSESSIVE", "ASSISTANT_NAME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OWNER_NAME", "Sam")
    from src.persona import Persona

    persona = Persona.from_env()
    assert persona.owner_them == "them"
    assert persona.owner_their == "their"
    assert persona.assistant_name == "Sam's assistant"


def test_an_unset_owner_name_still_produces_a_usable_persona(monkeypatch):
    """Unconfigured must degrade to odd-sounding, never to a crash on a live call."""
    monkeypatch.delenv("OWNER_NAME", raising=False)
    monkeypatch.delenv("ASSISTANT_NAME", raising=False)
    from src.persona import Persona

    persona = Persona.from_env()
    assert persona.owner_name
    assert persona.assistant_name


# --- the supervisor is optional and generically addressed -----------------


def test_legacy_hermes_env_names_still_work(env):
    """Existing installs have HERMES_* in their .env and must keep working."""
    cfg = env(
        HERMES_SUPERVISOR_URL="http://old:8645",
        HERMES_SUPERVISOR_KEY="oldkey",
        HERMES_SUPERVISOR_MODEL="receptionist",
    )
    assert cfg.config.supervisor_url == "http://old:8645"
    assert cfg.config.supervisor_key == "oldkey"
    assert cfg.config.supervisor_model == "receptionist"


def test_generic_names_win_when_both_are_set(env):
    cfg = env(
        HERMES_SUPERVISOR_URL="http://old:8645", SUPERVISOR_URL="http://new:11434",
        HERMES_SUPERVISOR_MODEL="receptionist", SUPERVISOR_MODEL="qwen2.5:14b",
    )
    assert cfg.config.supervisor_url == "http://new:11434"
    assert cfg.config.supervisor_model == "qwen2.5:14b"


def test_a_trailing_slash_on_the_url_does_not_double_up(env):
    """SUPERVISOR_URL is an origin; /v1/chat/completions is appended to it."""
    cfg = env(SUPERVISOR_URL="https://api.openai.com/")
    assert cfg.config.supervisor_url == "https://api.openai.com"


def test_supervisor_off_is_a_supported_state_not_a_missing_setting(env):
    from src.supervisor import SupervisorClient

    cfg = env(SUPERVISOR_ENABLED="false")
    assert cfg.config.missing_required() == []
    client = SupervisorClient(
        url=cfg.config.supervisor_url, api_key=cfg.config.supervisor_key,
        model=cfg.config.supervisor_model, enabled=cfg.config.supervisor_enabled,
    )
    assert client.enabled is False


@pytest.mark.anyio
async def test_a_disabled_supervisor_returns_no_summary_so_the_local_one_is_used(env):
    from src.supervisor import SupervisorClient

    cfg = env(SUPERVISOR_ENABLED="false")
    client = SupervisorClient("", "", "m", enabled=False)
    assert await client.deliver({"category": "unknown"}, 1.0) == (False, "")
    # And the callback script relays the reply verbatim rather than inventing one.
    script, _, note = await client.build_callback_script({}, "fri 2pm fine", 1.0)
    assert "fri 2pm fine" in script


# --- channel selection ----------------------------------------------------


def _notifier(cfg):
    from src.notify import NotifierSet

    return NotifierSet(cfg.config)


def test_channels_configured_only_in_env_are_used(env):
    cfg = env(
        TELEGRAM_BOT_TOKEN="123:AAtest",
        TELEGRAM_CHAT_ID="999",
        SMTP_HOST="smtp.example.test",
        SMTP_SENDER="bot@example.test",
        EMAIL_TO="me@example.test",
    )
    assert sorted(_notifier(cfg).channels()) == ["email", "telegram"]


def test_a_channel_disabled_in_settings_stops_delivering(env):
    cfg = env(TELEGRAM_BOT_TOKEN="123:AAtest", TELEGRAM_CHAT_ID="999")
    cfg.settings.save({"channels": {"telegram": {"enabled": False}}})
    assert sorted(_notifier(cfg).channels()) == []


def test_a_half_configured_channel_does_not_count(env):
    """Enabled but with nowhere to send is not a delivering channel."""
    cfg = env(SMTP_HOST="smtp.example.test")  # no recipient, no sender
    cfg.settings.save({"channels": {"email": {"enabled": True}}})
    assert "email" not in _notifier(cfg).channels()


# --- routing --------------------------------------------------------------


def test_a_category_routed_nowhere_is_suppressed_not_failed(env):
    """"Never tell me about spam" is a setting, not a delivery failure."""
    cfg = env(TELEGRAM_BOT_TOKEN="123:AAtest", TELEGRAM_CHAT_ID="999")
    cfg.settings.save(
        {"routing": {"default": ["telegram"], "spam_telesales": []}}
    )
    notifier = _notifier(cfg)
    assert notifier.suppressed("spam_telesales") is True
    assert notifier.suppressed("tradesperson_admin") is False


@pytest.mark.anyio
async def test_a_suppressed_category_reaches_no_channel(env):
    cfg = env(TELEGRAM_BOT_TOKEN="123:AAtest", TELEGRAM_CHAT_ID="999")
    cfg.settings.save({"routing": {"default": ["telegram"], "spam_telesales": []}})
    assert await _notifier(cfg).send("x", category="spam_telesales") == {}


def test_no_routing_table_means_every_enabled_channel(env):
    cfg = env(TELEGRAM_BOT_TOKEN="123:AAtest", TELEGRAM_CHAT_ID="999")
    assert _notifier(cfg).suppressed("anything") is False


# --- the summary itself ---------------------------------------------------


def test_summary_carries_no_markup_that_would_need_escaping():
    """The same string goes to every channel, so it must be plain text.

    Telegram is sent with no parse_mode precisely so a caller called
    "M&S_Delivery" cannot break the send; that only holds if nothing upstream
    introduces markup.
    """
    from src.notify import format_fallback

    text = format_fallback(
        {
            "category": "tradesperson_admin",
            "caller_name": "Dave",
            "company_or_relationship": "Dave's Plumbing",
            "reason": "Water under the boiler",
            "callback_number": "+447700900123",
        }
    )
    assert text.startswith("📞")
    assert "*" not in text and "_" not in text
    # The number goes last, on its own line, so it stays tappable.
    assert text.endswith("+447700900123")


def test_summary_prompt_demands_a_name_and_a_number():
    """Both were emergent rather than instructed, and both drifted.

    Across real calls the caller was named in 7 of 11 summaries and the number
    appeared in 9 of 11 - the prompt asked for prose and hoped. Naming is now an
    explicit rule, so it holds for the same reason the emoji always did.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    text = (root / "prompts" / "supervisor" / "summary.md").read_text(encoding="utf-8")

    assert "Say who rang" in text
    assert "known_contact_name" in text and "caller_name" in text
    assert "Give the number" in text
    # The spam carve-out must survive, or cold callers get a tappable number.
    assert "spam and telesales" in text


def test_fallback_names_a_known_contact():
    """The local path must agree with the supervisor prompt on naming."""
    from src.notify import format_fallback

    text = format_fallback(
        {"category": "tradesperson_admin", "known_contact_name": "Dana",
         "reason": "Running late", "callback_number": "+447700900123"}
    )
    assert "Dana" in text
    assert text.endswith("+447700900123")


def test_fallback_flags_a_name_mismatch_rather_than_picking_one():
    """A caller giving a different name from the saved one is worth seeing."""
    from src.notify import format_fallback

    text = format_fallback(
        {"category": "tradesperson_admin", "caller_name": "Dave",
         "known_contact_name": "Dana", "reason": "At the door"}
    )
    assert "Dave" in text and "Dana" in text


def test_spam_summaries_omit_the_callback_number():
    from src.notify import format_fallback

    text = format_fallback(
        {"category": "spam_telesales", "caller_name": "Solar",
         "callback_number": "+447700900123"}
    )
    assert "+447700900123" not in text


def test_subject_is_the_first_sentence_not_the_whole_paragraph():
    """format_fallback writes one paragraph, so "first line" is everything."""
    from src.notify import format_fallback, subject_line

    text = format_fallback(
        {"category": "tradesperson_admin", "caller_name": "Dave",
         "reason": "Water under the boiler", "callback_number": "+447700900123"}
    )
    subject = subject_line(text)
    assert subject.endswith(".")
    assert "Water under the boiler" not in subject
    assert len(subject) < 70


# --- the WhatsApp bridge --------------------------------------------------


@pytest.mark.parametrize(
    "flavour,session,expected_url,key,field",
    [
        ("waha", "default", "/api/sendText", "X-Api-Key", "chatId"),
        ("evolution", "inst", "/message/sendText/inst", "apikey", "number"),
    ],
)
def test_bridge_request_shapes(flavour, session, expected_url, key, field):
    from src.whatsapp import WhatsAppBridge

    bridge = WhatsAppBridge("http://bridge:3000", "k", flavour, session)
    url, body = bridge._send_request("+44 7700 900123", "hello")
    assert url.endswith(expected_url)
    assert "447700900123" in str(body[field])
    assert key in bridge._headers()


def test_custom_bridge_template_escapes_newlines_and_quotes():
    """A summary contains newlines; naive templating would produce broken JSON."""
    from src.whatsapp import WhatsAppBridge

    bridge = WhatsAppBridge(
        "http://bridge:9", "", "custom", "s",
        custom_send_path="/api/msg",
        custom_body='{"phone": "{to}", "message": "{text}"}',
    )
    _, body = bridge._send_request("+447700900123", 'one\ntwo "quoted"')
    assert body["message"] == 'one\ntwo "quoted"'
    assert body["phone"] == "447700900123"


# --- the admin settings page ----------------------------------------------


@pytest.fixture()
def client(env, tmp_path):
    def build(**extra):
        cfg = env(**extra)
        from src.admin import build_admin_app
        from src.history import CallHistory
        from src.notify import NotifierSet

        app = build_admin_app(
            password="hunter2",
            secret="s" * 32,
            contacts_path=tmp_path / "contacts.json",
            history=CallHistory(tmp_path),
            cfg=cfg.config,
            notifier=NotifierSet(cfg.config),
        )
        c = TestClient(app)
        c.post("/login", data={"password": "hunter2"}, follow_redirects=False)
        return c, cfg

    return build


def test_settings_page_requires_a_session(client):
    c, _ = client()
    c.cookies.clear()
    assert c.get("/settings", follow_redirects=False).status_code == 303


def test_settings_page_never_renders_a_secret(client):
    c, _ = client(TELEGRAM_BOT_TOKEN="123:AAsecret", TELEGRAM_CHAT_ID="999")
    body = c.get("/settings").text
    assert "AAsecret" not in body
    # Presence is shown instead, so a missing credential is still obvious.
    assert "TELEGRAM_BOT_TOKEN set" in body


def test_saving_routing_takes_effect_immediately(client):
    c, cfg = client(TELEGRAM_BOT_TOKEN="123:AAtest", TELEGRAM_CHAT_ID="999")
    c.post(
        "/settings/routing",
        data={"default:telegram": "on", "tradesperson_admin:telegram": "on"},
        follow_redirects=False,
    )
    assert cfg.settings.routing("spam_telesales") == []
    assert cfg.settings.routing("default") == ["telegram"]


def test_saving_behaviour_takes_effect_immediately(client):
    c, cfg = client()
    c.post(
        "/settings/behaviour",
        data={"greeting": "Hello, the practice.", "wrap_up_after_s": "90",
              "history_enabled": "on", "history_max_calls": "4"},
        follow_redirects=False,
    )
    assert cfg.config.greeting == "Hello, the practice."
    assert cfg.config.wrap_up_after_s == 90
    assert cfg.config.history_enabled is True


def test_an_unticked_checkbox_saves_as_false(client):
    """A checkbox absent from a POST means off, not "leave it alone"."""
    c, cfg = client(HISTORY_ENABLED="true")
    c.post("/settings/behaviour", data={"greeting": "x"}, follow_redirects=False)
    assert cfg.config.history_enabled is False


def test_a_nonsense_number_leaves_the_previous_value(client):
    c, cfg = client()
    c.post(
        "/settings/behaviour",
        data={"greeting": "x", "wrap_up_after_s": "banana"},
        follow_redirects=False,
    )
    assert cfg.config.wrap_up_after_s == 120


def test_saving_a_channel_never_writes_a_secret_to_disk(client, tmp_path):
    c, cfg = client(
        TELEGRAM_BOT_TOKEN="123:AAsecret", SMTP_PASSWORD="hunter2smtp",
        WHATSAPP_BRIDGE_KEY="bridgekey",
    )
    c.post(
        "/settings/channel/email",
        data={"enabled": "on", "to": "me@example.test",
              "host": "smtp.example.test", "port": "465",
              "sender": "bot@example.test"},
        follow_redirects=False,
    )
    written = (tmp_path / "settings.json").read_text(encoding="utf-8")
    for secret in ("AAsecret", "hunter2smtp", "bridgekey"):
        assert secret not in written
    assert json.loads(written)["channels"]["email"]["port"] == 465


def test_whatsapp_pairing_page_survives_an_unconfigured_bridge(client):
    c, _ = client()
    page = c.get("/settings/whatsapp")
    assert page.status_code == 200
    assert "No bridge URL set" in page.text

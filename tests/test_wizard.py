"""The setup wizard, and the credential store behind it.

This is the file that changed the project's security posture, so most of what is
here guards that change rather than the happy path:

- a stored credential must **never** be rendered back, on any page
- a blank field must mean "keep", not "delete"
- a provider with no credentials must not be selectable
- a message bounced through the redirect query string must be escaped

Nothing here touches the network. The wizard builds its dropdowns by asking the
live account what it offers, so every one of those lookups is stubbed by an
autouse fixture - which is also the only way to assert on what would have been
sent.

Run with `pytest` from the repo root.
"""

from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient

BASE_ENV = {
    "PUBLIC_BASE_URL": "https://example.test",
    "STREAM_TOKEN_SECRET": "0" * 64,
    "VALIDATE_TWILIO_SIGNATURE": "false",
    "OPENAI_API_KEY": "sk-from-env",
}

SECRET = "sk_supersecret_value_that_must_never_be_rendered"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    def configure(**extra):
        for key, value in {**BASE_ENV, **extra}.items():
            monkeypatch.setenv(key, str(value))
        monkeypatch.setenv("SETTINGS_PATH", str(tmp_path / "settings.json"))
        monkeypatch.setenv("SECRETS_PATH", str(tmp_path / "secrets.json"))
        monkeypatch.setenv("CONTACTS_PATH", str(tmp_path / "contacts.json"))
        monkeypatch.setenv("LOG_DIR", str(tmp_path))

        import src.config

        importlib.reload(src.config)
        return src.config

    return configure


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Stub every provider lookup the wizard makes.

    Autouse and not optional. The wizard builds its dropdowns by asking the live
    account what it offers, which is the right behaviour and completely wrong in
    a test: it would make the suite slow, flaky, dependent on someone's
    credentials, and capable of spending money. Individual tests override these
    to assert on what would have been sent.
    """
    import src.wizard as wizard

    monkeypatch.setattr(wizard, "openai_realtime_models", lambda key: [])
    monkeypatch.setattr(wizard, "openai_check_key", lambda key: (True, "ok"))
    monkeypatch.setattr(wizard, "twilio_numbers", lambda sid, token: [])
    monkeypatch.setattr(wizard, "twilio_check", lambda sid, token: (True, "ok"))
    monkeypatch.setattr(wizard, "list_voices", lambda key, limit=100: [])
    monkeypatch.setattr(wizard, "list_agents", lambda key: [])
    monkeypatch.setattr(wizard, "check_key", lambda key: (True, "ok"))


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
            secrets=cfg.secrets,
        )
        c = TestClient(app)
        c.post("/login", data={"password": "hunter2"}, follow_redirects=False)
        return c, cfg

    return build


# --- the store ------------------------------------------------------------


def test_a_stored_credential_overrides_the_environment(env):
    cfg = env(OPENAI_API_KEY="sk-from-env")
    assert cfg.config.openai_api_key == "sk-from-env"

    cfg.secrets.put("openai_api_key", "sk-from-ui")
    assert cfg.config.openai_api_key == "sk-from-ui"

    # Clearing falls back rather than blanking, so an env-only install is never
    # broken by having once touched the UI.
    cfg.secrets.clear("openai_api_key")
    assert cfg.config.openai_api_key == "sk-from-env"


def test_a_blank_field_keeps_the_stored_value(env):
    """The UI never renders a secret, so every field it draws starts empty.

    Treating that as a deletion would wipe a working credential on every save.
    """
    cfg = env()
    cfg.secrets.put("elevenlabs_api_key", SECRET)
    cfg.secrets.put_many({"elevenlabs_api_key": "", "twilio_auth_token": "tw"})

    assert cfg.config.elevenlabs_api_key == SECRET
    assert cfg.config.twilio_auth_token == "tw"


def test_unknown_keys_are_refused(env):
    cfg = env()
    assert cfg.secrets.put("admin_password", "letmein") is False
    assert cfg.secrets.get("admin_password") == ""


def test_the_generated_signing_key_is_not_guessable(env):
    cfg = env()
    cfg.secrets.generate_stream_secret()
    first = cfg.config.stream_secret
    assert len(first) == 64
    cfg.secrets.generate_stream_secret()
    assert cfg.config.stream_secret != first


def test_credentials_are_not_written_into_settings_json(env, tmp_path):
    """settings.json has to stay safe to read, diff and paste into a bug report."""
    cfg = env()
    cfg.secrets.put("elevenlabs_api_key", SECRET)
    cfg.settings.save({"behaviour": {"greeting": "hello"}})

    assert SECRET not in (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert SECRET in (tmp_path / "secrets.json").read_text(encoding="utf-8")


# --- write-only, which is the whole mitigation ----------------------------


@pytest.mark.parametrize(
    "path",
    ["/setup/identity", "/setup/voice", "/setup/phone", "/setup/notify",
     "/setup/finish", "/settings"],
)
def test_no_page_ever_renders_a_stored_credential(client, path):
    c, cfg = client()
    for key in ("openai_api_key", "elevenlabs_api_key", "twilio_auth_token",
                "telegram_bot_token", "smtp_password"):
        cfg.secrets.put(key, SECRET)

    body = c.get(path).text
    assert SECRET not in body
    # Presence is shown instead, so a missing credential is still obvious.
    assert "sk-from-env" not in body


def test_the_key_box_reports_presence_without_the_value(client):
    c, cfg = client()
    body = c.get("/setup/voice").text
    assert 'name="openai_api_key"' in body
    assert 'type="password"' in body
    # Set from the environment, and said so.
    assert "from OPENAI_API_KEY in .env" in body

    cfg.secrets.put("openai_api_key", SECRET)
    body = c.get("/setup/voice").text
    assert "from the UI" in body
    assert SECRET not in body


# --- access ---------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/setup", "/setup/voice", "/setup/finish"]
)
def test_every_wizard_page_requires_a_session(client, path):
    c, _ = client()
    c.cookies.clear()
    assert c.get(path, follow_redirects=False).status_code == 303


def test_wizard_posts_require_a_session(client):
    c, cfg = client()
    c.cookies.clear()
    response = c.post("/setup/notify", data={"telegram_bot_token": "stolen"},
                      follow_redirects=False)
    assert response.status_code == 303
    assert cfg.secrets.get("telegram_bot_token") == ""


# --- provider selection ---------------------------------------------------


def test_a_provider_without_credentials_cannot_be_selected(client):
    """The failure would land on a caller's phone, not on this page."""
    c, cfg = client(VOICE_PROVIDER="openai")
    assert cfg.config.provider_ready("elevenlabs") == (
        False, "no agent yet - run the setup wizard"
    )

    c.post("/setup/voice", data={"voice_provider": "elevenlabs"},
           follow_redirects=False)
    assert cfg.config.voice_provider == "openai"


def test_a_ready_provider_can_be_selected(client):
    c, cfg = client(ELEVENLABS_API_KEY="sk_x", ELEVENLABS_AGENT_ID="agent_1")
    c.post("/setup/voice", data={"voice_provider": "elevenlabs"},
           follow_redirects=False)
    assert cfg.config.voice_provider == "elevenlabs"


def test_an_unknown_provider_is_refused(client):
    c, cfg = client()
    c.post("/setup/voice", data={"voice_provider": "hal9000"},
           follow_redirects=False)
    assert cfg.config.voice_provider == "openai"


# --- key checking and provisioning ----------------------------------------


def test_a_key_that_fails_its_check_is_not_stored(client, monkeypatch):
    """A scope problem is reported here rather than discovered by a caller."""
    import src.wizard

    monkeypatch.setattr(
        src.wizard, "check_key",
        lambda key: (False, "The key is real but has no ConvAI permissions."),
    )
    c, cfg = client()
    response = c.post("/setup/voice/elevenlabs",
                      data={"elevenlabs_api_key": "sk_unscoped"},
                      follow_redirects=False)

    assert cfg.secrets.get("elevenlabs_api_key") == ""
    assert "no+ConvAI+permissions" in response.headers["location"].replace("%20", "+")


def test_a_good_key_is_stored(client, monkeypatch):
    import src.wizard

    monkeypatch.setattr(src.wizard, "check_key", lambda key: (True, "fine"))
    c, cfg = client()
    c.post("/setup/voice/elevenlabs", data={"elevenlabs_api_key": SECRET},
           follow_redirects=False)
    assert cfg.secrets.get("elevenlabs_api_key") == SECRET


def test_provisioning_records_the_agent_id_it_created(client, monkeypatch):
    import src.wizard

    seen = {}

    def fake_provision(key, specs, **kwargs):
        seen["key"] = key
        seen["tools"] = [s["name"] for s in specs]
        seen.update(kwargs)
        return "agent_new"

    monkeypatch.setattr(src.wizard, "provision", fake_provision)
    monkeypatch.setattr(src.wizard, "verify", lambda k, a: (True, []))

    c, cfg = client(ELEVENLABS_API_KEY="sk_x")
    c.post("/setup/voice/provision", data={"name": "front-desk"},
           follow_redirects=False)

    assert cfg.config.elevenlabs_agent_id == "agent_new"
    assert seen["name"] == "front-desk"
    # Transfer is off in this environment, so the tool is not provisioned - an
    # agent that cannot see it cannot be talked into using it.
    assert "transfer_call" not in seen["tools"]
    assert "take_message" in seen["tools"]


def test_provisioning_includes_transfer_only_when_transfers_are_on(client, monkeypatch):
    import src.wizard

    seen = {}
    monkeypatch.setattr(
        src.wizard, "provision",
        lambda key, specs, **kw: seen.update(tools=[s["name"] for s in specs])
        or "agent_t",
    )
    monkeypatch.setattr(src.wizard, "verify", lambda k, a: (True, []))

    c, _ = client(ELEVENLABS_API_KEY="sk_x", TRANSFER_ENABLED="true",
                  TRANSFER_TO_NUMBER="+447700900999")
    c.post("/setup/voice/provision", data={"name": "x"}, follow_redirects=False)
    assert "transfer_call" in seen["tools"]


def test_a_provisioning_failure_is_reported_not_swallowed(client, monkeypatch):
    import src.wizard

    def boom(*args, **kwargs):
        raise RuntimeError("the account is out of credits")

    monkeypatch.setattr(src.wizard, "provision", boom)
    c, cfg = client(ELEVENLABS_API_KEY="sk_x")
    response = c.post("/setup/voice/provision", data={"name": "x"},
                      follow_redirects=False)

    assert cfg.config.elevenlabs_agent_id == ""
    assert "out+of+credits" in response.headers["location"].replace("%20", "+")


# --- dropdowns ------------------------------------------------------------


def test_the_voice_step_is_dropdowns_not_text_boxes(client):
    c, _ = client()
    body = c.get("/setup/voice").text
    for name in ("voice_provider", "openai_voice", "openai_realtime_model",
                 "vad_eagerness", "elevenlabs_language"):
        assert f'<select name="{name}">' in body, name


def test_an_unknown_current_value_is_kept_as_an_option(client):
    """Otherwise saving an unrelated field silently rewrites it to the first
    entry in the list."""
    c, _ = client(OPENAI_VOICE="some-new-voice-openai-added")
    body = c.get("/setup/voice").text
    assert "some-new-voice-openai-added (current, not a known value)" in body


def test_the_elevenlabs_voice_dropdown_is_fetched_from_the_account(client, monkeypatch):
    import src.wizard

    monkeypatch.setattr(
        src.wizard, "list_voices",
        lambda key, limit=100: [
            {"id": "v_uk", "name": "Alice", "accent": "british", "gender": "female"}
        ],
    )
    monkeypatch.setattr(
        src.wizard, "list_agents",
        lambda key: [{"id": "agent_1", "name": "front desk"}],
    )
    c, _ = client(ELEVENLABS_API_KEY="sk_x")
    body = c.get("/setup/voice").text

    assert '<select name="elevenlabs_voice_id">' in body
    assert "Alice — british, female" in body
    assert '<select name="elevenlabs_agent_id">' in body
    assert "front desk" in body


# --- reflected input ------------------------------------------------------


def test_a_message_from_the_query_string_is_escaped(client):
    """It bounces back through a redirect, so it is attacker-controllable even
    on a loopback-only page."""
    c, _ = client()
    body = c.get("/setup/finish?msg=<img src=x onerror=alert(1)>").text
    assert "<img src=x" not in body
    assert "&lt;img src=x" in body


# --- readiness ------------------------------------------------------------


def test_the_finish_step_reports_readiness_rather_than_claiming_success(client):
    c, _ = client(OPENAI_API_KEY="", STREAM_TOKEN_SECRET="")
    body = c.get("/setup/finish").text
    assert "not ready" in body
    assert "Generate a signing key" in body


def test_generating_a_signing_key_is_refused_when_one_exists(client):
    """Rotating it mid-flight invalidates the token of any call being set up."""
    c, cfg = client()
    before = cfg.config.stream_secret
    c.post("/setup/generate-secret", follow_redirects=False)
    assert cfg.config.stream_secret == before


def test_generating_a_signing_key_works_when_there_is_none(client):
    c, cfg = client(STREAM_TOKEN_SECRET="")
    assert cfg.config.stream_secret == ""
    c.post("/setup/generate-secret", follow_redirects=False)
    assert len(cfg.config.stream_secret) == 64


def test_secrets_file_is_valid_json_after_several_writes(env, tmp_path):
    cfg = env()
    for index in range(5):
        cfg.secrets.put("telegram_bot_token", f"token-{index}")
    data = json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8"))
    assert data["telegram_bot_token"] == "token-4"


# --- live catalogues ------------------------------------------------------


def test_the_model_dropdown_is_read_from_the_account(client, monkeypatch):
    """A hardcoded list is wrong the moment the vendor ships something, and
    wrong silently. The first version of this shipped two models against an
    account that had ten."""
    import src.wizard as wizard

    monkeypatch.setattr(
        wizard, "openai_realtime_models",
        lambda key: ["gpt-realtime-2.1-mini", "gpt-realtime-2.1"],
    )
    c, _ = client()
    body = c.get("/setup/voice").text

    assert "gpt-realtime-2.1-mini" in body
    assert "2 Realtime models on this account" in body


def test_the_model_dropdown_falls_back_before_a_key_is_saved(client):
    c, _ = client(OPENAI_API_KEY="")
    body = c.get("/setup/voice").text
    assert "gpt-realtime-mini" in body
    assert "read from your account" in body


def test_a_model_not_on_the_account_is_refused(client, monkeypatch):
    import src.wizard as wizard

    monkeypatch.setattr(wizard, "openai_realtime_models", lambda key: ["gpt-realtime"])
    c, cfg = client(OPENAI_REALTIME_MODEL="gpt-realtime")
    response = c.post(
        "/setup/voice/openai",
        data={"openai_realtime_model": "gpt-imaginary"},
        follow_redirects=False,
    )
    assert cfg.config.openai_realtime_model == "gpt-realtime"
    assert "not+a+Realtime+model" in response.headers["location"].replace("%20", "+")


def test_an_openai_key_without_realtime_access_is_not_stored(client, monkeypatch):
    """It fails at the WebSocket handshake, by which point a caller is on the
    line hearing silence."""
    import src.wizard as wizard

    monkeypatch.setattr(
        wizard, "openai_check_key",
        lambda key: (False, "this account has no Realtime models"),
    )
    c, cfg = client()
    c.post("/setup/voice/openai", data={"openai_api_key": SECRET},
           follow_redirects=False)
    assert cfg.secrets.get("openai_api_key") == ""


def test_the_phone_number_is_picked_from_the_account(client, monkeypatch):
    import src.wizard as wizard

    monkeypatch.setattr(
        wizard, "twilio_numbers",
        lambda sid, token: [("+442079460958", "Reception")],
    )
    c, cfg = client(TWILIO_ACCOUNT_SID="AC1", TWILIO_AUTH_TOKEN="tok")
    body = c.get("/setup/phone").text
    assert '<select name="twilio_phone_number">' in body
    assert "Reception" in body

    c.post("/setup/phone", data={"twilio_phone_number": "+442079460958"},
           follow_redirects=False)
    assert cfg.config.twilio_phone_number == "+442079460958"


def test_a_number_not_on_the_account_is_refused(client, monkeypatch):
    import src.wizard as wizard

    monkeypatch.setattr(wizard, "twilio_numbers", lambda sid, token: [("+441", "a")])
    c, cfg = client(TWILIO_ACCOUNT_SID="AC1", TWILIO_AUTH_TOKEN="tok")
    response = c.post("/setup/phone", data={"twilio_phone_number": "+447999999999"},
                      follow_redirects=False)
    assert cfg.config.twilio_phone_number == ""
    assert "not+a+number" in response.headers["location"].replace("%20", "+")


def test_bad_twilio_credentials_are_not_stored(client, monkeypatch):
    """The common mistake is an API Key secret instead of the Auth Token; only
    the auth token signs webhooks."""
    import src.wizard as wizard

    monkeypatch.setattr(
        wizard, "twilio_check", lambda sid, token: (False, "Twilio rejected those.")
    )
    c, cfg = client()
    c.post("/setup/phone", data={"twilio_account_sid": "AC1",
                                 "twilio_auth_token": SECRET},
           follow_redirects=False)
    assert cfg.secrets.get("twilio_auth_token") == ""

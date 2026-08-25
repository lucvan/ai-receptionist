"""Choosing a voice provider, and what each one needs before it can answer.

The failure this guards against is a deployment that looks configured and is
not: a typo in VOICE_PROVIDER silently answering with the wrong service, or an
ElevenLabs install reporting itself healthy with no agent to connect to. Both
present as "it rang and something odd happened", which is expensive to diagnose
from a phone.

Run with `pytest` from the repo root. Nothing here touches the network.
"""

from __future__ import annotations

import importlib

import pytest

BASE_ENV = {
    "PUBLIC_BASE_URL": "https://example.test",
    "STREAM_TOKEN_SECRET": "0" * 64,
    "VALIDATE_TWILIO_SIGNATURE": "false",
}


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


# --- what each provider cannot answer a call without ----------------------


def test_openai_still_needs_its_key(env):
    cfg = env(VOICE_PROVIDER="openai", OPENAI_API_KEY="")
    assert "OPENAI_API_KEY" in cfg.config.missing_required()


def test_elevenlabs_needs_an_agent_id_not_an_openai_key(env):
    """An ElevenLabs install must not sit degraded for a key it never uses."""
    cfg = env(VOICE_PROVIDER="elevenlabs", OPENAI_API_KEY="", ELEVENLABS_AGENT_ID="")
    missing = cfg.config.missing_required()
    assert missing == ["ELEVENLABS_AGENT_ID"]

    ready = env(
        VOICE_PROVIDER="elevenlabs", OPENAI_API_KEY="", ELEVENLABS_AGENT_ID="agent_1"
    )
    assert ready.config.missing_required() == []


def test_a_public_agent_is_allowed_but_the_key_is_not_required_config(env):
    """Reachable without a key, deliberately - it is a legitimate first-run state.

    The service warns about it at startup instead of refusing to answer, because
    "we cannot take calls" is a worse outcome than "this agent is public".
    """
    cfg = env(
        VOICE_PROVIDER="elevenlabs", ELEVENLABS_AGENT_ID="agent_1", ELEVENLABS_API_KEY=""
    )
    assert cfg.config.missing_required() == []


def test_provider_is_case_and_whitespace_tolerant(env):
    cfg = env(VOICE_PROVIDER="  ElevenLabs  ", ELEVENLABS_AGENT_ID="agent_1")
    assert cfg.config.voice_provider == "elevenlabs"


# --- what /health reports -------------------------------------------------


def test_voice_model_names_the_thing_actually_answering(env):
    openai = env(VOICE_PROVIDER="openai", OPENAI_API_KEY="sk-x",
                 OPENAI_REALTIME_MODEL="gpt-realtime-mini")
    assert openai.config.voice_model == "gpt-realtime-mini"

    eleven = env(VOICE_PROVIDER="elevenlabs", ELEVENLABS_AGENT_ID="agent_1")
    assert eleven.config.voice_model == "elevenlabs:agent_1"


# --- picking the bridge ---------------------------------------------------


def _server(env, **extra):
    env(**extra)
    import src.server

    return importlib.reload(src.server)


def test_each_provider_selects_its_own_bridge(env):
    server = _server(env, VOICE_PROVIDER="elevenlabs", ELEVENLABS_AGENT_ID="agent_1")
    assert server.bridge_class().provider_name == "elevenlabs"

    server = _server(env, VOICE_PROVIDER="openai", OPENAI_API_KEY="sk-x")
    assert server.bridge_class().provider_name == "openai"


def test_an_unknown_provider_falls_back_loudly_rather_than_dropping_calls(env, caplog):
    """A typo must not stop the phone being answered."""
    server = _server(env, VOICE_PROVIDER="elevnlabs", OPENAI_API_KEY="sk-x")
    with caplog.at_level("ERROR"):
        assert server.bridge_class().provider_name == "openai"
    assert "unknown VOICE_PROVIDER" in caplog.text


def test_health_names_the_provider(env):
    from fastapi.testclient import TestClient

    server = _server(
        env,
        VOICE_PROVIDER="elevenlabs",
        ELEVENLABS_AGENT_ID="agent_1",
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="1",
    )
    with TestClient(server.app) as client:
        body = client.get("/health").json()

    assert body["voice_provider"] == "elevenlabs"
    assert body["model"] == "elevenlabs:agent_1"
    assert body["missing_config"] == []


# --- the admin settings page follows the provider -------------------------


def _admin(env, tmp_path, **extra):
    cfg = env(**extra)
    from src.admin import build_admin_app
    from src.history import CallHistory
    from src.notify import NotifierSet

    from fastapi.testclient import TestClient

    app = build_admin_app(
        password="hunter2",
        secret="s" * 32,
        contacts_path=tmp_path / "contacts.json",
        history=CallHistory(tmp_path),
        cfg=cfg.config,
        notifier=NotifierSet(cfg.config),
    )
    client = TestClient(app)
    client.post("/login", data={"password": "hunter2"}, follow_redirects=False)
    return client, cfg


def test_the_settings_page_shows_the_running_provider_s_fields(env, tmp_path):
    """Showing both sets would leave every deployment with boxes that do nothing.

    Voice *selection* is not here for ElevenLabs: the list has to be fetched from
    the account, so it lives in the wizard and this page links to it.
    """
    client, _ = _admin(env, tmp_path, VOICE_PROVIDER="elevenlabs",
                       ELEVENLABS_AGENT_ID="agent_1")
    body = client.get("/settings").text
    assert 'name="elevenlabs_language"' in body
    assert 'name="vad_eagerness"' not in body
    assert "agent_1" in body
    assert "/setup/voice" in body

    client, _ = _admin(env, tmp_path, VOICE_PROVIDER="openai", OPENAI_API_KEY="sk-x")
    body = client.get("/settings").text
    assert 'name="vad_eagerness"' in body
    assert 'name="elevenlabs_language"' not in body


def test_finite_settings_render_as_dropdowns_not_text_boxes(env, tmp_path):
    """A mistyped voice name does not fail on this page - it fails on a call."""
    client, _ = _admin(env, tmp_path, VOICE_PROVIDER="openai", OPENAI_API_KEY="sk-x")
    body = client.get("/settings").text

    for name in ("openai_voice", "vad_eagerness", "openai_realtime_model"):
        assert f'<select name="{name}">' in body, name
        assert f'<input type="text" name="{name}"' not in body, name


def test_a_hand_crafted_post_cannot_write_an_unknown_dropdown_value(env, tmp_path):
    """The form cannot produce this, so it only guards a forged POST - but the
    cost of accepting it is silence on a live call."""
    client, cfg = _admin(env, tmp_path, VOICE_PROVIDER="openai", OPENAI_API_KEY="sk-x",
                         OPENAI_VOICE="marin")
    client.post(
        "/settings/behaviour",
        data={"greeting": "x", "openai_voice": "not-a-real-voice"},
        follow_redirects=False,
    )
    assert cfg.config.openai_voice == "marin"


def test_saving_behaviour_does_not_wipe_the_other_provider_s_settings(env, tmp_path):
    """The form no longer renders every key, so a save must merge, not replace.

    Without this the OpenAI settings page would silently clear a saved
    ElevenLabs voice, and nobody would find out until they switched back.
    """
    client, cfg = _admin(env, tmp_path, VOICE_PROVIDER="openai", OPENAI_API_KEY="sk-x")
    cfg.settings.save({"behaviour": {"elevenlabs_voice_id": "voice_keepme"}})

    client.post(
        "/settings/behaviour",
        data={"greeting": "Hello.", "openai_voice": "marin", "history_enabled": "on"},
        follow_redirects=False,
    )

    assert cfg.config.greeting == "Hello."
    assert cfg.settings.behaviour("elevenlabs_voice_id", "") == "voice_keepme"


# --- both bridges present the same surface --------------------------------


def test_the_two_bridges_are_interchangeable_from_the_server_s_point_of_view(env):
    """Anything the server calls on a bridge has to exist on both.

    The server constructs one, awaits `run()`, then reads two attributes. If a
    provider-specific refactor ever moves one of those, this fails here rather
    than on a live call.
    """
    server = _server(env, OPENAI_API_KEY="sk-x")
    for bridge in server.BRIDGES.values():
        for attribute in ("run", "transfer_requested", "transfer_reason",
                          "provider_name", "is_outbound"):
            assert hasattr(bridge, attribute), (bridge.__name__, attribute)

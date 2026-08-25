"""Reading each provider's catalogue, and the credential checks built on them.

Pure parsing and ranking, with the HTTP layer stubbed. What matters here is the
filtering: a realtime *transcription* model in the agent dropdown is a call that
connects and never speaks, and a special-purpose model looks exactly like a
general one from its id alone.

Run with `pytest` from the repo root. Nothing here touches the network.
"""

from __future__ import annotations

import json

import pytest

from src import catalogs


@pytest.fixture()
def http(monkeypatch):
    """Stub the one function every catalogue lookup goes through."""
    calls: list[str] = []

    def install(status: int, payload):
        def fake(url, headers, timeout=20):
            calls.append(url)
            body = payload if isinstance(payload, str) else json.dumps(payload)
            return status, body

        monkeypatch.setattr(catalogs, "_get", fake)
        return calls

    return install


MODELS = {
    "data": [
        {"id": "gpt-4o"},
        {"id": "gpt-realtime"},
        {"id": "gpt-realtime-mini"},
        {"id": "gpt-realtime-2.1"},
        {"id": "gpt-realtime-mini-2025-12-15"},
        {"id": "gpt-realtime-whisper"},
        {"id": "gpt-realtime-translate"},
    ]
}


def test_only_realtime_models_are_offered(http):
    http(200, MODELS)
    models = catalogs.openai_realtime_models("sk-x")
    assert "gpt-4o" not in models
    assert "gpt-realtime-mini" in models


def test_special_purpose_realtime_models_are_excluded(http):
    """A transcription or translation model in this dropdown is a call that
    connects and then never speaks."""
    http(200, MODELS)
    models = catalogs.openai_realtime_models("sk-x")
    assert "gpt-realtime-whisper" not in models
    assert "gpt-realtime-translate" not in models


def test_pinned_snapshots_sort_after_plain_names(http):
    """`gpt-realtime-mini` is what most people want; the dated pin is for
    somebody who already knows why they need it."""
    http(200, MODELS)
    models = catalogs.openai_realtime_models("sk-x")
    assert models.index("gpt-realtime-mini") < models.index(
        "gpt-realtime-mini-2025-12-15"
    )


def test_no_key_means_no_lookup(http):
    calls = http(200, MODELS)
    assert catalogs.openai_realtime_models("") == []
    assert calls == []


def test_a_failed_lookup_is_empty_not_an_exception(http):
    """A dropdown that cannot be built should degrade to the static fallback,
    not take the settings page down."""
    http(500, "upstream exploded")
    assert catalogs.openai_realtime_models("sk-x") == []
    http(200, "not json at all")
    assert catalogs.openai_realtime_models("sk-x") == []


def test_a_rejected_openai_key_says_so(http):
    http(401, {"error": {"message": "bad key"}})
    ok, message = catalogs.openai_check_key("sk-x")
    assert ok is False
    assert "rejected" in message


def test_a_valid_key_without_realtime_access_is_reported_precisely(http):
    """The confusing case: the key is fine, the *account* lacks Realtime."""
    http(200, {"data": [{"id": "gpt-4o"}]})
    ok, message = catalogs.openai_check_key("sk-x")
    assert ok is False
    assert "no Realtime models" in message


def test_a_working_key_passes(http):
    http(200, MODELS)
    ok, _ = catalogs.openai_check_key("sk-x")
    assert ok is True


# --- Twilio ---------------------------------------------------------------


NUMBERS = {
    "incoming_phone_numbers": [
        {"phone_number": "+442079460958", "friendly_name": "Receptionist"},
        {"phone_number": "+15551234567", "friendly_name": ""},
        {"friendly_name": "malformed, no number"},
    ]
}


def test_twilio_numbers_are_listed_with_their_names(http):
    http(200, NUMBERS)
    numbers = catalogs.twilio_numbers("AC1", "tok")
    assert ("+442079460958", "Receptionist") in numbers
    # An entry with no number cannot be selected, so it is not offered.
    assert len(numbers) == 2


def test_twilio_needs_both_halves(http):
    calls = http(200, NUMBERS)
    assert catalogs.twilio_numbers("AC1", "") == []
    assert calls == []
    ok, message = catalogs.twilio_check("AC1", "")
    assert ok is False
    assert "both" in message.lower()


def test_the_api_key_secret_mistake_is_named(http):
    """Both look like credentials; only the auth token signs webhooks, and the
    wrong one 403s every call in a way that looks like a proxy fault."""
    http(401, "")
    ok, message = catalogs.twilio_check("AC1", "SK_not_the_auth_token")
    assert ok is False
    assert "API Key secret will not work" in message


def test_good_twilio_credentials_pass(http):
    http(200, {"sid": "AC1"})
    ok, _ = catalogs.twilio_check("AC1", "tok")
    assert ok is True

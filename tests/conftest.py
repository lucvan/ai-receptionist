"""Shared fixtures."""

import pytest


@pytest.fixture(autouse=True)
def no_live_model_list(monkeypatch):
    """The settings page reads the model list from OpenAI. Not in a test."""
    import src.admin

    monkeypatch.setattr(src.admin, "openai_realtime_models", lambda key: [])

"""Hermetic tests: decision/LLM keys are always empty here.

Live credentials in .env must never affect unit tests (no network, no spend).
Tests that need provider behavior inject fakes explicitly. See docs/24.
"""
import pytest


@pytest.fixture(autouse=True)
def _no_provider_keys(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    from app.core import config as config_mod
    monkeypatch.setattr(config_mod.settings, "TYPESAFE_API_KEY", "")
    monkeypatch.setattr(config_mod.settings, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(config_mod.settings, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config_mod.settings, "GROQ_API_KEY", "")
    monkeypatch.setattr(config_mod.settings, "TAVILY_API_KEY", "")

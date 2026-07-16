"""Guard against whitespace-corrupted credentials causing silent 401s.

Railway's Variables tab (and copy-paste in general) can leave a trailing
newline or leading/trailing spaces on a pasted key. Alpaca's API treats the
whitespace as part of the key, so the credential looks "SET" in the startup
health check yet still gets rejected with 401 Unauthorized. Strip whitespace
at load time so a stray newline can't cause this again.
"""

from __future__ import annotations

import importlib

import core.config as config


def _reload_with_env(monkeypatch, **env):
    for key in (
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "ALPACA_BASE_URL",
        "ANTHROPIC_API_KEY",
        "FMP_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    return importlib.reload(config)


def test_alpaca_api_key_strips_trailing_newline(monkeypatch):
    reloaded = _reload_with_env(monkeypatch, ALPACA_API_KEY="PKTESTKEY123\n")
    assert reloaded.ALPACA_API_KEY == "PKTESTKEY123"


def test_alpaca_secret_key_strips_surrounding_whitespace(monkeypatch):
    reloaded = _reload_with_env(monkeypatch, ALPACA_SECRET_KEY="  secretvalue \n")
    assert reloaded.ALPACA_SECRET_KEY == "secretvalue"


def test_fmp_api_key_strips_whitespace(monkeypatch):
    reloaded = _reload_with_env(monkeypatch, FMP_API_KEY="\tfmpkey\n")
    assert reloaded.FMP_API_KEY == "fmpkey"


def test_anthropic_api_key_strips_whitespace(monkeypatch):
    reloaded = _reload_with_env(monkeypatch, ANTHROPIC_API_KEY="sk-ant-123\n")
    assert reloaded.ANTHROPIC_API_KEY == "sk-ant-123"


def test_alpaca_base_url_strips_whitespace(monkeypatch):
    reloaded = _reload_with_env(monkeypatch, ALPACA_BASE_URL="https://paper-api.alpaca.markets\n")
    assert reloaded.ALPACA_BASE_URL == "https://paper-api.alpaca.markets"

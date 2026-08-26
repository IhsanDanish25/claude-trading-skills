"""AI-Trader publisher must stay a strict no-op until both AI_TRADER_ENABLED
and AI_TRADER_TOKEN are set, and must never raise into the trading path even
when the HTTP call fails — a broken publish sink cannot be allowed to block
or crash an order (same guarantee as core/trade_logger.py).
"""

from __future__ import annotations

import core.ai_trader_client as ai_trader_client

_ENTRY = {
    "ts": "2026-08-26T09:44:33-04:00",
    "symbol": "NVDA",
    "side": "buy",
    "qty": 2,
    "price": 120.50,
    "strategy": "pead",
}


def test_publish_trade_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(ai_trader_client, "_ENABLED", False)
    monkeypatch.setattr(ai_trader_client, "_TOKEN", "claw_test")

    called = {}
    monkeypatch.setattr(
        ai_trader_client.requests, "post", lambda *a, **k: called.setdefault("hit", True)
    )

    assert ai_trader_client.publish_trade(_ENTRY) is False
    assert "hit" not in called


def test_publish_trade_noop_when_no_token(monkeypatch):
    monkeypatch.setattr(ai_trader_client, "_ENABLED", True)
    monkeypatch.setattr(ai_trader_client, "_TOKEN", "")

    called = {}
    monkeypatch.setattr(
        ai_trader_client.requests, "post", lambda *a, **k: called.setdefault("hit", True)
    )

    assert ai_trader_client.publish_trade(_ENTRY) is False
    assert "hit" not in called


def test_publish_trade_sends_expected_payload(monkeypatch):
    monkeypatch.setattr(ai_trader_client, "_ENABLED", True)
    monkeypatch.setattr(ai_trader_client, "_TOKEN", "claw_test")

    sent = {}

    class FakeResp:
        status_code = 200
        text = ""

    def fake_post(url, headers=None, json=None, timeout=None):
        sent["url"] = url
        sent["headers"] = headers
        sent["json"] = json
        return FakeResp()

    monkeypatch.setattr(ai_trader_client.requests, "post", fake_post)

    assert ai_trader_client.publish_trade(_ENTRY) is True
    assert sent["url"].endswith("/api/signals/realtime")
    assert sent["headers"]["Authorization"] == "Bearer claw_test"
    assert sent["json"] == {
        "market": "us-stock",
        "action": "buy",
        "symbol": "NVDA",
        "price": 120.50,
        "quantity": 2,
        "content": "pead strategy",
        "executed_at": "2026-08-26T09:44:33-04:00",
    }


def test_publish_trade_maps_crypto_strategy_to_crypto_market(monkeypatch):
    monkeypatch.setattr(ai_trader_client, "_ENABLED", True)
    monkeypatch.setattr(ai_trader_client, "_TOKEN", "claw_test")

    sent = {}

    class FakeResp:
        status_code = 200
        text = ""

    monkeypatch.setattr(
        ai_trader_client.requests,
        "post",
        lambda url, headers=None, json=None, timeout=None: sent.update(json=json) or FakeResp(),
    )

    entry = {**_ENTRY, "symbol": "BTC/USD", "strategy": "crypto"}
    assert ai_trader_client.publish_trade(entry) is True
    assert sent["json"]["market"] == "crypto"


def test_publish_trade_returns_false_on_http_error(monkeypatch):
    monkeypatch.setattr(ai_trader_client, "_ENABLED", True)
    monkeypatch.setattr(ai_trader_client, "_TOKEN", "claw_test")

    class FakeResp:
        status_code = 401
        text = "invalid token"

    monkeypatch.setattr(
        ai_trader_client.requests, "post", lambda *a, **k: FakeResp()
    )

    assert ai_trader_client.publish_trade(_ENTRY) is False


def test_publish_trade_never_raises_on_network_error(monkeypatch):
    monkeypatch.setattr(ai_trader_client, "_ENABLED", True)
    monkeypatch.setattr(ai_trader_client, "_TOKEN", "claw_test")

    def raise_conn_error(*a, **k):
        raise ai_trader_client.requests.RequestException("boom")

    monkeypatch.setattr(ai_trader_client.requests, "post", raise_conn_error)

    assert ai_trader_client.publish_trade(_ENTRY) is False


def test_publish_trade_noop_on_incomplete_entry(monkeypatch):
    monkeypatch.setattr(ai_trader_client, "_ENABLED", True)
    monkeypatch.setattr(ai_trader_client, "_TOKEN", "claw_test")

    called = {}
    monkeypatch.setattr(
        ai_trader_client.requests, "post", lambda *a, **k: called.setdefault("hit", True)
    )

    incomplete = {"symbol": "NVDA", "side": "buy"}  # missing price/qty/ts
    assert ai_trader_client.publish_trade(incomplete) is False
    assert "hit" not in called

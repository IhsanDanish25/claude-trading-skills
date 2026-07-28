"""Regression: FMP 402 Payment Required is a billing outage, not a transient
rate-limit blip. Before this fix, every quote call re-hit a billing-disabled
FMP account and fell back to yfinance; it should instead disable FMP for the
day (no re-hitting a dead account, no per-tick warning spam) and fall back to
Alpaca-only quotes."""

from __future__ import annotations

import datetime

import core.fmp as fmp


class _Resp402:
    status_code = 402

    def json(self):
        return {}


def _reset_fmp_state(monkeypatch):
    monkeypatch.setattr(fmp, "_fmp_payment_required_day", None)
    monkeypatch.setattr(fmp, "_cache", {})
    monkeypatch.setattr(fmp, "FMP_API_KEY", "test-key")


def test_402_disables_fmp_for_today_and_falls_back_to_alpaca(monkeypatch):
    _reset_fmp_state(monkeypatch)

    calls = {"session_get": 0, "alpaca_quote": 0}

    class _Session:
        def get(self, *a, **k):
            calls["session_get"] += 1
            return _Resp402()

    monkeypatch.setattr(fmp, "_ensure_session", lambda: _Session())
    monkeypatch.setattr(fmp, "_fmp_session", _Session())
    monkeypatch.setattr(fmp, "_alpaca_quote",
                        lambda sym: calls.__setitem__("alpaca_quote", calls["alpaca_quote"] + 1)
                        or {"price": 123.45, "changesPercentage": 1.0})

    result = fmp.get_quote("AAPL")

    assert result == {"price": 123.45, "changesPercentage": 1.0}
    assert calls["session_get"] == 1
    assert calls["alpaca_quote"] == 1
    assert fmp._fmp_payment_required_day == datetime.date.today()


def test_billing_disabled_short_circuits_without_hitting_fmp_again(monkeypatch):
    _reset_fmp_state(monkeypatch)
    monkeypatch.setattr(fmp, "_fmp_payment_required_day", datetime.date.today())

    calls = {"session_get": 0}

    class _Session:
        def get(self, *a, **k):
            calls["session_get"] += 1
            return _Resp402()

    monkeypatch.setattr(fmp, "_ensure_session", lambda: _Session())
    monkeypatch.setattr(fmp, "_fmp_session", _Session())
    monkeypatch.setattr(fmp, "_alpaca_quote", lambda sym: {"price": 1.0, "changesPercentage": 0.0})

    result = fmp.get_quote("AAPL")

    assert result == {"price": 1.0, "changesPercentage": 0.0}
    # Already confirmed billing-disabled today — must not hit FMP again.
    assert calls["session_get"] == 0


def test_mark_fmp_payment_required_logs_once_per_day(monkeypatch, caplog):
    _reset_fmp_state(monkeypatch)
    import logging
    caplog.set_level(logging.ERROR, logger="core.fmp")

    fmp._mark_fmp_payment_required()
    fmp._mark_fmp_payment_required()

    error_records = [r for r in caplog.records if "402 Payment Required" in r.message]
    assert len(error_records) == 1

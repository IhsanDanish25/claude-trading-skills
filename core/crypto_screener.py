"""
Crypto momentum screener — BTC/USD, ETH/USD, SOL/USD via Alpaca.
Screens for 24h price momentum above threshold; returns ranked buy list.
"""
from __future__ import annotations

import datetime
import logging
import os

import pytz

log = logging.getLogger(__name__)

CRYPTO_UNIVERSE = ["BTC/USD", "ETH/USD", "SOL/USD"]
MOMENTUM_THRESHOLD = float(os.environ.get("CRYPTO_MOMENTUM_PCT", "3.0"))


def _client():
    from alpaca.data.historical import CryptoHistoricalDataClient
    return CryptoHistoricalDataClient(
        os.environ.get("ALPACA_API_KEY", ""),
        os.environ.get("ALPACA_SECRET_KEY", ""),
    )


def screen() -> list[dict]:
    """Return crypto symbols with 24h momentum >= CRYPTO_MOMENTUM_PCT, ranked by score."""
    from alpaca.data.requests import CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame

    end = datetime.datetime.now(pytz.UTC)
    start = end - datetime.timedelta(days=3)

    try:
        resp = _client().get_crypto_bars(
            CryptoBarsRequest(
                symbol_or_symbols=CRYPTO_UNIVERSE,
                timeframe=TimeFrame.Hour,
                start=start,
                end=end,
            )
        )
    except Exception as e:
        log.error("Crypto screener: bars fetch failed: %s", e)
        return []

    candidates = []
    for sym in CRYPTO_UNIVERSE:
        try:
            df = resp[sym].df
            if df is None or len(df) < 25:
                log.warning("Crypto %s: only %d bars — skipping", sym, len(df) if df is not None else 0)
                continue

            price_now = float(df["close"].iloc[-1])
            price_24h = float(df["close"].iloc[-25])
            momentum_pct = (price_now - price_24h) / price_24h * 100

            vol_now = float(df["volume"].iloc[-24:].mean())
            vol_prev = float(df["volume"].iloc[-48:-24].mean()) if len(df) >= 48 else vol_now
            vol_ratio = vol_now / vol_prev if vol_prev > 0 else 1.0

            score = round(momentum_pct * 8 + max(0, vol_ratio - 1) * 5, 1)
            log.info("Crypto %s: 24h=%+.2f%% vol×%.2f score=%.0f", sym, momentum_pct, vol_ratio, score)

            if momentum_pct >= MOMENTUM_THRESHOLD:
                candidates.append({
                    "symbol": sym,
                    "price": price_now,
                    "momentum_pct": round(momentum_pct, 2),
                    "vol_ratio": round(vol_ratio, 2),
                    "score": round(score, 0),
                    "reason": f"24h {momentum_pct:+.1f}% vol×{vol_ratio:.1f}",
                })
        except Exception as e:
            log.warning("Crypto %s: error %s", sym, e)

    candidates.sort(key=lambda x: x["score"], reverse=True)
    log.info("Crypto screen: %d/%d symbols pass momentum >= %.1f%%",
             len(candidates), len(CRYPTO_UNIVERSE), MOMENTUM_THRESHOLD)
    return candidates

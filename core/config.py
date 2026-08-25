"""
Central config — reads from env vars (Railway secrets) or .env file locally.
"""

import os
import sys

import pytz

ET = pytz.timezone("America/New_York")

try:
    from dotenv import load_dotenv

    # Load .env from repo root regardless of working directory.
    # override=True makes the local .env authoritative so a stale or wrong
    # ALPACA_API_KEY exported in the shell (or inherited from a polluted launch
    # environment) can't silently shadow the correct key and cause 401s. This is
    # a no-op on Railway, where .env is gitignored and never present in the
    # nixpacks image — Railway's injected secrets remain the only source there.
    _dotenv = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    load_dotenv(_dotenv, override=True)
except ImportError:
    pass

# ── Alpaca ────────────────────────────────────────────────────────────────────
# .strip() guards against a trailing newline or stray space on a pasted
# Railway variable — the whitespace becomes part of the key, the health
# check still reports "SET", and Alpaca rejects it with a 401 that looks
# like a bad/expired key.
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "").strip()
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "").strip()
ALPACA_BASE_URL = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets").strip()
PAPER_TRADE = (
    os.environ.get("ALPACA_PAPER_TRADE", os.environ.get("ALPACA_PAPER", "true")).strip().lower()
    == "true"
)

# Dry-run mode: runs the full live pipeline (signal, spread check, cost
# tracker) against real market data, but BrokerClient.buy() short-circuits
# right before order submission — no order ever reaches Alpaca.
# Introduced because the paper trading account was deleted (2026-08-01), so
# this is the only way to validate the pipeline end-to-end without risking
# real capital. Set DRY_RUN=true in Railway secrets, run for a few trading
# days, confirm clean logs, then unset before real orders resume.
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() == "true"

# ── Anthropic ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

# ── FMP ───────────────────────────────────────────────────────────────────────
FMP_API_KEY = os.environ.get("FMP_API_KEY", "").strip()

_REQUIRED = {
    "ALPACA_API_KEY": ALPACA_API_KEY,
    "ALPACA_SECRET_KEY": ALPACA_SECRET_KEY,
    "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
}


def validate() -> None:
    missing = [name for name, val in _REQUIRED.items() if not val]
    if missing:
        msg = (
            "Missing required environment variables (set them in Railway secrets):\n  "
            + "\n  ".join(missing)
        )
        print(msg, file=sys.stderr)
        raise RuntimeError(msg)


# ── Trading params ────────────────────────────────────────────────────────────
MAX_POSITION_SIZE_PCT = float(
    os.environ.get("MAX_POSITION_SIZE_PCT", os.environ.get("MAX_POSITION_PCT", "0.08"))
)
MAX_OPEN_POSITIONS = int(
    os.environ.get("MAX_OPEN_POSITIONS", os.environ.get("MAX_POSITIONS", "12"))
)
STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PCT", "0.02"))
TAKE_PROFIT_PCT = float(os.environ.get("TAKE_PROFIT_PCT", "0.06"))
# Minimum Claude confidence score (0-100) for midday_review's scan-buy path
# to treat a VCP candidate as a real buy signal.
MIDDAY_BUY_SCORE_MIN = float(os.environ.get("MIDDAY_BUY_SCORE_MIN", "70"))
MIN_RELATIVE_VOLUME = float(os.environ.get("MIN_REL_VOL", "1.5"))
MIN_PRICE = float(os.environ.get("MIN_PRICE", "5.0"))
MAX_PRICE = float(os.environ.get("MAX_PRICE", "100.0"))
MIN_COMPOSITE_SCORE = int(os.environ.get("MIN_COMPOSITE_SCORE", "20"))
RISK_PCT = float(os.environ.get("RISK_PCT", "0.0125"))
MAX_SPREAD_PCT = float(os.environ.get("MAX_SPREAD_PCT", "0.02"))  # wide-spread guard in get_price

# ── Edge upgrades ─────────────────────────────────────────────────────────────
ENTRY_DELAY_MIN = int(os.environ.get("ENTRY_DELAY_MIN", "20"))
MIN_RS_RATING = float(os.environ.get("MIN_RS_RATING", "0.0"))
BREAKOUT_VOL_MULT = float(os.environ.get("BREAKOUT_VOL_MULT", "1.5"))
PARTIAL_PROFIT_PCT = float(os.environ.get("PARTIAL_PROFIT_PCT", "0.06"))
PARTIAL_PROFIT_SIZE = float(os.environ.get("PARTIAL_PROFIT_SIZE", "0.5"))
TRAIL_STOP_PCT = float(os.environ.get("TRAIL_STOP_PCT", "0.04"))
FTD_DEFENSIVE_SIZE = float(os.environ.get("FTD_DEFENSIVE_SIZE", "0.025"))
ALLOW_FTD_BOTTOM_BUY = os.environ.get("ALLOW_FTD_BOTTOM_BUY", "true").lower() == "true"
STRONG_SECTORS_ONLY = os.environ.get("STRONG_SECTORS_ONLY", "true").lower() == "true"

# ── Edge pack 2 ───────────────────────────────────────────────────────────────
MAX_GAP_PCT = float(os.environ.get("MAX_GAP_PCT", "5.0"))
EARNINGS_BLACKOUT_DAYS = int(os.environ.get("EARNINGS_BLACKOUT_DAYS", "3"))
MAX_PER_SECTOR = int(os.environ.get("MAX_PER_SECTOR", "2"))
ALLOW_PYRAMIDING = os.environ.get("ALLOW_PYRAMIDING", "true").lower() == "true"
PYRAMID_TRIGGER_PCT = float(os.environ.get("PYRAMID_TRIGGER_PCT", "0.03"))
CIRCUIT_BREAKER_PCT = float(os.environ.get("CIRCUIT_BREAKER_PCT", "0.05"))
TRAIL_INTRADAY = os.environ.get("TRAIL_INTRADAY", "true").lower() == "true"

# ── State dir ─────────────────────────────────────────────────────────────────
STATE_DIR = os.environ.get(
    "STATE_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state")
)
os.makedirs(STATE_DIR, exist_ok=True)

# ── Watchlist (VCP universe) ──────────────────────────────────────────────────
WATCHLIST = [
    "AAPL",
    "MSFT",
    "NVDA",
    "AMD",
    "META",
    "GOOGL",
    "AMZN",
    "TSLA",
    "NFLX",
    "CRM",
    "ADBE",
    "PANW",
    "CRWD",
    "SNOW",
    "DDOG",
    "MELI",
    "SQ",
    "SHOP",
    "NET",
    "ZS",
    "CELH",
    "ENPH",
    "FSLR",
    "ON",
    "AEHR",
    "SMCI",
    "AXON",
    "COCO",
    "DUOL",
    "PINS",
]

# ── Strategy mode (comma-separated, run in order listed) ─────────────────────
# Supported: pead, meanrev, insider, squeeze, breakout, earnmom, gapfill, momentum, sector, vcp, macross
# macross (MA crossover, trend-following) added 2026-08 — opt-in, unvalidated
# until its own standalone backtest clears the same bar as breakout/meanrev/
# earnmom (see backtest_5_strategies.py). Do not add to the default list below.
# Recommended: STRATEGY_MODE=breakout,meanrev,earnmom,insider
#
# 2026-08-06 reconciliation (backtest_5_strategies.py, fixed sizing per
# BACKTEST_MAX_POSITION_PCT — see backtest_harness/earnings_engine.py for
# why sizing must be pinned, not read from live config):
#   Breakout: 200 trades, Sharpe 1.08, p=0.0097, TRUSTWORTHY
#   Mean Reversion: 573 trades, Sharpe 0.96, p=0.0223, TRUSTWORTHY
#   Earnings Momentum: 217 trades, Sharpe 0.96, p=0.0224, TRUSTWORTHY
#   PEAD: DROPPED — fails significance under both tested methodologies
#     (158 trades p=0.262; 871 trades p=0.659, also fails overfit gate).
#     A previously-cited PEAD result (Sharpe 1.28/p=0.017/152 trades) could
#     not be reproduced anywhere in this repo — treat as unverified.
#   Insider: kept — runs correctly live via SEC EDGAR (zero egress/API-key
#     issues), but its OWN backtest is blocked (FMP /stable/insider-trading
#     is paid-tier, 402 on the free plan; no historical EDGAR puller exists
#     yet). Unvalidated by backtest, not by live execution — see
#     docs/dev/strategy-validation-status.md.
#   Squeeze: dropped from the default per this reconciliation — not in the
#     kept set. Its own backtest is blocked the same way Insider's is (FMP
#     short-interest paid-tier), but unlike Insider its live path (core/
#     short_interest.py) pulls from yfinance, not FMP — not independently
#     verified as broken, just not part of what was kept here. Revisit
#     separately if you want it back in. Sector/momentum/gapfill/vcp remain
#     opt-in, unvalidated.
# None of the four beat SPY buy-and-hold on this window — these are
# risk-adjusted edges (low drawdown), not return-beaters. Size accordingly.
_STRATEGY_RAW = os.environ.get("STRATEGY_MODE", "breakout,meanrev,earnmom,insider").lower()
STRATEGY_MODES = [s.strip() for s in _STRATEGY_RAW.split(",") if s.strip()]

# ── PEAD params ───────────────────────────────────────────────────────────────
PEAD_HOLD_DAYS = int(os.environ.get("PEAD_HOLD_DAYS", "60"))
PEAD_STOP_PCT = float(os.environ.get("PEAD_STOP_PCT", "0.15"))
PEAD_LOOKBACK_DAYS = int(os.environ.get("PEAD_LOOKBACK_DAYS", "7"))
PEAD_MIN_SURPRISE_PCT = float(os.environ.get("PEAD_MIN_SURPRISE_PCT", "10.0"))
PEAD_MIN_PRICE = float(os.environ.get("PEAD_MIN_PRICE", "10.0"))
# Small live account (~$268 equity, 8% position cap ≈ $21/slot) can't afford
# whole shares above ~$25 — cap the screener's own price range so it surfaces
# affordable names instead of ranking by surprise% alone and getting filtered
# out downstream by _affordable_candidates() after burning the run on nothing.
PEAD_MAX_PRICE = float(os.environ.get("PEAD_MAX_PRICE", "25.0"))
PEAD_MIN_AVG_VOLUME = float(os.environ.get("PEAD_MIN_AVG_VOLUME", "500000"))
PEAD_SIZE_PCT = float(os.environ.get("PEAD_SIZE_PCT", "0.05"))

# ── MeanRev params (RSI oversold + Bollinger pullback + above SMA200) ─────────
# RSI_THRESHOLD 35: catches more setups in bull markets without being sloppy
# (30 was too strict — produced 0 candidates every day in the current rally)
# BB_THRESHOLD 2.0: allow price up to 2% above lower BB (band touch is rare
# on large-caps; $0.00 buffer meant 0 candidates even when stocks were clearly
# oversold and just above the band)
MEANREV_HOLD_DAYS = int(os.environ.get("MEANREV_HOLD_DAYS", "14"))
MEANREV_STOP_PCT = float(os.environ.get("MEANREV_STOP_PCT", "0.05"))
MEANREV_SIZE_PCT = float(os.environ.get("MEANREV_SIZE_PCT", "0.03"))
MEANREV_MIN_PRICE = float(os.environ.get("MEANREV_MIN_PRICE", "10.0"))
# Same affordability rationale as PEAD_MAX_PRICE above — keep the screener's
# range inside what an ~$21/slot account can actually buy as a whole share.
MEANREV_MAX_PRICE = float(os.environ.get("MEANREV_MAX_PRICE", "25.0"))
MEANREV_RSI_THRESHOLD = float(os.environ.get("MEANREV_RSI_THRESHOLD", "35.0"))
MEANREV_BB_THRESHOLD = float(os.environ.get("MEANREV_BB_THRESHOLD", "2.0"))
# dollar buffer above lower BB: 0.0 = price must be at/below the band exactly;
# 2.0 = allow price up to $2 above lower band (band touch is rare on daily closes)
MEANREV_MIN_AVG_VOLUME = float(os.environ.get("MEANREV_MIN_AVG_VOLUME", "500000"))
MEANREV_LIMIT = int(os.environ.get("MEANREV_LIMIT", "5"))

# ── Insider params (FMP P-Purchase, scored by seniority + cluster + $ value) ─
INSIDER_HOLD_DAYS = int(os.environ.get("INSIDER_HOLD_DAYS", "30"))
INSIDER_STOP_PCT = float(os.environ.get("INSIDER_STOP_PCT", "0.08"))
# UNVALIDATED — insider has no backtest (blocked: FMP /stable/insider-trading
# is a paid-tier endpoint, 402 on the free plan; no historical EDGAR puller
# exists as an alternative — see docs/dev/strategy-validation-status.md).
# Mirrors EARNMOM_TARGET_PCT below (same 8% stop) purely for consistency,
# not because insider's own data supports this number — it doesn't have any.
# Attached as the OCO take-profit leg alongside the stop so profit-taking
# isn't left to the discretionary midday/EOD review.
INSIDER_TARGET_PCT = float(os.environ.get("INSIDER_TARGET_PCT", "0.10"))
INSIDER_SIZE_PCT = float(os.environ.get("INSIDER_SIZE_PCT", "0.04"))
INSIDER_MIN_PRICE = float(os.environ.get("INSIDER_MIN_PRICE", "5.0"))
INSIDER_MIN_DOLLAR = float(os.environ.get("INSIDER_MIN_DOLLAR", "100000"))
INSIDER_LOOKBACK_DAYS = int(os.environ.get("INSIDER_LOOKBACK_DAYS", "30"))
INSIDER_LIMIT = int(os.environ.get("INSIDER_LIMIT", "5"))

# ── Squeeze params (SI > 15% + DTC > 3 + momentum) ──────────────────────────
SQUEEZE_HOLD_DAYS = int(os.environ.get("SQUEEZE_HOLD_DAYS", "21"))
SQUEEZE_STOP_PCT = float(os.environ.get("SQUEEZE_STOP_PCT", "0.10"))
SQUEEZE_SIZE_PCT = float(os.environ.get("SQUEEZE_SIZE_PCT", "0.03"))
SQUEEZE_MIN_PRICE = float(os.environ.get("SQUEEZE_MIN_PRICE", "5.0"))
SQUEEZE_MIN_SI_PCT = float(os.environ.get("SQUEEZE_MIN_SI_PCT", "15.0"))
SQUEEZE_MIN_DTC = float(os.environ.get("SQUEEZE_MIN_DTC", "3.0"))
SQUEEZE_MIN_MOMENTUM = float(os.environ.get("SQUEEZE_MIN_MOMENTUM", "5.0"))
# minimum 20-day momentum % to consider stock has fuel for squeeze
SQUEEZE_LIMIT = int(os.environ.get("SQUEEZE_LIMIT", "5"))

# ── Breakout params (above 50-day resistance + 1.5x volume) ─────────────────
BREAKOUT_HOLD_DAYS = int(os.environ.get("BREAKOUT_HOLD_DAYS", "21"))
BREAKOUT_STOP_PCT = float(os.environ.get("BREAKOUT_STOP_PCT", "0.06"))
BREAKOUT_SIZE_PCT = float(os.environ.get("BREAKOUT_SIZE_PCT", "0.04"))
BREAKOUT_MIN_PRICE = float(os.environ.get("BREAKOUT_MIN_PRICE", "10.0"))
BREAKOUT_VOL_MULT = float(os.environ.get("BREAKOUT_VOL_MULT", "1.5"))
BREAKOUT_MIN_AVG_VOLUME = float(os.environ.get("BREAKOUT_MIN_AVG_VOLUME", "500000"))
BREAKOUT_LIMIT = int(os.environ.get("BREAKOUT_LIMIT", "5"))

# ── MA Crossover params (20/50-day golden cross, volume-confirmed) ──────────
# Pure trend-following: unlike breakout (resistance level), meanrev (RSI
# extreme), or earnmom (earnings catalyst), this reacts only to two moving
# averages agreeing the intermediate trend just turned up. No hard target —
# time/trail-managed exit, same as breakout, so a real trend is left to run.
MACROSS_FAST_PERIOD = int(os.environ.get("MACROSS_FAST_PERIOD", "20"))
MACROSS_SLOW_PERIOD = int(os.environ.get("MACROSS_SLOW_PERIOD", "50"))
# How many trading days back a golden cross is still considered "fresh"
# enough to act on — 0 would mean today only, misses the setup entirely.
MACROSS_MAX_DAYS_SINCE_CROSS = int(os.environ.get("MACROSS_MAX_DAYS_SINCE_CROSS", "3"))
# Cross-day volume vs. its own trailing 20-day average — filters out
# low-conviction crosses that drift through the average on thin volume.
MACROSS_MIN_VOLUME_RATIO = float(os.environ.get("MACROSS_MIN_VOLUME_RATIO", "1.2"))
MACROSS_HOLD_DAYS = int(os.environ.get("MACROSS_HOLD_DAYS", "21"))
MACROSS_STOP_PCT = float(os.environ.get("MACROSS_STOP_PCT", "0.06"))
MACROSS_SIZE_PCT = float(os.environ.get("MACROSS_SIZE_PCT", "0.04"))
MACROSS_MIN_PRICE = float(os.environ.get("MACROSS_MIN_PRICE", "10.0"))
MACROSS_MIN_AVG_VOLUME = float(os.environ.get("MACROSS_MIN_AVG_VOLUME", "500000"))
MACROSS_LIMIT = int(os.environ.get("MACROSS_LIMIT", "5"))

# ── Earnings Momentum params (beat 8-45 days ago, still drifting up) ────────
EARNMOM_HOLD_DAYS = int(os.environ.get("EARNMOM_HOLD_DAYS", "35"))
EARNMOM_STOP_PCT = float(os.environ.get("EARNMOM_STOP_PCT", "0.08"))
# Derived from EARNMOM's own validated backtest (backtest_5_strategies.py,
# fixed sizing per BACKTEST_MAX_POSITION_PCT): 217 trades, Sharpe 0.96,
# p=0.022, TRUSTWORTHY. That run had no target (pure time/trail exit) and
# posted avg_win_pct=9.1%, avg_loss_pct=-6.38% — a 2:1-vs-stop target
# (16%) would sit ~1.75x above the strategy's actual average winner and
# essentially never fire. 10% sits close to the empirical average win —
# ~1.25:1 vs the 8% stop — so it functions as real, tested-realistic
# profit-taking instead of a target that's decorative. Attached as the
# OCO take-profit leg alongside the stop.
EARNMOM_TARGET_PCT = float(os.environ.get("EARNMOM_TARGET_PCT", "0.10"))
EARNMOM_SIZE_PCT = float(os.environ.get("EARNMOM_SIZE_PCT", "0.04"))
EARNMOM_MIN_PRICE = float(os.environ.get("EARNMOM_MIN_PRICE", "10.0"))
EARNMOM_MIN_AVG_VOLUME = float(os.environ.get("EARNMOM_MIN_AVG_VOLUME", "500000"))
EARNMOM_MIN_SURPRISE_PCT = float(os.environ.get("EARNMOM_MIN_SURPRISE_PCT", "5.0"))
EARNMOM_LOOKBACK_DAYS = int(os.environ.get("EARNMOM_LOOKBACK_DAYS", "60"))
EARNMOM_MAX_DAYS_AGO = int(os.environ.get("EARNMOM_MAX_DAYS_AGO", "45"))
EARNMOM_MIN_DRIFT_PCT = float(os.environ.get("EARNMOM_MIN_DRIFT_PCT", "2.0"))
# stock must be up at least this much since earnings beat
EARNMOM_LIMIT = int(os.environ.get("EARNMOM_LIMIT", "5"))

# ── MA Pullback params (20/200 SMA trend & pullback) ────────
# MA Pullback strategy: price above SMA200, rising SMA20, price crossing above SMA20
MAPULLBACK_STOP_PCT = float(os.environ.get("MAPULLBACK_STOP_PCT", "0.02"))  # 2% hard stop
MAPULLBACK_SIZE_PCT = float(os.environ.get("MAPULLBACK_SIZE_PCT", "0.05"))  # 5% position size (same as others)
MAPULLBACK_MIN_PRICE = float(os.environ.get("MAPULLBACK_MIN_PRICE", "10.0"))
MAPULLBACK_MAX_PRICE = float(os.environ.get("MAPULLBACK_MAX_PRICE", "25.0"))
MAPULLBACK_MIN_AVG_VOLUME = float(os.environ.get("MAPULLBACK_MIN_AVG_VOLUME", "500000"))
MAPULLBACK_LIMIT = int(os.environ.get("MAPULLBACK_LIMIT", "5"))

# ── Time-series confirming filter (directional forecast) ────────────────────
# NOT a standalone strategy — a confirming filter layered on existing
# strategies (see core/timeseries_signal.py). An existing strategy's entry
# only proceeds if this model agrees (long) or is neutral/inconclusive; it
# never opens a trade by itself. Off by default until its own standalone
# backtest (same harness, same gates as breakout/meanrev/earnmom) clears the
# 2026-08-06-style bar: trade_count, not_overfit, significant.
TIMESERIES_ENABLED = os.environ.get("TIMESERIES_ENABLED", "false").lower() == "true"
# "arima" (ARIMA(1,1,1) on price levels — standalone-backtested 2026-08-10,
# never cleared 60% confidence, mean 9.7%; see backtests/timeseries_standalone_
# 2026-08-10/summary.md) or "lstm" (tiny 1-layer LSTM on return sequences —
# the task's fallback option, tried next; UNVALIDATED, needs its own
# standalone backtest run before TIMESERIES_ENABLED is ever considered).
TIMESERIES_MODEL = os.environ.get("TIMESERIES_MODEL", "lstm").strip().lower()
TIMESERIES_LSTM_LOOKBACK = int(os.environ.get("TIMESERIES_LSTM_LOOKBACK", "20"))
TIMESERIES_LSTM_EPOCHS = int(os.environ.get("TIMESERIES_LSTM_EPOCHS", "30"))
# Model must be at least this confident (0-1) in a DISAGREEING direction
# before it blocks a trade — below this, treated as neutral/inconclusive.
TIMESERIES_MIN_CONFIDENCE = float(os.environ.get("TIMESERIES_MIN_CONFIDENCE", "0.6"))
TIMESERIES_MIN_HISTORY_DAYS = int(os.environ.get("TIMESERIES_MIN_HISTORY_DAYS", "60"))
# Only used by the standalone backtest entry point (get_historical_timeseries_signals)
# to size/exit a synthetic trade for validation purposes — the live filter
# never opens or exits positions itself.
TIMESERIES_HOLD_DAYS = int(os.environ.get("TIMESERIES_HOLD_DAYS", "5"))
TIMESERIES_STOP_PCT = float(os.environ.get("TIMESERIES_STOP_PCT", "0.05"))
TIMESERIES_MIN_PRICE = float(os.environ.get("TIMESERIES_MIN_PRICE", "10.0"))
TIMESERIES_MIN_AVG_VOLUME = float(os.environ.get("TIMESERIES_MIN_AVG_VOLUME", "500000"))

# ── Gap Fill params (morning gap fade — intraday mean reversion) ─────────────
# Entry: stock gaps > min at open; fade the spike back to prior close.
# Win rate 55-70% (best on 3-8% gaps with volume confirmation).
GAPFILL_MIN_GAP_PCT = float(os.environ.get("GAPFILL_MIN_GAP_PCT", "3.0"))
GAPFILL_MAX_GAP_PCT = float(os.environ.get("GAPFILL_MAX_GAP_PCT", "12.0"))
GAPFILL_MIN_PRICE = float(os.environ.get("GAPFILL_MIN_PRICE", "5.0"))
GAPFILL_MIN_VOLUME = float(os.environ.get("GAPFILL_MIN_VOLUME", "500000"))
GAPFILL_EARNINGS_BLACKOUT_DAYS = int(os.environ.get("GAPFILL_EARNINGS_BLACKOUT_DAYS", "5"))
GAPFILL_STOP_PCT = float(os.environ.get("GAPFILL_STOP_PCT", "0.04"))
GAPFILL_LIMIT = int(os.environ.get("GAPFILL_LIMIT", "3"))

# ── Momentum Continuation params (3-day streak) ────────────────────────────
# Entry: stock up N consecutive days on volume; ride day 4 continuation.
# Win rate 55-65%. 3-5 day streaks have best Sharpe; drops off at 7+.
MOMENTUM_STREAK_DAYS = int(os.environ.get("MOMENTUM_STREAK_DAYS", "3"))
MOMENTUM_STOP_PCT = float(os.environ.get("MOMENTUM_STOP_PCT", "0.05"))
MOMENTUM_TAKE_PROFIT_PCT = float(os.environ.get("MOMENTUM_TAKE_PROFIT_PCT", "0.08"))
MOMENTUM_MIN_PRICE = float(os.environ.get("MOMENTUM_MIN_PRICE", "5.0"))
MOMENTUM_MIN_AVG_VOLUME = float(os.environ.get("MOMENTUM_MIN_AVG_VOLUME", "500000"))
MOMENTUM_MIN_MOMENTUM_PCT = float(os.environ.get("MOMENTUM_MIN_MOMENTUM_PCT", "3.0"))
MOMENTUM_HOLD_DAYS = int(os.environ.get("MOMENTUM_HOLD_DAYS", "5"))
MOMENTUM_LIMIT = int(os.environ.get("MOMENTUM_LIMIT", "5"))

# ── Sector Rotation params ────────────────────────────────────────────────
# Entry: buy strongest stock in top-performing sector.
# Win rate 55-65%. Works best when sector leadership is clear.
SECTOR_MIN_RANK = int(os.environ.get("SECTOR_MIN_RANK", "4"))
SECTOR_STOP_PCT = float(os.environ.get("SECTOR_STOP_PCT", "0.06"))
SECTOR_TAKE_PROFIT_PCT = float(os.environ.get("SECTOR_TAKE_PROFIT_PCT", "0.10"))
SECTOR_MIN_PRICE = float(os.environ.get("SECTOR_MIN_PRICE", "5.0"))
SECTOR_MIN_AVG_VOLUME = float(os.environ.get("SECTOR_MIN_AVG_VOLUME", "500000"))
SECTOR_MIN_RS = float(os.environ.get("SECTOR_MIN_RS", "15.0"))
SECTOR_HOLD_DAYS = int(os.environ.get("SECTOR_HOLD_DAYS", "14"))
SECTOR_MAX_GAP_PCT = float(os.environ.get("SECTOR_MAX_GAP_PCT", "8.0"))
SECTOR_LIMIT = int(os.environ.get("SECTOR_LIMIT", "3"))

# ── VCP params (volatility-contraction breakout, Claude-scored in pre_market) ─
# Consumes the buy_list pre_market already screened + scored (state/
# pre_market_watchlist.json) rather than re-screening — opt-in, unvalidated.
VCP_SIZE_PCT = float(os.environ.get("VCP_SIZE_PCT", "0.04"))
VCP_STOP_PCT = float(os.environ.get("VCP_STOP_PCT", "0.08"))
VCP_HOLD_DAYS = int(os.environ.get("VCP_HOLD_DAYS", "21"))

# ── E4 Portable Alpha: idle cash → SPY ───────────────────────────────────────
SPY_BASE_ENABLED = os.environ.get("SPY_BASE_ENABLED", "true").lower() == "true"
SPY_CASH_RESERVE_PCT = float(os.environ.get("SPY_CASH_RESERVE_PCT", "0.10"))
SPY_REBALANCE_BAND = float(os.environ.get("SPY_REBALANCE_BAND", "0.05"))
# Governance: hard cap prevents SPY from consuming the entire portfolio.
# SPY_EXEMPT from circuit breaker — this is the structural base position.
SPY_MAX_PCT = float(os.environ.get("SPY_MAX_PCT", "0.93"))  # % of equity
SPY_MAX_POSITIONS = int(os.environ.get("SPY_MAX_POSITIONS", "1"))  # shares outstanding

# ── Timezone ──────────────────────────────────────────────────────────────────
TIMEZONE = "America/New_York"

# ── S&P 500 universe (top 80 for FMP-limited screeners) ─────────────────────
# Curated 80 symbols covering all major sectors — reasonable universe for
# FMP /stable/ endpoints that are slower than Alpaca IEX.
SP80_UNIVERSE = [
    # Technology
    "AAPL",
    "MSFT",
    "NVDA",
    "AVGO",
    "AMD",
    "META",
    "GOOGL",
    "AMZN",
    "ADBE",
    "CRM",
    "ORCL",
    "ACN",
    "CSCO",
    "IBM",
    "INTC",
    "QCOM",
    "TXN",
    "NOW",
    "INTU",
    "AMAT",
    # Consumer
    "NFLX",
    "TSLA",
    "HD",
    "MCD",
    "NKE",
    "SBUX",
    "TGT",
    "LOW",
    "UPS",
    "DG",
    # Healthcare
    "LLY",
    "JNJ",
    "UNH",
    "PFE",
    "ABBV",
    "MRK",
    "BMY",
    "GILD",
    "AMGN",
    "ISRG",
    # Financials
    "JPM",
    "BAC",
    "WFC",
    "GS",
    "MS",
    "BLK",
    "C",
    "AXP",
    "SCHW",
    "USB",
    # Industrials
    "CAT",
    "GE",
    "RTX",
    "HON",
    "BA",
    "LMT",
    "DE",
    "MMM",
    "ADP",
    "PCAR",
    # Energy
    "XOM",
    "CVX",
    "COP",
    "EOG",
    "SLB",
    "PSX",
    "MPC",
    "VLO",
    "OXY",
    "HAL",
    # Utilities / Real estate / Materials
    "NEE",
    "DUK",
    "SO",
    "SPG",
    "PLD",
    "AMT",
    "CCI",
    "EQIX",
    "LIN",
    # Communication
    "DIS",
    "CMCSA",
    "CHTR",
    "TMUS",
    "NFLX",
    "PYPL",
    "SNAP",
    # Health tech / Biotech
    "DXCM",
    "HUM",
    "CI",
    "ELV",
    "CNC",
    "ZLAB",
    "REGN",
    "BIIB",
    "MRNA",
    # Misc
    "V",
    "MA",
    "ADP",
    "IDXX",
    "ODFL",
    "FAST",
    "CPRT",
    "ADI",
]
SP80_UNIVERSE = sorted(list(set(SP80_UNIVERSE)))  # de-dup

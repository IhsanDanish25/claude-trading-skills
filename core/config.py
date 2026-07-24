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

# ── Anthropic ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

# ── FMP ───────────────────────────────────────────────────────────────────────
FMP_API_KEY = os.environ.get("FMP_API_KEY", "").strip()

_REQUIRED = {
    "ALPACA_API_KEY": ALPACA_API_KEY,
    "ALPACA_SECRET_KEY": ALPACA_SECRET_KEY,
    "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
    "FMP_API_KEY": FMP_API_KEY,
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
    os.environ.get("MAX_POSITION_SIZE_PCT", os.environ.get("MAX_POSITION_PCT", "0.05"))
)
MAX_OPEN_POSITIONS = int(
    os.environ.get("MAX_OPEN_POSITIONS", os.environ.get("MAX_POSITIONS", "10"))
)
STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PCT", "0.02"))
TAKE_PROFIT_PCT = float(os.environ.get("TAKE_PROFIT_PCT", "0.06"))
MIN_RELATIVE_VOLUME = float(os.environ.get("MIN_REL_VOL", "1.5"))
MIN_PRICE = float(os.environ.get("MIN_PRICE", "5.0"))
MAX_PRICE = float(os.environ.get("MAX_PRICE", "500.0"))
MIN_COMPOSITE_SCORE = int(os.environ.get("MIN_COMPOSITE_SCORE", "20"))
RISK_PCT = float(os.environ.get("RISK_PCT", "0.01"))
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
# Supported: pead, meanrev, insider, squeeze, breakout, earnmom, gapfill, momentum, sector, vcp
# Recommended: STRATEGY_MODE=pead,meanrev,insider,squeeze
# breakout and earnmom are excluded from the recommended default — backtested
# negative (Breakout Sharpe -0.38 p=0.585; EarnMom Sharpe -0.37, 31.4% win
# rate). Sector/momentum/gapfill/vcp are opt-in until live-validated (none of
# the four appear in docs/dev/strategy-validation-status.md).
_STRATEGY_RAW = os.environ.get("STRATEGY_MODE", "pead").lower()
STRATEGY_MODES = [s.strip() for s in _STRATEGY_RAW.split(",") if s.strip()]

# ── PEAD params ───────────────────────────────────────────────────────────────
PEAD_HOLD_DAYS = int(os.environ.get("PEAD_HOLD_DAYS", "60"))
PEAD_STOP_PCT = float(os.environ.get("PEAD_STOP_PCT", "0.15"))
PEAD_LOOKBACK_DAYS = int(os.environ.get("PEAD_LOOKBACK_DAYS", "7"))
PEAD_MIN_SURPRISE_PCT = float(os.environ.get("PEAD_MIN_SURPRISE_PCT", "10.0"))
PEAD_MIN_PRICE = float(os.environ.get("PEAD_MIN_PRICE", "10.0"))
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
MEANREV_RSI_THRESHOLD = float(os.environ.get("MEANREV_RSI_THRESHOLD", "35.0"))
MEANREV_BB_THRESHOLD = float(os.environ.get("MEANREV_BB_THRESHOLD", "2.0"))
# dollar buffer above lower BB: 0.0 = price must be at/below the band exactly;
# 2.0 = allow price up to $2 above lower band (band touch is rare on daily closes)
MEANREV_MIN_AVG_VOLUME = float(os.environ.get("MEANREV_MIN_AVG_VOLUME", "500000"))
MEANREV_LIMIT = int(os.environ.get("MEANREV_LIMIT", "5"))

# ── Insider params (FMP P-Purchase, scored by seniority + cluster + $ value) ─
INSIDER_HOLD_DAYS = int(os.environ.get("INSIDER_HOLD_DAYS", "30"))
INSIDER_STOP_PCT = float(os.environ.get("INSIDER_STOP_PCT", "0.08"))
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

# ── Earnings Momentum params (beat 8-45 days ago, still drifting up) ────────
EARNMOM_HOLD_DAYS = int(os.environ.get("EARNMOM_HOLD_DAYS", "35"))
EARNMOM_STOP_PCT = float(os.environ.get("EARNMOM_STOP_PCT", "0.08"))
EARNMOM_SIZE_PCT = float(os.environ.get("EARNMOM_SIZE_PCT", "0.04"))
EARNMOM_MIN_PRICE = float(os.environ.get("EARNMOM_MIN_PRICE", "10.0"))
EARNMOM_MIN_AVG_VOLUME = float(os.environ.get("EARNMOM_MIN_AVG_VOLUME", "500000"))
EARNMOM_MIN_SURPRISE_PCT = float(os.environ.get("EARNMOM_MIN_SURPRISE_PCT", "5.0"))
EARNMOM_LOOKBACK_DAYS = int(os.environ.get("EARNMOM_LOOKBACK_DAYS", "60"))
EARNMOM_MAX_DAYS_AGO = int(os.environ.get("EARNMOM_MAX_DAYS_AGO", "45"))
EARNMOM_MIN_DRIFT_PCT = float(os.environ.get("EARNMOM_MIN_DRIFT_PCT", "2.0"))
# stock must be up at least this much since earnings beat
EARNMOM_LIMIT = int(os.environ.get("EARNMOM_LIMIT", "5"))

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
    "REIT",
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

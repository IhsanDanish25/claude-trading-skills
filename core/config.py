"""
Central config — reads from env vars (Railway secrets) or .env file locally.
"""
import os
import sys

try:
    from dotenv import load_dotenv
    # Load .env from repo root regardless of working directory.
    # Existing env vars take precedence (override=False is the default).
    _dotenv = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    load_dotenv(_dotenv)
except ImportError:
    pass

# ── Alpaca ────────────────────────────────────────────────────────────────────
ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
PAPER_TRADE       = os.environ.get("ALPACA_PAPER_TRADE", os.environ.get("ALPACA_PAPER", "true")).lower() == "true"

# ── Anthropic ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── FMP ───────────────────────────────────────────────────────────────────────
FMP_API_KEY = os.environ.get("FMP_API_KEY", "")

_REQUIRED = {
    "ALPACA_API_KEY":    ALPACA_API_KEY,
    "ALPACA_SECRET_KEY": ALPACA_SECRET_KEY,
    "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
    "FMP_API_KEY":       FMP_API_KEY,
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
MAX_POSITION_SIZE_PCT = float(os.environ.get("MAX_POSITION_SIZE_PCT",
                              os.environ.get("MAX_POSITION_PCT", "0.05")))
MAX_OPEN_POSITIONS    = int(os.environ.get("MAX_OPEN_POSITIONS",
                            os.environ.get("MAX_POSITIONS", "10")))
STOP_LOSS_PCT         = float(os.environ.get("STOP_LOSS_PCT", "0.02"))
TAKE_PROFIT_PCT       = float(os.environ.get("TAKE_PROFIT_PCT", "0.06"))
MIN_RELATIVE_VOLUME   = float(os.environ.get("MIN_REL_VOL", "1.5"))
MIN_PRICE             = float(os.environ.get("MIN_PRICE", "5.0"))
MAX_PRICE             = float(os.environ.get("MAX_PRICE", "500.0"))
MIN_COMPOSITE_SCORE   = int(os.environ.get("MIN_COMPOSITE_SCORE", "20"))
RISK_PCT              = float(os.environ.get("RISK_PCT", "0.01"))

# ── Edge upgrades ─────────────────────────────────────────────────────────────
ENTRY_DELAY_MIN       = int(os.environ.get("ENTRY_DELAY_MIN", "20"))
MIN_RS_RATING         = float(os.environ.get("MIN_RS_RATING", "0.0"))
BREAKOUT_VOL_MULT     = float(os.environ.get("BREAKOUT_VOL_MULT", "1.5"))
PARTIAL_PROFIT_PCT    = float(os.environ.get("PARTIAL_PROFIT_PCT", "0.06"))
PARTIAL_PROFIT_SIZE   = float(os.environ.get("PARTIAL_PROFIT_SIZE", "0.5"))
TRAIL_STOP_PCT        = float(os.environ.get("TRAIL_STOP_PCT", "0.04"))
FTD_DEFENSIVE_SIZE    = float(os.environ.get("FTD_DEFENSIVE_SIZE", "0.025"))
ALLOW_FTD_BOTTOM_BUY  = os.environ.get("ALLOW_FTD_BOTTOM_BUY", "true").lower() == "true"
STRONG_SECTORS_ONLY   = os.environ.get("STRONG_SECTORS_ONLY", "true").lower() == "true"

# ── Edge pack 2 ───────────────────────────────────────────────────────────────
MAX_GAP_PCT           = float(os.environ.get("MAX_GAP_PCT", "5.0"))
EARNINGS_BLACKOUT_DAYS = int(os.environ.get("EARNINGS_BLACKOUT_DAYS", "3"))
MAX_PER_SECTOR        = int(os.environ.get("MAX_PER_SECTOR", "2"))
ALLOW_PYRAMIDING      = os.environ.get("ALLOW_PYRAMIDING", "true").lower() == "true"
PYRAMID_TRIGGER_PCT   = float(os.environ.get("PYRAMID_TRIGGER_PCT", "0.03"))
CIRCUIT_BREAKER_PCT   = float(os.environ.get("CIRCUIT_BREAKER_PCT", "0.05"))
TRAIL_INTRADAY        = os.environ.get("TRAIL_INTRADAY", "true").lower() == "true"

# ── State dir ─────────────────────────────────────────────────────────────────
STATE_DIR = os.environ.get("STATE_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state"))
os.makedirs(STATE_DIR, exist_ok=True)

# ── Watchlist (VCP universe) ──────────────────────────────────────────────────
WATCHLIST = [
    "AAPL","MSFT","NVDA","AMD","META","GOOGL","AMZN","TSLA","NFLX","CRM",
    "ADBE","PANW","CRWD","SNOW","DDOG","MELI","SQ","SHOP","NET","ZS",
    "CELH","ENPH","FSLR","ON","AEHR","SMCI","AXON","COCO","DUOL","PINS"
]

# ── Strategy mode ─────────────────────────────────────────────────────────────
# Comma-separated list of strategies to run each market-open cycle, in order.
# Available: "pead", "vcp", "meanrev", "insider", "squeeze", "breakout", "earnmom"
# Strategies share the slot budget; exhausted slots stop the loop early.
STRATEGY_MODE  = os.environ.get("STRATEGY_MODE", "pead").lower()
STRATEGY_MODES = [s.strip() for s in STRATEGY_MODE.split(",") if s.strip()]

# ── PEAD params ───────────────────────────────────────────────────────────────
PEAD_HOLD_DAYS        = int(os.environ.get("PEAD_HOLD_DAYS", "60"))
PEAD_STOP_PCT         = float(os.environ.get("PEAD_STOP_PCT", "0.15"))
PEAD_LOOKBACK_DAYS    = int(os.environ.get("PEAD_LOOKBACK_DAYS", "7"))
PEAD_MIN_SURPRISE_PCT = float(os.environ.get("PEAD_MIN_SURPRISE_PCT", "10.0"))
PEAD_MIN_PRICE        = float(os.environ.get("PEAD_MIN_PRICE", "10.0"))
PEAD_MIN_AVG_VOLUME   = float(os.environ.get("PEAD_MIN_AVG_VOLUME", "500000"))
PEAD_SIZE_PCT         = float(os.environ.get("PEAD_SIZE_PCT", "0.05"))

# ── Mean-reversion screener params ───────────────────────────────────────────
MEANREV_RSI_THRESHOLD = float(os.environ.get("MEANREV_RSI_THRESHOLD", "30.0"))
MEANREV_BB_STD        = float(os.environ.get("MEANREV_BB_STD", "2.0"))
MEANREV_MIN_PRICE     = float(os.environ.get("MEANREV_MIN_PRICE", "5.0"))
MEANREV_MIN_AVG_VOL   = float(os.environ.get("MEANREV_MIN_AVG_VOL", "500000"))
MEANREV_SIZE_PCT      = float(os.environ.get("MEANREV_SIZE_PCT", str(MAX_POSITION_SIZE_PCT)))

# ── Insider screener params ───────────────────────────────────────────────────
INSIDER_LOOKBACK_DAYS = int(os.environ.get("INSIDER_LOOKBACK_DAYS", "30"))
INSIDER_MIN_SCORE     = int(os.environ.get("INSIDER_MIN_SCORE", "20"))
INSIDER_MIN_CLUSTER   = int(os.environ.get("INSIDER_MIN_CLUSTER", "1"))
INSIDER_SIZE_PCT      = float(os.environ.get("INSIDER_SIZE_PCT", str(MAX_POSITION_SIZE_PCT)))

# ── Squeeze screener params ───────────────────────────────────────────────────
SQUEEZE_SI_MIN_PCT    = float(os.environ.get("SQUEEZE_SI_MIN_PCT", "15.0"))
SQUEEZE_DTC_MIN       = float(os.environ.get("SQUEEZE_DTC_MIN", "3.0"))
SQUEEZE_MIN_PRICE     = float(os.environ.get("SQUEEZE_MIN_PRICE", "3.0"))
SQUEEZE_MIN_AVG_VOL   = float(os.environ.get("SQUEEZE_MIN_AVG_VOL", "500000"))
SQUEEZE_SIZE_PCT      = float(os.environ.get("SQUEEZE_SIZE_PCT", str(MAX_POSITION_SIZE_PCT)))

# ── Breakout screener params ──────────────────────────────────────────────────
BREAKOUT_VOL_MULT_MIN = float(os.environ.get("BREAKOUT_VOL_MULT_MIN", "1.5"))
BREAKOUT_MIN_PRICE    = float(os.environ.get("BREAKOUT_MIN_PRICE", "5.0"))
BREAKOUT_MIN_AVG_VOL  = float(os.environ.get("BREAKOUT_MIN_AVG_VOL", "200000"))
BREAKOUT_SIZE_PCT     = float(os.environ.get("BREAKOUT_SIZE_PCT", str(MAX_POSITION_SIZE_PCT)))

# ── Earnings-momentum screener params ─────────────────────────────────────────
EARNMOM_LOOKBACK_MIN      = int(os.environ.get("EARNMOM_LOOKBACK_MIN", "8"))
EARNMOM_LOOKBACK_MAX      = int(os.environ.get("EARNMOM_LOOKBACK_MAX", "45"))
EARNMOM_MIN_SURPRISE_PCT  = float(os.environ.get("EARNMOM_MIN_SURPRISE_PCT", "0.0"))
EARNMOM_MIN_DRIFT_PCT     = float(os.environ.get("EARNMOM_MIN_DRIFT_PCT", "0.0"))
EARNMOM_MIN_PRICE         = float(os.environ.get("EARNMOM_MIN_PRICE", "5.0"))
EARNMOM_MIN_AVG_VOL       = float(os.environ.get("EARNMOM_MIN_AVG_VOL", "500000"))
EARNMOM_SIZE_PCT          = float(os.environ.get("EARNMOM_SIZE_PCT", str(MAX_POSITION_SIZE_PCT)))

# ── E4 Portable Alpha: idle cash → SPY ───────────────────────────────────────
SPY_BASE_ENABLED      = os.environ.get("SPY_BASE_ENABLED", "true").lower() == "true"
SPY_CASH_RESERVE_PCT  = float(os.environ.get("SPY_CASH_RESERVE_PCT", "0.10"))  # keep 10% cash buffer
SPY_REBALANCE_BAND    = float(os.environ.get("SPY_REBALANCE_BAND", "0.05"))    # rebalance if off by 5%+

# ── Timezone ──────────────────────────────────────────────────────────────────
TIMEZONE = "America/New_York"

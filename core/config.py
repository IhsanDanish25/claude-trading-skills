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

# ── Timezone ──────────────────────────────────────────────────────────────────
TIMEZONE = "America/New_York"

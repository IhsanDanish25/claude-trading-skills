"""
Central config — reads from env vars (Railway secrets).

All API keys use os.environ.get() so the module can be imported safely.
Call validate() at the start of any routine to get a clear error listing
every missing key, instead of a raw KeyError on the first one.
"""
import os
import sys

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
    """Raise RuntimeError listing every missing required env var."""
    missing = [name for name, val in _REQUIRED.items() if not val]
    if missing:
        msg = (
            "Missing required environment variables (set them in Railway secrets):\n  "
            + "\n  ".join(missing)
        )
        print(msg, file=sys.stderr)
        raise RuntimeError(msg)

# ── Trading params ────────────────────────────────────────────────────────────
MAX_POSITION_SIZE_PCT = float(os.environ.get("MAX_POSITION_PCT", "0.05"))   # 5% per trade
MAX_OPEN_POSITIONS    = int(os.environ.get("MAX_POSITIONS", "10"))
STOP_LOSS_PCT         = float(os.environ.get("STOP_LOSS_PCT", "0.02"))      # 2% stop
TAKE_PROFIT_PCT       = float(os.environ.get("TAKE_PROFIT_PCT", "0.06"))    # 6% target
MIN_RELATIVE_VOLUME   = float(os.environ.get("MIN_REL_VOL", "1.5"))
MIN_PRICE             = float(os.environ.get("MIN_PRICE", "5.0"))
MAX_PRICE             = float(os.environ.get("MAX_PRICE", "500.0"))

# ── Watchlist (VCP universe) ──────────────────────────────────────────────────
WATCHLIST = [
    "AAPL","MSFT","NVDA","AMD","META","GOOGL","AMZN","TSLA","NFLX","CRM",
    "ADBE","PANW","CRWD","SNOW","DDOG","MELI","SQ","SHOP","NET","ZS",
    "CELH","ENPH","FSLR","ON","AEHR","SMCI","AXON","COCO","DUOL","PINS"
]

# ── Timezone ──────────────────────────────────────────────────────────────────
TIMEZONE = "America/New_York"

# ── Persistent state directory (survives across routine ticks) ────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(_PROJECT_ROOT, "state")
os.makedirs(STATE_DIR, exist_ok=True)

"""
Central config — reads from env vars (Railway secrets).
"""
import os

# ── Alpaca ────────────────────────────────────────────────────────────────────
ALPACA_API_KEY    = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
PAPER_TRADE       = os.environ.get("ALPACA_PAPER_TRADE", os.environ.get("ALPACA_PAPER", "true")).lower() == "true"

# ── Anthropic ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# ── FMP ───────────────────────────────────────────────────────────────────────
FMP_API_KEY = os.environ["FMP_API_KEY"]

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

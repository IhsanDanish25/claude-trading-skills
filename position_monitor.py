"""
Position Monitor — runs every 10 min during market hours.

Two jobs:
  1. STOP-LOSS: close any position down >= MONITOR_STOP_PCT from avg entry
  2. RE-ATTACH TAKE-PROFIT: if no open sell limit order exists for a position,
     place a new one at MONITOR_TP_PCT above avg entry

Run standalone:
  uv run python3 position_monitor.py
"""
import os, logging
from pathlib import Path
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _load_dotenv(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip().strip('"').strip("'")

_load_dotenv(Path(__file__).parent / ".env")

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
PAPER             = os.environ.get("ALPACA_PAPER", "true").lower() == "true"

MONITOR_STOP_PCT  = float(os.environ.get("MONITOR_STOP_PCT", "0.10"))  # close if down 10%
MONITOR_TP_PCT    = float(os.environ.get("MONITOR_TP_PCT",   "0.05"))  # re-attach TP at +5%
MONITOR_MAX_DAYS  = int(os.environ.get("MONITOR_MAX_DAYS",   "5"))     # Connors time stop: exit after N days
DRY_RUN           = os.environ.get("AUTO_TRADER_DRY_RUN", "0") == "1"


def _client() -> TradingClient:
    return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER)


def _get_open_sell_orders(client: TradingClient) -> dict:
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    try:
        orders = client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.OPEN,
            side=OrderSide.SELL,
            limit=100,
        ))
        result = {}
        for o in orders:
            result.setdefault(o.symbol, []).append(o)
        return result
    except Exception as e:
        logger.warning("Could not fetch open orders: %s", e)
        return {}


def _latest_price(ticker: str) -> float:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestTradeRequest
    try:
        dc = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        trade = dc.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=ticker)
        )[ticker]
        return float(trade.price)
    except Exception:
        return 0.0


def _close_position(client: TradingClient, ticker: str) -> bool:
    if DRY_RUN:
        logger.info("[DRY-RUN] would close %s", ticker)
        return True
    try:
        client.close_position(ticker)
        logger.info("✅ CLOSED %s", ticker)
        return True
    except Exception as e:
        logger.error("Close %s failed: %s", ticker, e)
        return False


def _attach_tp(client: TradingClient, ticker: str, qty: float, tp_price: float) -> bool:
    if DRY_RUN:
        logger.info("[DRY-RUN] would attach TP $%.2f for %s", tp_price, ticker)
        return True
    for tif in (TimeInForce.GTC, TimeInForce.DAY):
        try:
            client.submit_order(LimitOrderRequest(
                symbol=ticker,
                qty=round(qty, 9),
                side=OrderSide.SELL,
                limit_price=round(tp_price, 2),
                time_in_force=tif,
            ))
            logger.info("  ↳ TP $%.2f attached (%s) — %s", tp_price, tif.value, ticker)
            return True
        except Exception as e:
            logger.warning("  ↳ TP attach failed (%s) %s: %s", tif.value, ticker, e)
    return False


def monitor_once() -> None:
    client = _client()

    if not client.get_clock().is_open:
        logger.info("Market closed — nothing to monitor.")
        return

    positions = client.get_all_positions()
    if not positions:
        logger.info("No open positions.")
        return

    open_sells = _get_open_sell_orders(client)
    logger.info("Monitoring %d position(s)...", len(positions))

    for pos in positions:
        ticker    = pos.symbol
        avg_entry = float(pos.avg_entry_price)
        qty       = float(pos.qty)
        pnl_pct   = float(pos.unrealized_plpc) * 100

        logger.info("  %s | qty=%.4f | entry=$%.2f | P&L=%+.1f%%",
                    ticker, qty, avg_entry, pnl_pct)

        # ── 0. Connors time stop: exit if held too long with no profit ────
        try:
            from datetime import timezone
            entry_dt = pos.avg_entry_price  # fallback
            # Use position's asset_id as proxy — check filled_at via orders
            orders = client.get_orders(__import__('alpaca.trading.requests', fromlist=['GetOrdersRequest']).GetOrdersRequest(
                status=__import__('alpaca.trading.enums', fromlist=['QueryOrderStatus']).QueryOrderStatus.CLOSED,
                symbols=[ticker], limit=5,
            ))
            buy_orders = [o for o in orders if o.side.value == "buy" and o.filled_at]
            if buy_orders:
                filled_at = min(buy_orders, key=lambda o: o.filled_at).filled_at
                days_held = (datetime.now(timezone.utc) - filled_at).days
                if days_held >= MONITOR_MAX_DAYS and pnl_pct < 1.0:
                    logger.warning("  %s: held %d days, P&L=%+.1f%% — TIME STOP (Connors rule)",
                                   ticker, days_held, pnl_pct)
                    if _close_position(client, ticker):
                        for o in open_sells.get(ticker, []):
                            try:
                                client.cancel_order_by_id(str(o.id))
                            except Exception:
                                pass
                    continue
        except Exception as e:
            logger.debug("Time stop check %s: %s", ticker, e)

        # ── 1. Stop-loss ──────────────────────────────────────────────────
        if pnl_pct <= -(MONITOR_STOP_PCT * 100):
            logger.warning("  %s: down %.1f%% — CLOSING (stop=%.0f%%)",
                           ticker, pnl_pct, MONITOR_STOP_PCT * 100)
            if _close_position(client, ticker):
                for o in open_sells.get(ticker, []):
                    try:
                        client.cancel_order_by_id(str(o.id))
                    except Exception:
                        pass
            continue

        # ── 2. Re-attach take-profit if missing ───────────────────────────
        tp_price = round(avg_entry * (1 + MONITOR_TP_PCT), 2)
        current  = _latest_price(ticker) or avg_entry

        if ticker not in open_sells:
            if current >= tp_price:
                logger.info("  %s: price $%.2f >= TP $%.2f — closing at market",
                            ticker, current, tp_price)
                _close_position(client, ticker)
            else:
                logger.info("  %s: no sell order — re-attaching TP @ $%.2f", ticker, tp_price)
                _attach_tp(client, ticker, qty, tp_price)
        else:
            logger.info("  %s: sell order active ✓", ticker)


def run() -> None:
    logger.info("=== Position Monitor | %s ===", datetime.now().strftime("%Y-%m-%d %H:%M"))
    try:
        monitor_once()
    except Exception as e:
        logger.error("Monitor error: %s", e)
    logger.info("=== Monitor done ===")


if __name__ == "__main__":
    run()

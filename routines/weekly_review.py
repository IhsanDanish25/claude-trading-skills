"""
WEEKLY REVIEW ROUTINE — 4:00 PM ET, Friday
───────────────────────────────────────────
1. Aggregate daily logs from /tmp/
2. Pull week's closed trades from Alpaca
3. Compute win rate, avg gain/loss, Sharpe estimate
4. Claude: generate weekly narrative + next week plan
5. Log full report
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import datetime
import pytz
import statistics

from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

try:
    from alpaca.trading.requests import GetPortfolioHistoryRequest
except ImportError:
    GetPortfolioHistoryRequest = None

from core import logger, config
from core.broker   import BrokerClient
from core.fmp      import get_market_breadth
from core.analyst  import generate_weekly_summary

log = logger.setup("weekly_review")
ET  = pytz.timezone("America/New_York")


def load_week_logs() -> list:
    logs = []
    today = datetime.date.today()
    for i in range(5):
        d    = today - datetime.timedelta(days=i)
        path = os.path.join(config.STATE_DIR, f"daily_log_{d.isoformat()}.json")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    logs.append(json.load(f))
            except Exception as e:
                log.warning(f"Load {path}: {e}")
    return logs


def get_closed_trades(broker: BrokerClient, days: int = 7) -> list:
    since = datetime.datetime.now(pytz.utc) - datetime.timedelta(days=days)
    try:
        # FIX: use QueryOrderStatus not OrderStatus
        req    = GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=since, limit=200)
        orders = broker.trade.get_orders(filter=req)
        trades = []
        for o in orders:
            if o.filled_avg_price and o.filled_qty:
                trades.append({
                    "symbol":    o.symbol,
                    "side":      str(o.side),
                    "qty":       float(o.filled_qty),
                    "price":     float(o.filled_avg_price),
                    "filled_at": str(o.filled_at),
                })
        return trades
    except Exception as e:
        log.error(f"Get orders fail: {e}")
        return []


def calc_week_stats(trades: list) -> dict:
    buys   = {}
    pnls   = []
    wins   = 0
    losses = 0

    for t in sorted(trades, key=lambda x: x.get("filled_at", "")):
        sym   = t["symbol"]
        side  = t["side"].lower()
        price = t["price"]
        qty   = t["qty"]

        if "buy" in side:
            if sym not in buys:
                buys[sym] = []
            buys[sym].append({"price": price, "qty": qty})
        elif "sell" in side and sym in buys and buys[sym]:
            entry = buys[sym].pop(0)
            pnl   = (price - entry["price"]) / entry["price"] * 100
            pnls.append(pnl)
            if pnl > 0:
                wins += 1
            else:
                losses += 1

    total = wins + losses
    return {
        "trades_closed":   total,
        "wins":            wins,
        "losses":          losses,
        "win_rate":        round(wins / total * 100, 1) if total > 0 else 0,
        "avg_gain_pct":    round(statistics.mean([p for p in pnls if p > 0]), 2) if any(p > 0 for p in pnls) else 0,
        "avg_loss_pct":    round(statistics.mean([p for p in pnls if p <= 0]), 2) if any(p <= 0 for p in pnls) else 0,
        "best_trade_pct":  round(max(pnls), 2) if pnls else 0,
        "worst_trade_pct": round(min(pnls), 2) if pnls else 0,
        "all_pnls":        [round(p, 2) for p in pnls],
    }


def run():
    config.validate()
    logger.banner(log, "WEEKLY REVIEW — FRIDAY 4:00 PM ET")

    broker     = BrokerClient()
    today      = datetime.date.today()
    week_start = today - datetime.timedelta(days=4)

    log.info(f"Week: {week_start.isoformat()} → {today.isoformat()}")

    daily_logs  = load_week_logs()
    log.info(f"Daily logs found: {len(daily_logs)}")

    trades      = get_closed_trades(broker, days=7)
    log.info(f"Closed trades this week: {len(trades)}")

    trade_stats = calc_week_stats(trades)
    log.info(f"── Trade stats")
    log.info(f"  Closed:   {trade_stats['trades_closed']}")
    log.info(f"  Win rate: {trade_stats['win_rate']}%")
    log.info(f"  Avg gain: {trade_stats['avg_gain_pct']:+.2f}%")
    log.info(f"  Avg loss: {trade_stats['avg_loss_pct']:+.2f}%")
    log.info(f"  Best:     {trade_stats['best_trade_pct']:+.2f}%")
    log.info(f"  Worst:    {trade_stats['worst_trade_pct']:+.2f}%")

    # Portfolio history via broker helper
    week_return_pct = 0
    try:
        hist = broker.get_portfolio_history(period="1W")
        if hist and hist.equity and len(hist.equity) >= 2:
            start_eq = float(hist.equity[0])
            end_eq   = float(hist.equity[-1])
            if start_eq > 0:
                week_return_pct = round((end_eq - start_eq) / start_eq * 100, 2)
        log.info(f"  Week return: {week_return_pct:+.2f}%")
    except Exception as e:
        log.warning(f"Portfolio history fail: {e}")

    breadth = get_market_breadth()
    acct    = broker.get_account()
    pv      = float(acct.portfolio_value)
    regimes = [d.get("regime", "unknown") for d in daily_logs]

    week_stats = {
        "week":            f"{week_start.isoformat()} to {today.isoformat()}",
        "portfolio_value": pv,
        "week_return_pct": week_return_pct,
        "trades_taken":    trade_stats["trades_closed"],
        "win_rate":        trade_stats["win_rate"],
        "avg_gain_pct":    trade_stats["avg_gain_pct"],
        "avg_loss_pct":    trade_stats["avg_loss_pct"],
        "best_trade":      trade_stats["best_trade_pct"],
        "worst_trade":     trade_stats["worst_trade_pct"],
        "spy_week_change": breadth.get("spy_change_pct", 0),
        "qqq_week_change": breadth.get("qqq_change_pct", 0),
        "regime_changes":  list(set(regimes)),
        "open_positions":  broker.position_count(),
        "trade_pnls":      trade_stats["all_pnls"],
        "lessons": [
            f"Win rate: {trade_stats['win_rate']}% ({'above' if trade_stats['win_rate'] >= 50 else 'below'} 50% target)",
            f"Market regime this week: {', '.join(set(regimes))}",
        ],
    }

    log.info("── Claude: generating weekly summary")
    try:
        summary = generate_weekly_summary(week_stats)
        log.info("\n" + "─" * 60)
        for line in summary.split("\n"):
            log.info(f"  {line}")
        log.info("─" * 60)
    except Exception as e:
        log.error(f"Summary generation fail: {e}")
        summary = "Summary unavailable"

    report_path = os.path.join(config.STATE_DIR, f"weekly_report_{today.isoformat()}.json")
    report = {"date": today.isoformat(), "stats": week_stats, "summary": summary}
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    log.info(f"Weekly report saved → {report_path}")

    log.info("── Next week setup")
    log.info(f"  Positions:      {broker.position_count()}")
    log.info(f"  Cash available: ${float(acct.cash):,.2f}")
    log.info(f"  Slots:          {config.MAX_OPEN_POSITIONS - broker.position_count()}")

    if trade_stats["win_rate"] < 40 and trade_stats["trades_closed"] >= 5:
        log.warning("  ⚠️  Win rate < 40% — reduce size next week")
    if week_return_pct < -3:
        log.warning("  ⚠️  Week < -3% — cash bias start of next week")

    logger.banner(log, "WEEKLY REVIEW COMPLETE")


if __name__ == "__main__":
    run()

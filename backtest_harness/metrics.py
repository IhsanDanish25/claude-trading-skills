"""Performance metrics + equity-curve plotting for the baseline report."""
from __future__ import annotations

import datetime
import math

TRADING_DAYS = 252


def _daily_returns(equity: list[float]) -> list[float]:
    out = []
    for a, b in zip(equity[:-1], equity[1:]):
        out.append((b / a - 1) if a > 0 else 0.0)
    return out


def _max_drawdown(equity: list[float]) -> float:
    peak = -math.inf
    mdd = 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1)
    return mdd * 100  # negative %


def _sharpe(rets: list[float]) -> float:
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    return (mean / sd * math.sqrt(TRADING_DAYS)) if sd > 0 else 0.0


def _sortino(rets: list[float]) -> float:
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    downside = [r for r in rets if r < 0]
    if not downside:
        return float("inf")
    dd = math.sqrt(sum(r ** 2 for r in downside) / len(rets))
    return (mean / dd * math.sqrt(TRADING_DAYS)) if dd > 0 else 0.0


def equity_stats(curve: list[dict], label: str) -> dict:
    equity = [p["equity"] for p in curve]
    if len(equity) < 2:
        return {"label": label, "insufficient_data": True}
    d0 = datetime.date.fromisoformat(curve[0]["date"])
    d1 = datetime.date.fromisoformat(curve[-1]["date"])
    years = max((d1 - d0).days / 365.25, 1e-9)
    total_return = (equity[-1] / equity[0] - 1) * 100
    cagr = ((equity[-1] / equity[0]) ** (1 / years) - 1) * 100
    rets = _daily_returns(equity)
    return {
        "label": label,
        "start_equity": round(equity[0], 2),
        "end_equity": round(equity[-1], 2),
        "total_return_pct": round(total_return, 2),
        "cagr_pct": round(cagr, 2),
        "max_drawdown_pct": round(_max_drawdown(equity), 2),
        "sharpe": round(_sharpe(rets), 2),
        "sortino": round(_sortino(rets), 2) if _sortino(rets) != float("inf") else None,
        "start_date": curve[0]["date"],
        "end_date": curve[-1]["date"],
        "trading_days": len(equity),
    }


def trade_stats(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"num_trades": 0}
    wins = [t for t in trades if t["return_pct"] > 0]
    losses = [t for t in trades if t["return_pct"] <= 0]
    avg_win = sum(t["return_pct"] for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t["return_pct"] for t in losses) / len(losses) if losses else 0.0
    win_rate = len(wins) / n
    # Expectancy per trade in % of capital-at-risk-equivalent (return-based).
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss
    wl_ratio = (avg_win / abs(avg_loss)) if avg_loss != 0 else None
    return {
        "num_trades": n,
        "num_wins": len(wins),
        "num_losses": len(losses),
        "win_rate_pct": round(win_rate * 100, 2),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "win_loss_ratio": round(wl_ratio, 2) if wl_ratio is not None else None,
        "expectancy_pct_per_trade": round(expectancy, 3),
        "avg_holding_days": round(sum(t["holding_days"] for t in trades) / n, 1),
        "total_pnl_usd": round(sum(t["pnl_usd"] for t in trades), 2),
        "exits_stop": sum(1 for t in trades if t["exit_reason"] == "stop"),
        "exits_target": sum(1 for t in trades if t["exit_reason"] == "target"),
        "pyramid_trades": sum(1 for t in trades if t.get("is_pyramid")),
    }


def plot_equity(strategy_curve: list[dict], spy_curve: list[dict], out_path: str,
                title: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    sdates = [datetime.date.fromisoformat(p["date"]) for p in strategy_curve]
    sval = [p["equity"] for p in strategy_curve]
    bdates = [datetime.date.fromisoformat(p["date"]) for p in spy_curve]
    bval = [p["equity"] for p in spy_curve]

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(sdates, sval, label="Composite strategy (baseline)", color="#0a7", linewidth=1.6)
    ax.plot(bdates, bval, label="SPY buy & hold", color="#888", linewidth=1.3, linestyle="--")
    ax.set_title(title)
    ax.set_ylabel("Portfolio value ($)")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper left")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def spy_buy_hold(store, start_date: str, end_date: str, start_equity: float) -> list[dict]:
    """SPY buy-and-hold curve over the strategy window, marked daily."""
    import datetime as _dt
    bars = store.series.get("SPY", [])
    window = [b for b in bars if start_date <= b["date"] <= end_date]
    if not window:
        return []
    entry = window[0]["open"] if window[0].get("open") else window[0]["close"]
    shares = start_equity / entry
    return [{"date": b["date"], "equity": round(shares * b["close"], 2)} for b in window]

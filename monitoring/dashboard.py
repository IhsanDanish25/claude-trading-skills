"""Live monitoring dashboard for the trading bot.

Runs as a SEPARATE process from the worker/scheduler:

    streamlit run monitoring/dashboard.py

Reads two kinds of data:
  1. Live account/positions straight from Alpaca (core.broker.BrokerClient) —
     always current, never stale.
  2. The bot's own on-disk state under STATE_DIR — day_start_value.json,
     trade_log.jsonl, skipped_routines.json, daily_log_{date}.json — the
     same files routines/*.py already read and write, so this dashboard
     stays honest about what the bot last actually did even if the broker
     call above disagrees (e.g. mid-flatten, or during an Alpaca outage).

Adapted from regime-trader's monitoring/dashboard.py, which assumed a single
live_state.json written by a main.py entrypoint this bot doesn't have —
this version reads the state files this bot actually writes instead.

Requires DASHBOARD_PASSWORD to be set (any non-empty value) — the whole app
is gated behind it (see check_password()) since it's reachable on a public
Railway domain and shows live account data with no other authentication.
"""

import glob
import hmac
import json
import os
import sys
from pathlib import Path

# Streamlit runs this script with only monitoring/ on sys.path, not the
# project root — add it explicitly so `core`/`config` resolve regardless
# of cwd or how `streamlit run` was invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

import core.config as config

st.set_page_config(page_title="trading-bot monitor", layout="wide")

REFRESH_SECONDS = int(os.environ.get("DASHBOARD_REFRESH_SECONDS", "30"))


# ──────────────────────────────────────────────────────────────────────────
# Auth — Streamlit has no built-in login, and this is exposed on a public
# Railway domain for mobile access. DASHBOARD_PASSWORD gates the whole app so
# a leaked/guessed URL doesn't hand a stranger live portfolio value and
# positions. Deliberately fails closed: an unset password refuses to serve
# rather than defaulting open.
# ──────────────────────────────────────────────────────────────────────────
def _password_matches(entered: str, expected: str) -> bool:
    """Constant-time comparison so response timing can't leak the password."""
    return hmac.compare_digest(entered, expected)


def check_password() -> bool:
    expected = os.environ.get("DASHBOARD_PASSWORD", "")
    if not expected:
        st.error("DASHBOARD_PASSWORD is not set — refusing to serve the dashboard unauthenticated.")
        st.stop()

    if st.session_state.get("password_correct"):
        return True

    def _on_submit():
        entered = st.session_state.get("password_input", "")
        st.session_state["password_correct"] = _password_matches(entered, expected)
        if "password_input" in st.session_state:
            del st.session_state["password_input"]

    st.text_input("Password", type="password", on_change=_on_submit, key="password_input")
    if st.session_state.get("password_correct") is False:
        st.error("Incorrect password")
    return False


# ──────────────────────────────────────────────────────────────────────────
# Data loading — broker (live) + on-disk state (bot's own record)
# ──────────────────────────────────────────────────────────────────────────
@st.cache_resource
def _get_broker():
    """One BrokerClient per Streamlit session, not per rerun — avoids
    reconnecting to Alpaca every 30s refresh."""
    from core.broker import BrokerClient

    return BrokerClient()


def load_account():
    """Returns (account_dict, positions_list, error_str_or_None). Never
    raises — a broker/network hiccup should degrade the dashboard, not
    crash it."""
    try:
        broker = _get_broker()
        account = broker.get_account()
        positions = broker.get_positions()
        return (
            {
                "portfolio_value": float(account.portfolio_value),
                "cash": float(account.cash),
                "buying_power": float(account.buying_power),
            },
            positions,
            None,
        )
    except Exception as exc:
        return None, [], str(exc)


def load_day_start_value():
    path = os.path.join(config.STATE_DIR, "day_start_value.json")
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("value"), data.get("date")
    except (FileNotFoundError, ValueError, KeyError):
        return None, None


def load_skipped_routines():
    path = os.path.join(config.STATE_DIR, "skipped_routines.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {"date": None, "skipped": []}


def load_trade_log(tail: int = 200) -> pd.DataFrame:
    path = os.path.join(config.STATE_DIR, "trade_log.jsonl")
    if not os.path.exists(path):
        return pd.DataFrame()
    rows = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "ts" in df.columns:
        df = df.sort_values("ts")
    return df.tail(tail)


def load_daily_logs(days: int = 30) -> pd.DataFrame:
    """Loads the most recent daily_log_YYYY-MM-DD.json files market_close.py
    writes — gives an equity-curve-ish history without needing a new state
    file. Sparse by design: only exists for days the bot actually closed."""
    pattern = os.path.join(config.STATE_DIR, "daily_log_*.json")
    paths = sorted(glob.glob(pattern))[-days:]
    rows = []
    for p in paths:
        try:
            with open(p) as f:
                rows.append(json.load(f))
        except (OSError, ValueError):
            continue
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "date" in df.columns:
        df = df.sort_values("date")
    return df


# ──────────────────────────────────────────────────────────────────────────
# Render sections
# ──────────────────────────────────────────────────────────────────────────
def render_account_summary(account: dict, day_start_value, day_start_date, error: str):
    st.subheader("Account")
    if error:
        st.error(f"Couldn't reach Alpaca: {error}")
        return

    cols = st.columns(4)
    cols[0].metric("Portfolio value", f"${account['portfolio_value']:,.2f}")
    cols[1].metric("Cash", f"${account['cash']:,.2f}")
    cols[2].metric("Buying power", f"${account['buying_power']:,.2f}")

    if day_start_value:
        day_pnl = account["portfolio_value"] - float(day_start_value)
        day_pnl_pct = day_pnl / float(day_start_value) if day_start_value else 0
        cols[3].metric(
            "Day P&L",
            f"${day_pnl:+,.2f}",
            delta=f"{day_pnl_pct:+.2%}",
        )
        st.caption(f"Day-start value ${float(day_start_value):,.2f} recorded {day_start_date}")
    else:
        cols[3].metric("Day P&L", "—")
        st.caption("No day-start value recorded yet today (market_open.py hasn't run)")


def render_risk_config():
    st.subheader("Risk config")
    cols = st.columns(4)
    cols[0].metric("Active strategies", ", ".join(config.STRATEGY_MODES) or "—")
    cols[1].metric("Max open positions", config.MAX_OPEN_POSITIONS)
    cols[2].metric("Max position size", f"{config.MAX_POSITION_SIZE_PCT:.1%}")
    cols[3].metric("Circuit breaker", f"{config.CIRCUIT_BREAKER_PCT:.1%}")


def render_positions_table(positions: list):
    st.subheader(f"Open positions ({len(positions)})")
    if not positions:
        st.info("No open positions.")
        return
    rows = []
    for p in positions:
        rows.append(
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "avg_entry": float(p.avg_entry_price),
                "current_price": float(p.current_price) if p.current_price else None,
                "market_value": float(p.market_value) if p.market_value else None,
                "unrealized_pl": float(p.unrealized_pl) if p.unrealized_pl else None,
                "unrealized_pl_pct": float(p.unrealized_plpc) if p.unrealized_plpc else None,
            }
        )
    df = pd.DataFrame(rows)
    df["unrealized_pl_pct"] = df["unrealized_pl_pct"].map(
        lambda x: f"{x:+.2%}" if x is not None else "—"
    )
    st.dataframe(df, use_container_width=True, hide_index=True)


def render_skipped_routines(skipped: dict):
    st.subheader("Routine health (today)")
    entries = skipped.get("skipped") or []
    if not entries:
        st.success("No skipped routines recorded today.")
        return
    st.warning(f"{len(entries)} routine(s) skipped today — see Railway logs for full context.")
    st.dataframe(pd.DataFrame(entries), use_container_width=True, hide_index=True)


def render_daily_history(daily_df: pd.DataFrame):
    st.subheader("Recent daily closes")
    if daily_df.empty:
        st.info("No daily_log files found yet — market_close.py writes one per trading day.")
        return
    chart_cols = [c for c in ["portfolio_value", "unrealized_pnl"] if c in daily_df.columns]
    if chart_cols:
        st.line_chart(daily_df.set_index("date")[chart_cols])
    st.dataframe(daily_df.iloc[::-1], use_container_width=True, hide_index=True)


def render_trade_log(trade_df: pd.DataFrame):
    st.subheader("Trade / decision log (recent)")
    if trade_df.empty:
        st.info("No entries in trade_log.jsonl yet.")
        return
    st.dataframe(trade_df.iloc[::-1], use_container_width=True, hide_index=True)


def render_dashboard():
    if not check_password():
        return

    st.title("trading-bot — live monitor")

    account, positions, error = load_account()
    day_start_value, day_start_date = load_day_start_value()
    skipped = load_skipped_routines()
    trade_df = load_trade_log()
    daily_df = load_daily_logs()

    render_account_summary(account or {}, day_start_value, day_start_date, error)
    render_risk_config()
    render_positions_table(positions)
    render_skipped_routines(skipped)
    render_daily_history(daily_df)
    render_trade_log(trade_df)

    st.caption(f"Auto-refreshing every {REFRESH_SECONDS}s — STATE_DIR={config.STATE_DIR}")

    import time

    time.sleep(REFRESH_SECONDS)
    st.rerun()


if __name__ == "__main__":
    render_dashboard()

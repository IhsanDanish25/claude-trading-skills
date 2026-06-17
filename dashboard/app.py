#!/usr/bin/env python3
"""Railway Trading Dashboard — Streamlit app.

Displays Alpaca account overview, positions, orders, P&L,
TradingView indicators, and routine schedule in one view.

Usage:
    streamlit run dashboard/app.py --server.port 8501
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

_root = Path(__file__).resolve().parents[1]
_scripts = str(_root / "scripts")
_skills_tv = str(_root / "skills" / "tradingview-analyzer" / "scripts")
if _scripts not in sys.path:
    sys.path.insert(0, _scripts)
if _skills_tv not in sys.path:
    sys.path.insert(0, _skills_tv)

from alpaca_client import AlpacaClient

st.set_page_config(
    page_title="Trading Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

ROUTINE_SCHEDULE = [
    ("Pre-Market", "pre_market", "06:00 UTC", "Mon-Fri"),
    ("Market Open", "market_open", "08:30 UTC", "Mon-Fri"),
    ("Midday Review", "midday_review", "12:00 UTC", "Mon-Fri"),
    ("Market Close", "market_close", "15:00 UTC", "Mon-Fri"),
    ("Weekly Review", "weekly_review", "16:00 UTC", "Friday"),
]


@st.cache_resource
def get_client() -> AlpacaClient:
    return AlpacaClient()


def load_account(client: AlpacaClient) -> dict | None:
    try:
        return client.get_account()
    except Exception as e:
        st.error(f"Failed to connect to Alpaca: {e}")
        return None


def load_positions(client: AlpacaClient) -> list[dict]:
    try:
        return client.get_positions()
    except Exception:
        return []


def load_orders(client: AlpacaClient) -> list[dict]:
    try:
        return client.get_orders(status="open", limit=20)
    except Exception:
        return []


def load_tv_indicators(symbols: list[str]) -> list[dict]:
    try:
        from tv_scanner import fetch_multi
        return fetch_multi(symbols, exchange="NASDAQ", interval="1d")
    except Exception:
        return []


def render_header(account: dict, mode: str):
    st.title("Trading Dashboard")
    mode_color = "green" if mode == "paper" else "red"
    st.markdown(f"**Mode:** :{mode_color}[{mode.upper()}] &nbsp; | &nbsp; "
                f"**Status:** {account.get('status', 'unknown')} &nbsp; | &nbsp; "
                f"**Updated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")


def render_account_metrics(account: dict):
    equity = float(account.get("equity", 0))
    cash = float(account.get("cash", 0))
    buying_power = float(account.get("buying_power", 0))
    portfolio_value = float(account.get("portfolio_value", 0))
    last_equity = float(account.get("last_equity", 0))

    day_change = equity - last_equity if last_equity > 0 else 0
    day_change_pct = (day_change / last_equity * 100) if last_equity > 0 else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Equity", f"${equity:,.2f}", f"{day_change:+,.2f} ({day_change_pct:+.2f}%)")
    c2.metric("Cash", f"${cash:,.2f}")
    c3.metric("Buying Power", f"${buying_power:,.2f}")
    c4.metric("Portfolio Value", f"${portfolio_value:,.2f}")


def render_positions(positions: list[dict]):
    st.subheader("Positions")
    if not positions:
        st.info("No open positions.")
        return

    rows = []
    total_pl = 0.0
    for p in positions:
        sym = p.get("symbol", "?")
        qty = float(p.get("qty", 0))
        avg_entry = float(p.get("avg_entry_price", 0))
        current = float(p.get("current_price", 0))
        market_value = float(p.get("market_value", 0))
        unrealized_pl = float(p.get("unrealized_pl", 0))
        unrealized_plpc = float(p.get("unrealized_plpc", 0)) * 100
        total_pl += unrealized_pl

        rows.append({
            "Symbol": sym,
            "Qty": qty,
            "Avg Entry": f"${avg_entry:.2f}",
            "Current": f"${current:.2f}",
            "Market Value": f"${market_value:,.2f}",
            "P&L": f"${unrealized_pl:+,.2f}",
            "P&L %": f"{unrealized_plpc:+.2f}%",
        })

    st.dataframe(rows, use_container_width=True, hide_index=True)

    color = "green" if total_pl >= 0 else "red"
    st.markdown(f"**Total Unrealized P&L:** :{color}[${total_pl:+,.2f}]")

    return [p.get("symbol", "") for p in positions]


def render_orders(orders: list[dict]):
    st.subheader("Open Orders")
    if not orders:
        st.info("No open orders.")
        return

    rows = []
    for o in orders:
        rows.append({
            "Symbol": o.get("symbol", "?"),
            "Side": o.get("side", "?").upper(),
            "Type": o.get("type", "?"),
            "Qty": o.get("qty", "?"),
            "Limit": o.get("limit_price", "—"),
            "Stop": o.get("stop_price", "—"),
            "Status": o.get("status", "?"),
            "Submitted": o.get("submitted_at", "?")[:16],
        })

    st.dataframe(rows, use_container_width=True, hide_index=True)


def render_tv_signals(tv_data: list[dict]):
    st.subheader("TradingView Signals")
    if not tv_data:
        st.info("No TradingView data available.")
        return

    rows = []
    for d in tv_data:
        ind = d.get("indicators", {})
        summary = d.get("summary", {})
        rec = summary.get("RECOMMENDATION", "N/A")

        rsi = ind.get("RSI")
        macd = ind.get("MACD.macd")
        macd_sig = ind.get("MACD.signal")

        rows.append({
            "Symbol": d.get("symbol", "?"),
            "Signal": rec,
            "Buy": summary.get("BUY", 0),
            "Sell": summary.get("SELL", 0),
            "Neutral": summary.get("NEUTRAL", 0),
            "RSI": f"{rsi:.1f}" if rsi is not None else "—",
            "MACD": f"{macd:.3f}" if macd is not None else "—",
            "MACD Signal": f"{macd_sig:.3f}" if macd_sig is not None else "—",
            "Price": f"${ind.get('close', 0):,.2f}" if ind.get("close") else "—",
        })

    st.dataframe(rows, use_container_width=True, hide_index=True)


def render_schedule():
    st.subheader("Routine Schedule")
    rows = []
    for name, routine, time, days in ROUTINE_SCHEDULE:
        rows.append({
            "Routine": name,
            "ID": routine,
            "Time": time,
            "Days": days,
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)


def main():
    client = get_client()

    if not client.is_configured():
        st.error("Alpaca credentials not configured.")
        st.code(client.setup_hint())
        return

    account = load_account(client)
    if not account:
        return

    render_header(account, client.mode_label)
    st.divider()

    render_account_metrics(account)
    st.divider()

    tab_positions, tab_orders, tab_signals, tab_schedule = st.tabs(
        ["Positions", "Orders", "TradingView Signals", "Schedule"]
    )

    positions = load_positions(client)
    orders = load_orders(client)

    with tab_positions:
        held_symbols = render_positions(positions)

    with tab_orders:
        render_orders(orders)

    with tab_signals:
        symbols = [p.get("symbol", "") for p in positions if p.get("symbol")]
        extra = st.text_input("Add symbols (comma-separated)", placeholder="AAPL,MSFT,TSLA")
        if extra:
            symbols.extend([s.strip().upper() for s in extra.split(",") if s.strip()])
        symbols = list(dict.fromkeys(symbols))

        if symbols:
            with st.spinner("Fetching TradingView indicators..."):
                tv_data = load_tv_indicators(symbols)
            render_tv_signals(tv_data)
        else:
            st.info("No symbols to analyze. Open positions or enter symbols above.")

    with tab_schedule:
        render_schedule()

    with st.sidebar:
        st.markdown("### Quick Actions")
        if st.button("Refresh Data"):
            st.cache_resource.clear()
            st.rerun()

        st.divider()
        st.markdown(f"**Account ID:** `{account.get('id', 'N/A')}`")
        st.markdown(f"**Day Trades:** {account.get('daytrade_count', 0)}/3")
        pdt = account.get("pattern_day_trader", False)
        st.markdown(f"**PDT Flag:** {'Yes' if pdt else 'No'}")


if __name__ == "__main__":
    main()

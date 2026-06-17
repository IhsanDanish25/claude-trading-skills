#!/usr/bin/env python3
"""TradingView indicator fetcher and screener scanner.

Fetches real-time technical indicators and screener data from TradingView.
No API key required.

Usage:
    # Single symbol indicators
    python3 tv_scanner.py --symbols AAPL --exchange NASDAQ

    # Multiple symbols, weekly interval
    python3 tv_scanner.py --symbols AAPL,MSFT,GOOGL --exchange NASDAQ --interval 1W

    # Screener scan
    python3 tv_scanner.py --screener america --scan oversold --top 20

    # JSON output only
    python3 tv_scanner.py --symbols AAPL --exchange NASDAQ --format json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tradingview_ta import TA_Handler, Interval

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

INTERVAL_MAP: dict[str, str] = {
    "1m": Interval.INTERVAL_1_MINUTE,
    "5m": Interval.INTERVAL_5_MINUTES,
    "15m": Interval.INTERVAL_15_MINUTES,
    "30m": Interval.INTERVAL_30_MINUTES,
    "1h": Interval.INTERVAL_1_HOUR,
    "2h": Interval.INTERVAL_2_HOURS,
    "4h": Interval.INTERVAL_4_HOURS,
    "1d": Interval.INTERVAL_1_DAY,
    "1W": Interval.INTERVAL_1_WEEK,
    "1M": Interval.INTERVAL_1_MONTH,
}

DEFAULT_EXCHANGE = "NASDAQ"
DEFAULT_SCREENER = "america"
DEFAULT_INTERVAL = "1d"
DEFAULT_TOP = 20

SCAN_PRESETS: dict[str, dict[str, Any]] = {
    "oversold": {
        "description": "RSI < 30 — potential bounce candidates",
        "filter_fn": lambda ind: ind.get("RSI", 50) < 30,
    },
    "overbought": {
        "description": "RSI > 70 — potential pullback candidates",
        "filter_fn": lambda ind: ind.get("RSI", 50) > 70,
    },
    "strong_buy": {
        "description": "TradingView aggregate recommendation = STRONG_BUY",
        "recommendation": "STRONG_BUY",
    },
    "strong_sell": {
        "description": "TradingView aggregate recommendation = STRONG_SELL",
        "recommendation": "STRONG_SELL",
    },
    "trending_up": {
        "description": "Price above SMA 50 and SMA 200, SMA 50 > SMA 200",
        "filter_fn": lambda ind: (
            ind.get("close", 0) > ind.get("SMA50", 0) > ind.get("SMA200", 0)
            and ind.get("SMA50", 0) > 0
            and ind.get("SMA200", 0) > 0
        ),
    },
    "trending_down": {
        "description": "Price below SMA 50 and SMA 200, SMA 50 < SMA 200",
        "filter_fn": lambda ind: (
            0 < ind.get("close", 0) < ind.get("SMA50", float("inf"))
            and ind.get("SMA50", float("inf")) < ind.get("SMA200", float("inf"))
        ),
    },
}

KEY_INDICATORS = [
    "RSI",
    "RSI[1]",
    "MACD.macd",
    "MACD.signal",
    "Stoch.K",
    "Stoch.D",
    "ADX",
    "CCI20",
    "AO",
    "Mom",
    "W.R",
    "UO",
    "BB.upper",
    "BB.lower",
    "close",
    "open",
    "high",
    "low",
    "volume",
    "change",
    "Pivot.M.Classic.Middle",
    "Pivot.M.Classic.R1",
    "Pivot.M.Classic.S1",
    "SMA10",
    "SMA20",
    "SMA50",
    "SMA100",
    "SMA200",
    "EMA10",
    "EMA20",
    "EMA50",
    "EMA100",
    "EMA200",
]


def fetch_analysis(
    symbol: str,
    exchange: str = DEFAULT_EXCHANGE,
    screener: str = DEFAULT_SCREENER,
    interval: str = DEFAULT_INTERVAL,
) -> dict[str, Any]:
    """Fetch TradingView analysis for a single symbol."""
    tv_interval = INTERVAL_MAP.get(interval)
    if not tv_interval:
        raise ValueError(f"Invalid interval '{interval}'. Valid: {list(INTERVAL_MAP.keys())}")

    handler = TA_Handler(
        symbol=symbol.upper(),
        screener=screener,
        exchange=exchange.upper(),
        interval=tv_interval,
    )
    analysis = handler.get_analysis()

    indicators = {}
    for key in KEY_INDICATORS:
        val = analysis.indicators.get(key)
        if val is not None:
            indicators[key] = round(val, 4) if isinstance(val, float) else val

    return {
        "symbol": symbol.upper(),
        "exchange": exchange.upper(),
        "interval": interval,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": analysis.summary,
        "oscillators": analysis.oscillators,
        "moving_averages": analysis.moving_averages,
        "indicators": indicators,
    }


def fetch_multi(
    symbols: list[str],
    exchange: str = DEFAULT_EXCHANGE,
    screener: str = DEFAULT_SCREENER,
    interval: str = DEFAULT_INTERVAL,
) -> list[dict[str, Any]]:
    """Fetch analysis for multiple symbols. Skips failures with warnings."""
    results = []
    for sym in symbols:
        try:
            data = fetch_analysis(sym, exchange, screener, interval)
            results.append(data)
            logger.info("Fetched %s: %s", sym, data["summary"]["RECOMMENDATION"])
        except Exception as exc:
            logger.warning("Failed to fetch %s: %s", sym, exc)
    return results


def run_scan(
    preset: str,
    symbols: list[str],
    exchange: str = DEFAULT_EXCHANGE,
    screener: str = DEFAULT_SCREENER,
    interval: str = DEFAULT_INTERVAL,
    top: int = DEFAULT_TOP,
) -> list[dict[str, Any]]:
    """Run a scan preset against a list of symbols."""
    if preset not in SCAN_PRESETS:
        raise ValueError(f"Unknown scan preset '{preset}'. Valid: {list(SCAN_PRESETS.keys())}")

    config = SCAN_PRESETS[preset]
    results = fetch_multi(symbols, exchange, screener, interval)

    filtered = []
    for r in results:
        if "recommendation" in config:
            if r["summary"]["RECOMMENDATION"] == config["recommendation"]:
                filtered.append(r)
        elif "filter_fn" in config:
            if config["filter_fn"](r["indicators"]):
                filtered.append(r)

    filtered.sort(key=lambda x: x["indicators"].get("RSI", 50))
    return filtered[:top]


def format_markdown_report(results: list[dict[str, Any]], title: str) -> str:
    """Format analysis results as a markdown report."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# {title}", f"**Generated:** {now}\n"]

    for r in results:
        sym = r["symbol"]
        summary = r["summary"]
        rec = summary["RECOMMENDATION"]
        buy_count = summary["BUY"]
        sell_count = summary["SELL"]
        neutral_count = summary["NEUTRAL"]
        ind = r["indicators"]

        lines.append(f"## {sym} ({r['exchange']}) — {r['interval']}")
        lines.append("")
        lines.append(f"**Recommendation:** {rec} "
                      f"(Buy: {buy_count}, Sell: {sell_count}, Neutral: {neutral_count})")
        lines.append("")

        # Price info
        close = ind.get("close")
        change = ind.get("change")
        if close is not None:
            change_str = f" ({change:+.2f}%)" if change is not None else ""
            lines.append(f"**Price:** ${close:,.2f}{change_str}")
            lines.append("")

        # Oscillators table
        osc = r.get("oscillators", {})
        osc_rec = osc.get("RECOMMENDATION", "N/A")
        lines.append(f"### Oscillators — {osc_rec}")
        lines.append("")
        lines.append("| Indicator | Value | Signal |")
        lines.append("|---|---|---|")

        osc_compute = osc.get("COMPUTE", {})
        for name, val in sorted(osc_compute.items()):
            signal = "BUY" if val > 0 else ("SELL" if val < 0 else "NEUTRAL")
            ind_val = ind.get(name, ind.get(f"{name}.macd", "—"))
            if isinstance(ind_val, float):
                ind_val = f"{ind_val:.4f}"
            lines.append(f"| {name} | {ind_val} | {signal} |")
        lines.append("")

        # Moving Averages table
        ma = r.get("moving_averages", {})
        ma_rec = ma.get("RECOMMENDATION", "N/A")
        lines.append(f"### Moving Averages — {ma_rec}")
        lines.append("")
        lines.append("| MA | Value | Price vs MA |")
        lines.append("|---|---|---|")
        for period in ["10", "20", "50", "100", "200"]:
            sma = ind.get(f"SMA{period}")
            ema = ind.get(f"EMA{period}")
            if sma is not None and close is not None:
                rel = "Above" if close > sma else "Below"
                lines.append(f"| SMA {period} | ${sma:,.2f} | {rel} |")
            if ema is not None and close is not None:
                rel = "Above" if close > ema else "Below"
                lines.append(f"| EMA {period} | ${ema:,.2f} | {rel} |")
        lines.append("")

        # Key levels
        rsi = ind.get("RSI")
        macd_val = ind.get("MACD.macd")
        macd_sig = ind.get("MACD.signal")
        stoch_k = ind.get("Stoch.K")

        lines.append("### Key Readings")
        lines.append("")
        if rsi is not None:
            status = "OVERBOUGHT" if rsi > 70 else ("OVERSOLD" if rsi < 30 else "Neutral")
            lines.append(f"- **RSI(14):** {rsi:.2f} — {status}")
        if macd_val is not None and macd_sig is not None:
            cross = "Bullish" if macd_val > macd_sig else "Bearish"
            lines.append(f"- **MACD:** {macd_val:.4f} / Signal: {macd_sig:.4f} — {cross}")
        if stoch_k is not None:
            status = "Overbought" if stoch_k > 80 else ("Oversold" if stoch_k < 20 else "Neutral")
            lines.append(f"- **Stochastic %K:** {stoch_k:.2f} — {status}")

        pivot = ind.get("Pivot.M.Classic.Middle")
        r1 = ind.get("Pivot.M.Classic.R1")
        s1 = ind.get("Pivot.M.Classic.S1")
        if pivot:
            lines.append(f"- **Pivot:** ${pivot:,.2f} | R1: ${r1:,.2f} | S1: ${s1:,.2f}")

        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def write_outputs(
    results: list[dict[str, Any]],
    title: str,
    filename_base: str,
    output_dir: Path,
    fmt: str = "both",
) -> list[Path]:
    """Write JSON and/or markdown output files."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written = []

    if fmt in ("json", "both"):
        json_path = output_dir / f"{filename_base}.json"
        json_path.write_text(json.dumps(results, indent=2, default=str))
        written.append(json_path)
        logger.info("Wrote %s", json_path)

    if fmt in ("md", "both"):
        md_path = output_dir / f"{filename_base}.md"
        md_path.write_text(format_markdown_report(results, title))
        written.append(md_path)
        logger.info("Wrote %s", md_path)

    return written


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TradingView indicator fetcher and screener")
    parser.add_argument("--symbols", type=str, help="Comma-separated ticker symbols (e.g. AAPL,MSFT)")
    parser.add_argument("--exchange", type=str, default=DEFAULT_EXCHANGE, help="Exchange code")
    parser.add_argument("--screener", type=str, default=DEFAULT_SCREENER, help="Screener region")
    parser.add_argument("--interval", type=str, default=DEFAULT_INTERVAL,
                        choices=list(INTERVAL_MAP.keys()), help="Timeframe interval")
    parser.add_argument("--scan", type=str, choices=list(SCAN_PRESETS.keys()),
                        help="Run a screener scan preset")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, help="Max results for scans")
    parser.add_argument("--format", type=str, default="both", choices=["json", "md", "both"],
                        help="Output format")
    parser.add_argument("--output-dir", type=str, default="reports/",
                        help="Output directory for reports")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = Path(args.output_dir)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not args.symbols and not args.scan:
        logger.error("Provide --symbols or --scan. Use --help for usage.")
        return 1

    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else []

    if args.scan:
        if not symbols:
            logger.error("--scan requires --symbols (list of tickers to scan against)")
            return 1
        logger.info("Running scan: %s on %d symbols", args.scan, len(symbols))
        results = run_scan(
            args.scan, symbols, args.exchange, args.screener, args.interval, args.top
        )
        title = f"TradingView Scan: {args.scan.replace('_', ' ').title()}"
        filename = f"tradingview_scan_{args.scan}_{today}"
    else:
        logger.info("Fetching indicators for %d symbols", len(symbols))
        results = fetch_multi(symbols, args.exchange, args.screener, args.interval)
        sym_label = "_".join(symbols[:3])
        if len(symbols) > 3:
            sym_label += f"_+{len(symbols) - 3}"
        title = f"TradingView Analysis: {', '.join(symbols)}"
        filename = f"tradingview_indicators_{sym_label}_{today}"

    if not results:
        logger.warning("No results returned.")
        return 1

    write_outputs(results, title, filename, output_dir, args.format)
    return 0


if __name__ == "__main__":
    sys.exit(main())

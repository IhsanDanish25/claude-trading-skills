---
layout: default
title: "Tradingview Analyzer"
grand_parent: English
parent: Skill Guides
nav_order: 11
lang_peer: /ja/skills/tradingview-analyzer/
permalink: /en/skills/tradingview-analyzer/
generated: true
---

# Tradingview Analyzer
{: .no_toc }

Use when the user asks for TradingView technical analysis, indicator data, screener scans, or provides TradingView chart screenshots for interpretation. Fetches real-time TradingView indicators (RSI, MACD, EMA, oscillators) and screener results without an API key. Also analyzes TradingView chart images when provided.
{: .fs-6 .fw-300 }

<span class="badge badge-free">No API</span>

[View Source on GitHub](https://github.com/tradermonty/claude-trading-skills/tree/main/skills/tradingview-analyzer){: .btn .fs-5 .mb-4 .mb-md-0 }

<details open markdown="block">
  <summary>Table of Contents</summary>
  {: .text-delta }
- TOC
{:toc}
</details>

---

## 1. Overview

Fetch real-time technical indicators, oscillator readings, and moving average data directly from TradingView's computation engine. Run multi-market screener scans to find stocks matching technical criteria. Optionally analyze user-provided TradingView chart screenshots for pattern recognition and setup evaluation.

This skill operates in two modes:
1. **Data Mode** — Programmatic indicator fetch and screener scans (no API key required)
2. **Chart Mode** — Visual analysis of TradingView chart screenshots provided by the user

---

## 2. When to Use

- User asks for TradingView indicators on a specific ticker (RSI, MACD, EMA, etc.)
- User wants a TradingView-style technical summary (buy/sell/neutral recommendation)
- User asks to scan/screen stocks using TradingView criteria
- User provides a TradingView chart screenshot for analysis
- User wants to compare TradingView signals across multiple tickers
- Need real-time oscillator and moving average readings without FMP API

---

## 3. Prerequisites

- **No API Key Required** — Uses TradingView's public computation engine
- **Python Package**: `tradingview_ta` (listed in project dependencies)
- **Chart Mode**: User must provide chart screenshot images

---

## 4. Quick Start

```bash
python3 skills/tradingview-analyzer/scripts/tv_scanner.py \
  --symbols AAPL,MSFT,GOOGL \
  --exchange NASDAQ \
  --interval 1W \
  --output-dir reports/
```

---

## 5. Workflow

### Data Mode: Indicator Fetch

1. Identify the ticker(s) and exchange. Default to NASDAQ for US stocks.
2. Run the scanner script:

```bash
python3 skills/tradingview-analyzer/scripts/tv_scanner.py \
  --symbols AAPL,MSFT,GOOGL \
  --exchange NASDAQ \
  --interval 1W \
  --output-dir reports/
```

3. Read the JSON output for programmatic use or the markdown report for human review.
4. Load the indicator interpretation guide if the user needs explanation:

```
Read: references/tradingview_indicators_guide.md
```

5. Synthesize findings: highlight convergence/divergence across indicators, flag overbought/oversold conditions, note any strong buy/sell signals.

### Data Mode: Screener Scan

1. Run the screener to find stocks matching technical criteria:

```bash
python3 skills/tradingview-analyzer/scripts/tv_scanner.py \
  --screener america \
  --scan oversold \
  --top 20 \
  --output-dir reports/
```

Available scan presets: `oversold`, `overbought`, `strong_buy`, `strong_sell`, `high_volume`, `trending_up`, `trending_down`

2. Review results and cross-reference with other skills (position-sizer, technical-analyst) as needed.

### Chart Mode: Screenshot Analysis

1. Confirm receipt of TradingView chart screenshot(s).
2. Load the analysis framework:

```
Read: references/tradingview_indicators_guide.md
```

3. Analyze each chart systematically:
   - Identify the timeframe and symbol from the chart header
   - Read all visible indicators and their current values
   - Assess trend direction from price action and moving averages
   - Note any divergences between price and oscillators
   - Identify support/resistance levels visible on the chart
   - Check volume patterns if volume pane is visible

4. Optionally fetch live indicator data for the same symbol to complement visual analysis:

```bash
python3 skills/tradingview-analyzer/scripts/tv_scanner.py \
  --symbols <SYMBOL> --exchange <EXCHANGE> --interval <TIMEFRAME>
```

5. Generate a combined report with visual observations and live data.

---

## 6. Resources

**References:**

- `skills/tradingview-analyzer/references/tradingview_indicators_guide.md`

**Scripts:**

- `skills/tradingview-analyzer/scripts/tv_scanner.py`

---
layout: default
title: "Buffett Value"
grand_parent: English
parent: Skill Guides
nav_order: 11
lang_peer: /ja/skills/buffett-value/
permalink: /en/skills/buffett-value/
generated: true
---

# Buffett Value
{: .no_toc }

Screen for Buffett-style value stocks using business quality, consistency, and valuation filters with candlestick entry timing and profit/fundamentals-driven exits
{: .fs-6 .fw-300 }

<span class="badge badge-free">No API</span>

[View Source on GitHub](https://github.com/tradermonty/claude-trading-skills/tree/main/skills/buffett-value){: .btn .fs-5 .mb-4 .mb-md-0 }

<details open markdown="block">
  <summary>Table of Contents</summary>
  {: .text-delta }
- TOC
{:toc}
</details>

---

## 1. Overview

The Buffett Value strategy focuses on:
- **Business Quality**: High ROE, ROIC, low debt, stable margins (moat proxies)
- **Consistency**: Years of positive EPS growth, no dividend cuts
- **Valuation**: Margin of safety via intrinsic value calculation (Graham formula or DCF)
- **Entry Timing**: Candlestick patterns after passing fundamental screens
- **Exit Logic**: Profit-target or fundamentals-deterioration driven (no time-based exits)

---

## 2. When to Use

Use this skill when you want to:
- Find undervalued, high-quality companies for long-term holding
- Implement a value investing approach with specific buy/sell rules
- Screen for stocks with durable competitive advantages trading below intrinsic value
- Build a concentrated portfolio of conviction positions (5-10 max)

---

## 3. Prerequisites

- **API Key:** None required
- **Python 3.9+** recommended

---

## 4. Quick Start

### 1. Data Collection
- Fetches fundamental data via yfinance only -- no FMP API key required
- Gets price/volume data for candlestick analysis (also via yfinance)

---

## 5. Workflow

### 1. Data Collection
- Fetches fundamental data via yfinance only -- no FMP API key required
- Gets price/volume data for candlestick analysis (also via yfinance)

### 2. Business Quality Filter
Screens for:
- ROE > 15% (5-year average)
- ROIC > 12%
- Debt-to-equity < 0.5 OR interest coverage > 8x
- Gross margin stability (low standard deviation over 10 years)

### 3. Consistency Filter
Screens for:
- Positive EPS growth in ≥8 of last 10 years
- No dividend cuts (if dividend payer)

### 4. Valuation / Margin of Safety
Calculates:
- Intrinsic value using Graham formula: EPS × (8.5 + 2g) where g = 5-year EPS growth estimate
- Or simplified DCF approach
- Buy signal only when price ≤ 70-75% of intrinsic value (25-30% discount)
- Additional filter: P/E below stock's 10-year average P/E

### 5. Position Sizing (Conviction-Based)
- Concentrated portfolio: 5-10 positions maximum
- Size positions by conviction level, not fixed percentage
- Higher conviction = larger position size (within risk limits)

### 6. Entry Timing (Candlestick Gate)
Only executes AFTER stock passes all fundamental screens:
- Bullish engulfing pattern
- Hammer or inverted hammer
- Morning star (3-candle pattern)
- Doji at support (requires confirmation candle)
- BLOCK entry if bearish reversal candle appears same day (avoid falling knife)

### 7. Exit Logic (Profit/Fundamentals-Driven)
Exit when ANY of these occur:
- Price reaches/exceeds fair-value target (profit target, uncapped by time)
- Fundamentals deteriorate (ROE drops significantly, debt spikes, investment thesis broken)
- A materially better margin-of-safety opportunity appears (higher discount to IV)
- NO fixed holding period
- NO hard percentage stop-loss (unlike other strategies)

---

## 6. Resources

**Scripts:**

- `skills/buffett-value/scripts/analyst.py`
- `skills/buffett-value/scripts/buy.py`
- `skills/buffett-value/scripts/hold.py`
- `skills/buffett-value/scripts/sell.py`

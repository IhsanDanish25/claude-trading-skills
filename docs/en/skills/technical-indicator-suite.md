---
layout: default
title: "Technical Indicator Suite"
grand_parent: English
parent: Skill Guides
nav_order: 11
lang_peer: /ja/skills/technical-indicator-suite/
permalink: /en/skills/technical-indicator-suite/
generated: true
---

# Technical Indicator Suite
{: .no_toc }

Calculate RSI, MACD, Bollinger Bands, ATR, and EMA for any stock or ETF. Provides programmatic technical indicator values with buy/sell signal interpretation. Use when user asks for RSI, MACD, Bollinger Bands, ATR values, wants to calculate technical indicators for a specific stock, or needs signal-based entry/exit levels from indicators.
{: .fs-6 .fw-300 }

<span class="badge badge-free">No API</span>

[View Source on GitHub](https://github.com/tradermonty/claude-trading-skills/tree/main/skills/technical-indicator-suite){: .btn .fs-5 .mb-4 .mb-md-0 }

<details open markdown="block">
  <summary>Table of Contents</summary>
  {: .text-delta }
- TOC
{:toc}
</details>

---

## 1. Overview

# Technical Indicator Suite

---

## 2. When to Use

- User asks for RSI, MACD, or Bollinger Band values for a specific stock
- User wants to know if a stock is overbought or oversold by indicators
- User needs ATR for position sizing or stop placement
- User wants programmatic indicator values (not a chart image)

---

## 3. Prerequisites

- No API key required for yfinance data
- FMP API key optional for higher-quality OHLCV data

---

## 4. Quick Start

```bash
# Single symbol with all indicators
python3 skills/technical-indicator-suite/scripts/calculate_indicators.py \
  --symbol AAPL \
  --output-dir reports/

# Multiple symbols, specific indicators
python3 skills/technical-indicator-suite/scripts/calculate_indicators.py \
  --symbols AAPL NVDA MSFT \
  --indicators rsi macd bb atr \
  --rsi-period 14 \
  --output-dir reports/

# Screen for RSI < 30 or RSI > 70
python3 skills/technical-indicator-suite/scripts/calculate_indicators.py \
  --symbols AAPL NVDA MSFT AMD META \
  --rsi-oversold 30 \
  --rsi-overbought 70 \
  --output-dir reports/
```

---

## 5. Workflow

### Step 1: Calculate Indicators

```bash
# Single symbol with all indicators
python3 skills/technical-indicator-suite/scripts/calculate_indicators.py \
  --symbol AAPL \
  --output-dir reports/

# Multiple symbols, specific indicators
python3 skills/technical-indicator-suite/scripts/calculate_indicators.py \
  --symbols AAPL NVDA MSFT \
  --indicators rsi macd bb atr \
  --rsi-period 14 \
  --output-dir reports/

# Screen for RSI < 30 or RSI > 70
python3 skills/technical-indicator-suite/scripts/calculate_indicators.py \
  --symbols AAPL NVDA MSFT AMD META \
  --rsi-oversold 30 \
  --rsi-overbought 70 \
  --output-dir reports/
```

### Step 2: Interpret Signals

**RSI (14-period):**
- < 30: Oversold — potential mean reversion long
- > 70: Overbought — potential mean reversion short or trailing stop tightening
- Divergence (price new high, RSI lower high): bearish momentum divergence

**MACD (12/26/9):**
- MACD line crosses above signal: bullish momentum shift
- MACD histogram expanding above zero: trend acceleration
- Negative histogram shrinking: potential bullish reversal ahead

**Bollinger Bands (20/2σ):**
- Price at lower band + RSI < 35: high-probability mean reversion setup
- Bollinger Band squeeze (bandwidth < 5%): breakout imminent
- Price walks upper band: strong uptrend, hold longs

**ATR (14-period):**
- Stop = entry − (1.5 × ATR) for trend trades
- Stop = entry − (0.5 × ATR) for mean reversion
- Position size = Risk $ / ATR

### Step 3: Combine Signals

Signal confluence scoring:
- RSI + MACD crossover aligned: 2 points
- Price at support + BB lower: add 1 point
- Volume confirmation: add 1 point
- 3+ points = high-conviction setup

---

## 6. Resources

**Scripts:**

- `skills/technical-indicator-suite/scripts/calculate_indicators.py`

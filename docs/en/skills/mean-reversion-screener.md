---
layout: default
title: "Mean Reversion Screener"
grand_parent: English
parent: Skill Guides
nav_order: 11
lang_peer: /ja/skills/mean-reversion-screener/
permalink: /en/skills/mean-reversion-screener/
generated: true
---

# Mean Reversion Screener
{: .no_toc }

Screen for oversold stocks with high mean reversion probability using RSI, Bollinger Band position, and distance from moving averages. Identifies quality stocks in Stage 2 uptrends that have pulled back to support and show statistical reversion setups. Use when user asks for oversold stocks, mean reversion setups, pullback entries, RSI dip buys, or stocks bouncing off support levels.
{: .fs-6 .fw-300 }

<span class="badge badge-free">No API</span>

[View Source on GitHub](https://github.com/tradermonty/claude-trading-skills/tree/main/skills/mean-reversion-screener){: .btn .fs-5 .mb-4 .mb-md-0 }

<details open markdown="block">
  <summary>Table of Contents</summary>
  {: .text-delta }
- TOC
{:toc}
</details>

---

## 1. Overview

# Mean Reversion Screener

---

## 2. When to Use

- User asks for oversold stocks or pullback buy setups
- User wants RSI dip entries in uptrending stocks
- User asks for Bollinger Band lower-band touches with reversal signals
- User wants to buy dips in quality names that have pulled back to support

---

## 3. Prerequisites

- FMP API key (`FMP_API_KEY` environment variable or `--api-key`)
- Free tier (250 calls/day) sufficient for watchlist screening

---

## 4. Quick Start

```bash
# Screen watchlist (default 30 curated stocks)
python3 skills/mean-reversion-screener/scripts/screen_mean_reversion.py \
  --output-dir reports/

# Custom symbols with tighter RSI threshold
python3 skills/mean-reversion-screener/scripts/screen_mean_reversion.py \
  --symbols AAPL MSFT NVDA AMD META GOOGL AMZN TSLA \
  --rsi-max 35 \
  --min-pullback-pct 8.0 \
  --output-dir reports/

# Aggressive: RSI < 40 within 5% of 50-day MA
python3 skills/mean-reversion-screener/scripts/screen_mean_reversion.py \
  --rsi-max 40 \
  --max-distance-from-50d 5.0 \
  --output-dir reports/
```

---

## 5. Workflow

### Step 1: Run the Screener

```bash
# Screen watchlist (default 30 curated stocks)
python3 skills/mean-reversion-screener/scripts/screen_mean_reversion.py \
  --output-dir reports/

# Custom symbols with tighter RSI threshold
python3 skills/mean-reversion-screener/scripts/screen_mean_reversion.py \
  --symbols AAPL MSFT NVDA AMD META GOOGL AMZN TSLA \
  --rsi-max 35 \
  --min-pullback-pct 8.0 \
  --output-dir reports/

# Aggressive: RSI < 40 within 5% of 50-day MA
python3 skills/mean-reversion-screener/scripts/screen_mean_reversion.py \
  --rsi-max 40 \
  --max-distance-from-50d 5.0 \
  --output-dir reports/
```

### Step 2: Evaluate Candidates

**Quality filters (applied automatically):**
- Price > 200-day SMA (Stage 2 uptrend)
- RSI(14) < 40 (oversold relative to own history)
- Price within 2σ of lower Bollinger Band
- Volume declining on down days (healthy pullback, not distribution)

**Reversion score (0–100):**
- RSI component (35%): lower RSI = higher score
- BB position (30%): closer to lower band = higher score
- Distance from 50-day MA (20%): further below = higher score
- Volume profile (15%): low-volume pullback = higher score

### Step 3: Entry and Risk

- **Entry**: At current price or limit at Bollinger Band lower
- **Stop**: Below the recent swing low (1–3% below pullback low)
- **Target**: 50-day SMA (typical mean reversion target)
- **Risk/Reward**: Should be minimum 2:1 before entry

### Step 4: Avoid Traps

- Reject if: news-driven selloff (fundamental change)
- Reject if: broader market in downtrend (S&P 500 below 200-day)
- Reject if: earnings within 5 trading days (binary event risk)

---

## 6. Resources

**Scripts:**

- `skills/mean-reversion-screener/scripts/screen_mean_reversion.py`

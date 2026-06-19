---
layout: default
title: "Insider Buying Detector"
grand_parent: English
parent: Skill Guides
nav_order: 11
lang_peer: /ja/skills/insider-buying-detector/
permalink: /en/skills/insider-buying-detector/
generated: true
---

# Insider Buying Detector
{: .no_toc }

Detect significant insider buying activity (CEO, CFO, directors purchasing company stock in the open market). Filters for cluster buying, large dollar amounts, and purchases near 52-week lows as the strongest signals. Use when user asks about insider buying, what executives are buying, insider purchases, or wants to find stocks where management is buying their own shares.
{: .fs-6 .fw-300 }

<span class="badge badge-free">No API</span>

[View Source on GitHub](https://github.com/tradermonty/claude-trading-skills/tree/main/skills/insider-buying-detector){: .btn .fs-5 .mb-4 .mb-md-0 }

<details open markdown="block">
  <summary>Table of Contents</summary>
  {: .text-delta }
- TOC
{:toc}
</details>

---

## 1. Overview

# Insider Buying Detector

---

## 2. When to Use

- User asks what insiders are buying
- User wants to find stocks with recent executive purchases
- User asks for insider buying signals or SEC Form 4 analysis
- User wants management conviction signals for a stock thesis

---

## 3. Prerequisites

- FMP API key (`FMP_API_KEY` environment variable or `--api-key`)
- Free tier (250 calls/day) sufficient for recent transaction scans

---

## 4. Quick Start

```bash
# Default: last 30 days, all S&P 500 insider buys
python3 skills/insider-buying-detector/scripts/detect_insider_buying.py \
  --output-dir reports/

# Last 7 days, minimum $100K purchase
python3 skills/insider-buying-detector/scripts/detect_insider_buying.py \
  --days 7 \
  --min-value 100000 \
  --output-dir reports/

# Specific stock
python3 skills/insider-buying-detector/scripts/detect_insider_buying.py \
  --symbol AAPL \
  --days 90 \
  --output-dir reports/

# Cluster buying (multiple insiders same stock)
python3 skills/insider-buying-detector/scripts/detect_insider_buying.py \
  --min-buyers 2 \
  --days 30 \
  --output-dir reports/
```

---

## 5. Workflow

### Step 1: Run the Detector

```bash
# Default: last 30 days, all S&P 500 insider buys
python3 skills/insider-buying-detector/scripts/detect_insider_buying.py \
  --output-dir reports/

# Last 7 days, minimum $100K purchase
python3 skills/insider-buying-detector/scripts/detect_insider_buying.py \
  --days 7 \
  --min-value 100000 \
  --output-dir reports/

# Specific stock
python3 skills/insider-buying-detector/scripts/detect_insider_buying.py \
  --symbol AAPL \
  --days 90 \
  --output-dir reports/

# Cluster buying (multiple insiders same stock)
python3 skills/insider-buying-detector/scripts/detect_insider_buying.py \
  --min-buyers 2 \
  --days 30 \
  --output-dir reports/
```

### Step 2: Rank Signal Quality

**High-conviction signals:**
- CEO or CFO purchase (not options exercise): 3 points
- Purchase value > $500K: 2 points
- Cluster buying (2+ insiders): 2 points
- Purchase within 20% of 52-week low: 2 points
- First purchase by this insider in 12 months: 1 point

**Low-conviction / exclude:**
- Option exercises (not open-market purchases)
- 10b5-1 plan purchases (pre-scheduled, less informative)
- Purchases < $10K (negligible relative to compensation)
- Director purchases (less informative than C-suite)

### Step 3: Combine with Technical Analysis

- Insider buy + stock at support level: strongest setup
- Insider buy + stock breaking 52-week high: momentum + conviction
- Insider buy + oversold RSI: potential mean reversion setup
- Multiple insider buys + increasing institutional ownership: accumulation phase

---

## 6. Resources

**Scripts:**

- `skills/insider-buying-detector/scripts/detect_insider_buying.py`

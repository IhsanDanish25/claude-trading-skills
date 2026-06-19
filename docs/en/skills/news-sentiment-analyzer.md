---
layout: default
title: "News Sentiment Analyzer"
grand_parent: English
parent: Skill Guides
nav_order: 11
lang_peer: /ja/skills/news-sentiment-analyzer/
permalink: /en/skills/news-sentiment-analyzer/
generated: true
---

# News Sentiment Analyzer
{: .no_toc }

Analyze news sentiment for stocks or market themes using NLP scoring. Fetches recent headlines, scores bullish/bearish tone, tracks sentiment momentum over time, and flags sentiment divergence from price action. Use when user asks about news sentiment, whether news is bullish or bearish for a stock, sentiment analysis, or wants to quantify news impact on a security.
{: .fs-6 .fw-300 }

<span class="badge badge-free">No API</span>

[View Source on GitHub](https://github.com/tradermonty/claude-trading-skills/tree/main/skills/news-sentiment-analyzer){: .btn .fs-5 .mb-4 .mb-md-0 }

<details open markdown="block">
  <summary>Table of Contents</summary>
  {: .text-delta }
- TOC
{:toc}
</details>

---

## 1. Overview

# News Sentiment Analyzer

---

## 2. When to Use

- User asks if news flow is bullish or bearish for a stock
- User wants to quantify news sentiment before a trade
- User asks for sentiment analysis of a sector or theme
- User wants to detect sentiment divergence (price up, news bearish or vice versa)

---

## 3. Prerequisites

- FMP API key (`FMP_API_KEY`) for structured news feed
- No key required for WebSearch-based news (fallback mode)

---

## 4. Quick Start

```bash
# Single stock
python3 skills/news-sentiment-analyzer/scripts/analyze_sentiment.py \
  --symbol AAPL \
  --days 7 \
  --output-dir reports/

# Multiple stocks
python3 skills/news-sentiment-analyzer/scripts/analyze_sentiment.py \
  --symbols AAPL NVDA MSFT TSLA \
  --days 14 \
  --output-dir reports/

# Theme/sector (keyword mode)
python3 skills/news-sentiment-analyzer/scripts/analyze_sentiment.py \
  --keyword "artificial intelligence" \
  --days 7 \
  --output-dir reports/
```

---

## 5. Workflow

### Step 1: Run Sentiment Analysis

```bash
# Single stock
python3 skills/news-sentiment-analyzer/scripts/analyze_sentiment.py \
  --symbol AAPL \
  --days 7 \
  --output-dir reports/

# Multiple stocks
python3 skills/news-sentiment-analyzer/scripts/analyze_sentiment.py \
  --symbols AAPL NVDA MSFT TSLA \
  --days 14 \
  --output-dir reports/

# Theme/sector (keyword mode)
python3 skills/news-sentiment-analyzer/scripts/analyze_sentiment.py \
  --keyword "artificial intelligence" \
  --days 7 \
  --output-dir reports/
```

### Step 2: Interpret Sentiment Scores

**Sentiment score range: -1.0 (fully bearish) to +1.0 (fully bullish)**

- +0.5 to +1.0: Strongly bullish — positive catalyst, potential momentum continuation
- +0.2 to +0.5: Mildly bullish — net positive, wait for price confirmation
- -0.2 to +0.2: Neutral — no directional edge from news
- -0.5 to -0.2: Mildly bearish — caution, tighten stops on longs
- -1.0 to -0.5: Strongly bearish — avoid new longs, potential short setup

**Sentiment momentum:**
- 3-day sentiment trending up while price flat: accumulation signal
- 3-day sentiment trending down while price new high: distribution warning

### Step 3: Divergence Signals

- **Bullish divergence**: Price falling but sentiment improving → potential reversal
- **Bearish divergence**: Price rising but sentiment deteriorating → caution
- **Confirmation**: Price and sentiment aligned → trend continuation likely

---

## 6. Resources

**Scripts:**

- `skills/news-sentiment-analyzer/scripts/analyze_sentiment.py`

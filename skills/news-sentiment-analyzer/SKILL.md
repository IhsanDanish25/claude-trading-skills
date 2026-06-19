---
name: news-sentiment-analyzer
description: Analyze news sentiment for stocks or market themes using NLP scoring. Fetches recent headlines, scores bullish/bearish tone, tracks sentiment momentum over time, and flags sentiment divergence from price action. Use when user asks about news sentiment, whether news is bullish or bearish for a stock, sentiment analysis, or wants to quantify news impact on a security.
---

# News Sentiment Analyzer

Score recent news headlines for bullish/bearish sentiment, track sentiment momentum, and flag divergence from price.

## When to Use

- User asks if news flow is bullish or bearish for a stock
- User wants to quantify news sentiment before a trade
- User asks for sentiment analysis of a sector or theme
- User wants to detect sentiment divergence (price up, news bearish or vice versa)

## Prerequisites

- FMP API key (`FMP_API_KEY`) for structured news feed
- No key required for WebSearch-based news (fallback mode)

## Workflow

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

## Output

- `sentiment_YYYY-MM-DD.json` — Scores per symbol with article breakdown
- `sentiment_YYYY-MM-DD.md` — Sentiment dashboard with divergence alerts

## Resources

- `references/sentiment_scoring.md` — Keyword lexicon and scoring methodology

---
name: breakout-scanner
description: Scan for stocks breaking out of consolidation ranges on above-average volume. Detects price breakouts above resistance levels, 52-week highs, and multi-week consolidation boxes with volume confirmation. Use when user asks for breakout stocks, 52-week high breakouts, volume breakouts, stocks breaking resistance, or wants to find momentum initiation setups.
---

# Breakout Scanner

Detect stocks breaking out of consolidation above resistance with volume confirmation — the momentum initiation signal.

## When to Use

- User asks for stocks breaking out today or this week
- User wants 52-week high breakouts with volume
- User asks for stocks breaking above a consolidation range
- User wants momentum initiation setups (not continuation)

## Prerequisites

- FMP API key (`FMP_API_KEY` environment variable or `--api-key`)
- Free tier sufficient for daily scans

## Workflow

### Step 1: Run the Scanner

```bash
# Default: S&P 500 screener for today's breakouts
python3 skills/breakout-scanner/scripts/scan_breakouts.py \
  --output-dir reports/

# Custom universe with tighter volume filter
python3 skills/breakout-scanner/scripts/scan_breakouts.py \
  --symbols AAPL NVDA MSFT AMD META GOOGL AMZN TSLA NFLX CRM \
  --min-volume-ratio 1.5 \
  --lookback-days 60 \
  --output-dir reports/

# 52-week high breakouts only
python3 skills/breakout-scanner/scripts/scan_breakouts.py \
  --mode 52wk-high \
  --min-volume-ratio 2.0 \
  --output-dir reports/
```

### Step 2: Validate Breakouts

**Breakout quality criteria:**
- **Volume ratio > 1.5x**: At least 50% above 20-day average volume (minimum)
- **Volume ratio > 2.0x**: Strong institutional participation (preferred)
- **Consolidation length ≥ 3 weeks**: Longer base = more powerful breakout
- **Tight base**: Range width < 15% during consolidation

**Breakout modes:**
- `52wk-high`: Price breaks above 52-week high
- `box-breakout`: Price breaks above multi-week consolidation range high
- `pivot-breakout`: Price breaks above identified pivot (flat base, cup handle)

### Step 3: Entry and Risk

- **Entry**: At breakout price (within 3% of breakout level)
- **Stop**: Below the consolidation range low
- **Target**: Measured move = base depth added to breakout level
- **Do not chase**: If price > 5% above breakout level, wait for next base

### Step 4: False Breakout Filters

- Reject if: broad market in distribution (FTD score < 50)
- Reject if: volume is below 1.5x average (weak commitment)
- Reject if: stock has earnings within 5 trading days
- Reject if: price reverses back into range on same day

## Output

- `breakouts_YYYY-MM-DD.json` — Flagged breakouts with quality scores
- `breakouts_YYYY-MM-DD.md` — Ranked breakout table with setup details

## Resources

- `references/breakout_methodology.md` — Base patterns and volume confirmation rules

---
name: earnings-momentum-tracker
description: Track post-earnings price momentum continuation for stocks that gapped up or down on earnings. Measures 5, 10, and 20-day momentum after the earnings event to identify PEAD (Post-Earnings Announcement Drift) candidates still in play. Use when user asks about earnings momentum, post-earnings drift, stocks still running after earnings, or wants to find earnings continuation setups.
---

# Earnings Momentum Tracker

Measure and rank post-earnings price momentum to find PEAD continuation plays still in their drift window.

## When to Use

- User asks which stocks are still running after earnings
- User wants to find post-earnings momentum continuation setups
- User asks about PEAD (Post-Earnings Announcement Drift) candidates
- User wants to screen for earnings gap-and-go setups in progress

## Prerequisites

- FMP API key (`FMP_API_KEY` environment variable or `--api-key`)
- Free tier (250 calls/day) sufficient for 20-stock scan

## Workflow

### Step 1: Run the Tracker

```bash
# Default: last 30 days, top 20 by momentum
python3 skills/earnings-momentum-tracker/scripts/track_earnings_momentum.py \
  --output-dir reports/

# Custom lookback and minimum earnings gap
python3 skills/earnings-momentum-tracker/scripts/track_earnings_momentum.py \
  --lookback-days 14 \
  --min-gap-pct 5.0 \
  --min-momentum-5d 3.0 \
  --output-dir reports/
```

### Step 2: Interpret Momentum Scores

- **5-day momentum**: Immediate post-earnings drift (days 1–5)
- **10-day momentum**: Mid-term continuation (days 1–10)
- **20-day momentum**: Full PEAD window (institutional accumulation phase)
- **Momentum grade**: A (>15% 20d), B (8–15%), C (3–8%), D (<3%)

### Step 3: Entry Criteria

- Stock still within first 20 trading days since earnings
- 5-day momentum positive and accelerating
- Volume above 20-day average on up days
- No distribution days (3+ heavy-volume down days)

### Step 4: Risk Management

- Stop-loss: Below the earnings gap low
- Target: 1.5–2x the initial earnings gap magnitude
- Exit signal: 3 consecutive red closes on above-average volume

## Output

- `earnings_momentum_YYYY-MM-DD.json` — Ranked candidates
- `earnings_momentum_YYYY-MM-DD.md` — Human-readable report with trade setups

## Resources

- `references/pead_framework.md` — PEAD theory and empirical drift windows

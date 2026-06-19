---
layout: default
title: "Earnings Momentum Tracker"
grand_parent: English
parent: Skill Guides
nav_order: 11
lang_peer: /ja/skills/earnings-momentum-tracker/
permalink: /en/skills/earnings-momentum-tracker/
generated: true
---

# Earnings Momentum Tracker
{: .no_toc }

Track post-earnings price momentum continuation for stocks that gapped up or down on earnings. Measures 5, 10, and 20-day momentum after the earnings event to identify PEAD (Post-Earnings Announcement Drift) candidates still in play. Use when user asks about earnings momentum, post-earnings drift, stocks still running after earnings, or wants to find earnings continuation setups.
{: .fs-6 .fw-300 }

<span class="badge badge-free">No API</span>

[View Source on GitHub](https://github.com/tradermonty/claude-trading-skills/tree/main/skills/earnings-momentum-tracker){: .btn .fs-5 .mb-4 .mb-md-0 }

<details open markdown="block">
  <summary>Table of Contents</summary>
  {: .text-delta }
- TOC
{:toc}
</details>

---

## 1. Overview

# Earnings Momentum Tracker

---

## 2. When to Use

- User asks which stocks are still running after earnings
- User wants to find post-earnings momentum continuation setups
- User asks about PEAD (Post-Earnings Announcement Drift) candidates
- User wants to screen for earnings gap-and-go setups in progress

---

## 3. Prerequisites

- FMP API key (`FMP_API_KEY` environment variable or `--api-key`)
- Free tier (250 calls/day) sufficient for 20-stock scan

---

## 4. Quick Start

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

---

## 5. Workflow

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

---

## 6. Resources

**Scripts:**

- `skills/earnings-momentum-tracker/scripts/track_earnings_momentum.py`

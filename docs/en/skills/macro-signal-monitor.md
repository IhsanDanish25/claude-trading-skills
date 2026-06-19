---
layout: default
title: "Macro Signal Monitor"
grand_parent: English
parent: Skill Guides
nav_order: 11
lang_peer: /ja/skills/macro-signal-monitor/
permalink: /en/skills/macro-signal-monitor/
generated: true
---

# Macro Signal Monitor
{: .no_toc }

Monitor cross-asset macro signals including yield curve shape, DXY trend, VIX regime, commodity momentum (gold, oil), and credit spreads to determine the current macro environment and its implications for equity risk. Use when user asks about macro environment, yield curve signals, dollar strength impact, VIX regime, risk-on vs risk-off conditions, or wants macro context for equity exposure decisions.
{: .fs-6 .fw-300 }

<span class="badge badge-free">No API</span>

[View Source on GitHub](https://github.com/tradermonty/claude-trading-skills/tree/main/skills/macro-signal-monitor){: .btn .fs-5 .mb-4 .mb-md-0 }

<details open markdown="block">
  <summary>Table of Contents</summary>
  {: .text-delta }
- TOC
{:toc}
</details>

---

## 1. Overview

# Macro Signal Monitor

---

## 2. When to Use

- User asks about the macro environment or risk-on/risk-off conditions
- User wants to understand yield curve signals and their equity impact
- User asks about DXY (dollar) strength and its effect on sectors/earnings
- User wants macro context before increasing or reducing equity exposure

---

## 3. Prerequisites

- No API key required (uses yfinance for free market data)
- Internet connection required for live data

---

## 4. Quick Start

```bash
# Default: all macro signals, 90-day lookback
python3 skills/macro-signal-monitor/scripts/monitor_macro_signals.py \
  --output-dir reports/

# Extended lookback for regime detection
python3 skills/macro-signal-monitor/scripts/monitor_macro_signals.py \
  --lookback-days 180 \
  --output-dir reports/
```

---

## 5. Workflow

### Step 1: Run the Monitor

```bash
# Default: all macro signals, 90-day lookback
python3 skills/macro-signal-monitor/scripts/monitor_macro_signals.py \
  --output-dir reports/

# Extended lookback for regime detection
python3 skills/macro-signal-monitor/scripts/monitor_macro_signals.py \
  --lookback-days 180 \
  --output-dir reports/
```

### Step 2: Interpret the Macro Dashboard

**Yield curve (10Y–2Y spread):**
- > +50bps: Normal / steepening → early cycle, risk-on
- 0 to +50bps: Flat → late cycle, reduce cyclicals
- < 0 (inverted): Recession signal (with 6–18 month lag)
- Rapidly steepening from inversion: bear steepener — equity negative

**VIX regime:**
- < 15: Low fear, risk-on — full equity exposure appropriate
- 15–25: Normal — standard positioning
- 25–35: Elevated fear — reduce position sizes 25–50%
- > 35: Panic — cash defensive, watch for capitulation reversal

**DXY (US Dollar):**
- Rising DXY: headwind for multinational earners, commodities, EM
- Falling DXY: tailwind for tech exports, gold, EM, commodities
- DXY > 200-day MA: defensive posture for international exposure

**Gold:**
- Rising gold + falling yields: real rates declining → liquidity conditions favorable
- Rising gold + rising yields: stagflation fear → defensive positioning
- Gold/SPX ratio rising: risk-off rotation in progress

**Credit spreads (HY–IG spread):**
- < 300bps: Benign credit — no systemic risk signal
- 300–500bps: Caution — credit stress building
- > 500bps: Stress — significant equity risk-off historically

### Step 3: Composite Macro Score

Macro score (0–100) → equity exposure guidance:
- 70–100: Risk-on — max equity exposure (per regime model)
- 50–70: Neutral — moderate exposure
- 30–50: Cautious — reduce exposure, raise cash
- 0–30: Risk-off — minimal equity, defensive only

---

## 6. Resources

**Scripts:**

- `skills/macro-signal-monitor/scripts/monitor_macro_signals.py`

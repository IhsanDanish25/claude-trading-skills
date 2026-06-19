---
layout: default
title: "Options Flow Scanner"
grand_parent: English
parent: Skill Guides
nav_order: 11
lang_peer: /ja/skills/options-flow-scanner/
permalink: /en/skills/options-flow-scanner/
generated: true
---

# Options Flow Scanner
{: .no_toc }

Scan for unusual options activity and large institutional options flow. Identifies calls/puts with abnormal volume vs open interest ratios, sweeps, and block trades. Use when user asks about options flow, unusual options activity, dark pool options, smart money options bets, or wants to track what institutions are buying in the options market.
{: .fs-6 .fw-300 }

<span class="badge badge-free">No API</span>

[View Source on GitHub](https://github.com/tradermonty/claude-trading-skills/tree/main/skills/options-flow-scanner){: .btn .fs-5 .mb-4 .mb-md-0 }

<details open markdown="block">
  <summary>Table of Contents</summary>
  {: .text-delta }
- TOC
{:toc}
</details>

---

## 1. Overview

# Options Flow Scanner

---

## 2. When to Use

- User asks for unusual options activity or options flow
- User wants to know what smart money is betting on via options
- User requests call/put sweep detection or block trade analysis
- User wants options-based confirmation for a directional trade thesis

---

## 3. Prerequisites

- FMP API key (`FMP_API_KEY` environment variable or `--api-key`)
- Free tier sufficient for single-symbol scans; paid tier for broad screening

---

## 4. Quick Start

```bash
# Scan specific symbols
python3 skills/options-flow-scanner/scripts/scan_options_flow.py \
  --symbols AAPL NVDA MSFT TSLA META \
  --output-dir reports/

# Scan with custom filters
python3 skills/options-flow-scanner/scripts/scan_options_flow.py \
  --symbols AAPL NVDA \
  --min-volume 500 \
  --min-oi-ratio 3.0 \
  --output-dir reports/
```

---

## 5. Workflow

### Step 1: Run the Scanner

```bash
# Scan specific symbols
python3 skills/options-flow-scanner/scripts/scan_options_flow.py \
  --symbols AAPL NVDA MSFT TSLA META \
  --output-dir reports/

# Scan with custom filters
python3 skills/options-flow-scanner/scripts/scan_options_flow.py \
  --symbols AAPL NVDA \
  --min-volume 500 \
  --min-oi-ratio 3.0 \
  --output-dir reports/
```

### Step 2: Interpret Results

For each flagged contract:
- **Volume/OI Ratio > 3x**: Unusual interest relative to existing open interest
- **Sweep flag**: Multi-exchange split orders indicate urgency (institutional)
- **Put/Call ratio**: < 0.7 bullish skew, > 1.3 bearish skew
- **Days to expiry**: < 7 DTE = speculative; 30–90 DTE = directional conviction

### Step 3: Cross-Reference

- Load `references/options_flow_interpretation.md` for sweep vs block trade context
- Confirm underlying chart trend using Technical Analyst skill
- Check earnings dates — flow before earnings is often hedging, not directional

---

## 6. Resources

**Scripts:**

- `skills/options-flow-scanner/scripts/scan_options_flow.py`

---
name: sector-rotation-detector
description: Detect sector rotation by measuring relative momentum across all 11 SPDR sector ETFs (XLK, XLV, XLF, XLE, XLI, XLY, XLP, XLU, XLRE, XLB, XLC). Ranks sectors by 1-month, 3-month, and 6-month momentum and identifies which sectors money is rotating into and out of. Use when user asks about sector rotation, which sectors are leading or lagging, where money is flowing, or wants a sector momentum ranking.
---

# Sector Rotation Detector

Rank all 11 SPDR sector ETFs by multi-timeframe momentum to identify rotation patterns and leading/lagging sectors.

## When to Use

- User asks which sectors are leading or lagging the market
- User wants to know where institutional money is rotating
- User asks for sector momentum ranking or relative strength
- User wants macro context for individual stock selection

## Prerequisites

- No API key required (uses yfinance for free ETF data)
- Internet connection required for live data

## Workflow

### Step 1: Run the Detector

```bash
# Default: all 11 SPDR sectors, 1/3/6-month momentum
python3 skills/sector-rotation-detector/scripts/detect_sector_rotation.py \
  --output-dir reports/

# Add SPY as benchmark comparison
python3 skills/sector-rotation-detector/scripts/detect_sector_rotation.py \
  --benchmark SPY \
  --output-dir reports/
```

### Step 2: Interpret Rotation

**Leading sectors** (top 3 by 1-month momentum):
- Cyclicals leading (XLY, XLI, XLF) → risk-on, early expansion
- Defensives leading (XLP, XLU, XLV) → risk-off, late cycle or contraction
- Tech (XLK) leading → growth environment, momentum regime

**Rotation signals:**
- Sector moves from bottom half to top half of rankings → accumulation phase
- Sector losing 1-month rank despite strong 6-month → distribution beginning
- Divergence between 1-month and 6-month → inflection point

### Step 3: Apply to Stock Selection

- Focus individual stock screening on top 2–3 ranked sectors
- Avoid longs in bottom 3 sectors (sector headwind)
- Sector ETF relative strength confirms individual stock thesis

## Output

- `sector_rotation_YYYY-MM-DD.json` — Rankings with momentum scores
- `sector_rotation_YYYY-MM-DD.md` — Visual rotation table + interpretation

## Resources

- `references/sector_rotation_framework.md` — Economic cycle to sector mapping

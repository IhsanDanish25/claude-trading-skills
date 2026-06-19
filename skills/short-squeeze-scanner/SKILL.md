---
name: short-squeeze-scanner
description: Identify potential short squeeze candidates by combining high short interest, low float, rising price momentum, and increasing volume. Scores stocks on their squeeze potential using the classic short squeeze formula (days-to-cover, short float %, price momentum). Use when user asks about short squeeze candidates, high short interest stocks, potential squeezes, or wants to find stocks where shorts could be forced to cover.
---

# Short Squeeze Scanner

Score stocks on short squeeze potential using days-to-cover, short float percentage, price momentum, and volume surge indicators.

## When to Use

- User asks for short squeeze candidates or high short interest stocks
- User wants to find stocks where shorts may be forced to cover
- User asks about squeeze potential for a specific stock
- User wants to trade against heavily shorted names showing momentum

## Prerequisites

- FMP API key (`FMP_API_KEY` environment variable or `--api-key`)
- Free tier sufficient for watchlist screening; paid tier for full universe

## Workflow

### Step 1: Run the Scanner

```bash
# Default: screen for squeeze setups
python3 skills/short-squeeze-scanner/scripts/scan_short_squeeze.py \
  --output-dir reports/

# Custom thresholds
python3 skills/short-squeeze-scanner/scripts/scan_short_squeeze.py \
  --min-short-float 15.0 \
  --max-days-to-cover 5.0 \
  --min-price-momentum 5.0 \
  --output-dir reports/

# Specific symbols
python3 skills/short-squeeze-scanner/scripts/scan_short_squeeze.py \
  --symbols GME AMC BBBY SPCE MSTR COIN \
  --output-dir reports/
```

### Step 2: Interpret Squeeze Score

**Squeeze score components (0–100):**
- **Short float % (30%)**: > 20% = high short interest
- **Days-to-cover (25%)**: < 3 days = shorts can cover quickly (less squeeze); > 7 days = trapped
- **Price momentum 5d (20%)**: Rising price forces short covering
- **Volume surge (15%)**: Volume > 2x average = covering pressure
- **Float size (10%)**: < 10M float = easier to squeeze

**Squeeze grades:**
- A (80–100): Classic squeeze setup — high conviction
- B (60–80): Moderate squeeze potential
- C (40–60): Watch only
- D (< 40): No significant squeeze risk

### Step 3: Catalysts That Trigger Squeezes

- Positive earnings surprise on heavily shorted stock
- Analyst upgrade or price target raise
- Regulatory approval or contract win
- Social media / retail investor coordination (monitor Reddit sentiment)
- Short seller report rebuttal by management

### Step 4: Risk Management

- Short squeezes are high-risk, high-reward — limit position size to 1–2% of portfolio
- Use options (call spreads) to limit downside on squeeze plays
- Set hard stops — squeezes can reverse violently
- Do not hold through earnings on squeeze plays

## Output

- `short_squeeze_YYYY-MM-DD.json` — Ranked candidates with squeeze scores
- `short_squeeze_YYYY-MM-DD.md` — Squeeze dashboard with days-to-cover table

## Resources

- `references/short_squeeze_mechanics.md` — Short squeeze dynamics and historical examples

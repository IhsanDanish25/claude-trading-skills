# Strategy Validation Status (Developer / Maintainer Operations)

This is the current, evidence-backed validation status of every strategy the
live trading system (`core/`, `worker.py`, `auto_trader.py`) can run under
`STRATEGY_MODE`. It exists so a strategy's presence in the codebase is never
mistaken for proof it works — see `validation_gates.py` for why gates exist
and what they check.

Related references:

- `validation_gates.py` — the four-gate pass/fail logic (`run_gates`).
- `backtest_harness/` — the point-in-time harness (`engine.py`, `metrics.py`,
  `satellite_signals.py`) used to produce these numbers.
- `truth_check.json` — raw PEAD daily-return series backing the PEAD row below.
- `backtests/five_strategies_2026-07-05/summary.json` — raw output backing
  the Breakout / Mean Reversion / Insider / Squeeze / Earnings Momentum rows.
- `core/config.py` (`STRATEGY_MODES`) — the env var these strategies are
  selected through in production.

---

## 1. Validation gates (recap)

A backtest result is only meaningful if it clears all of:

| Gate | Threshold | What it catches |
|---|---|---|
| `trade_count` | ≥ 50 closed trades | Small-sample noise dressed up as edge |
| `not_overfit` | in-sample/out-of-sample return ratio ≤ 1.5, both windows positive | Curve-fitting to the training window |
| `significant` | two-sided p-value < 0.05 on mean daily return | Returns indistinguishable from zero |
| `beats_spy` | beats SPY buy-and-hold on total return AND (Sharpe or drawdown) | Edge that isn't just beta |

`trustworthy` requires the first three. `beats_field` requires all four. A
strategy failing `trustworthy` should not be trusted in size, regardless of
what its headline Sharpe looks like.

---

## 2. Status by strategy

| Strategy | Status | Window | Trades | Sharpe | Win rate | p-value | Gates passed | Verdict |
|---|---|---|---|---|---|---|---|---|
| **PEAD** | validated (failing) | 2024-05-14 → 2026-06-26 | 871 | 0.30 | — | 0.659 | trade_count only | Not trustworthy |
| **Mean Reversion** | validated (failing) | 2024-05-14 → 2026-06-26 | 13 | 1.01 | 53.9% | 0.142 | none | Not trustworthy |
| **Breakout** | validated (failing) | 2024-05-14 → 2026-06-26 | 27 | -0.38 | 37.0% | 0.585 | none | Not trustworthy; retired from recommended `STRATEGY_MODE` |
| **Insider Buying** | blocked, no network | — | — | — | — | — | — | Untested — needs FMP `/stable/insider-trading` history |
| **Short Squeeze** | blocked, no network | — | — | — | — | — | — | Untested — needs FMP `/stable/short-interest` history |
| **Earnings Momentum** | blocked, no network | — | — | — | — | — | — | Untested — needs yfinance historical earnings dates |

**None of the currently backtestable strategies (PEAD, Mean Reversion,
Breakout) clear `trustworthy`.** Insider, Squeeze, and Earnings Momentum have
never been backtested at all — their live code paths are unvalidated, not
just unproven.

### 2.1 PEAD

- Source: `truth_check.json` (871 closed trades, 2024-05-14 → 2026-06-26,
  10bps slippage) fed through `validation_gates.run_gates`.
- Result: total return +4.4% vs SPY +39.4%; Sharpe 0.30 vs SPY 1.06; max
  drawdown -8.9% vs SPY -19.0%.
- Fails `not_overfit` (IS/OOS ratio -2.44 — in-sample was positive at +7.9%,
  out-of-sample went negative at -3.2%, the classic overfit signature) and
  `significant` (p=0.659, far above the 0.05 bar). Only clears `trade_count`.
- PEAD is nonetheless the current default (`STRATEGY_MODE=pead`) because it
  is the only strategy that has run live long enough to have a real
  execution track record — its backtest failing these gates is a known,
  open risk, not an oversight. Do not increase PEAD sizing on the strength
  of this backtest.

### 2.2 Mean Reversion

- Source: `backtest_harness/satellite_signals.py` (offline replica of
  `core/meanrev_screener.py` math) walked over the committed OHLCV cache,
  same window as PEAD.
- Result: Sharpe 1.01 looks the best of the three backtestable strategies,
  but only 13 trades fired in a 531-trading-day window — an order of
  magnitude below the 50-trade floor. `p=0.142` also misses significance.
  A good-looking Sharpe on 13 trades is not evidence; it's a coin flip that
  landed heads.

### 2.3 Breakout

- Source: same harness/window as Mean Reversion.
- Result: Sharpe -0.38, 37.0% win rate, p=0.585 — actively negative, not
  just unproven. This is why `core/config.py`'s recommended `STRATEGY_MODE`
  example excludes `breakout` (see commit "Retire breakout/earnmom from
  recommended STRATEGY_MODE"). It remains supported as an opt-in value only
  for anyone who wants to re-validate it later; it should not be enabled by
  default.

### 2.4 Insider Buying / Short Squeeze / Earnings Momentum

- All three are reported `blocked_no_network` in
  `backtests/five_strategies_2026-07-05/summary.json`: they need live or
  historical fetches (FMP insider-trading, FMP short-interest, yfinance
  earnings dates respectively) to hosts blocked by that backtest session's
  egress policy. No performance numbers exist for them at all.
- Treat their live code paths as **unvalidated**, not merely "not yet
  re-validated" like Breakout. Anyone enabling them via `STRATEGY_MODE`
  before a real backtest exists is running unvalidated logic with real
  capital.

---

## 3. Known caveats on the existing numbers

- The satellite backtest universe (Breakout, Mean Reversion) is the 30
  non-ETF symbols present in the committed bar cache
  (`backtest_harness/cache/*.json`), **not** the full 103-symbol
  `SP80_UNIVERSE` used in live production scans, because the session that
  produced them had no network access to refresh the cache. Results may not
  generalize to the full universe.
- PEAD's 871-trade sample clears the trade-count bar easily, but still
  fails significance and shows an overfit IS/OOS signature — more trades
  do not make a result trustworthy if the gates fail for other reasons.

## 4. Re-running the validation

```bash
# PEAD (uses backtest_harness/engine.py against truth_check-style output)
python3 -c "
import json
from validation_gates import run_gates
d = json.load(open('truth_check.json'))
print(run_gates(
    strat_daily_returns=d['strat_daily_returns'],
    spy_daily_returns=d['spy_daily_returns'],
    n_trades=d['n_closed_trades'],
    is_return=d['in_sample_total_return'],
    oos_return=d['out_of_sample_total_return'],
).summary())
"

# Breakout / Mean Reversion / Insider / Squeeze / Earnings Momentum
python3 backtest_5_strategies.py --output-dir backtests/
```

Re-run whenever the bar cache is refreshed, the strategy's entry/exit rules
change, or before promoting a strategy into the recommended `STRATEGY_MODE`
set in `core/config.py`. Update the table above with the new date, gate
results, and verdict — do not silently overwrite the prior verdict without
noting what changed.

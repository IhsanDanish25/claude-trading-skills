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
- `backtest_harness/standard_universe.json` + the committed
  `backtest_harness/cache/*.json.gz` — the standard bar cache backing the
  Breakout / Mean Reversion / Earnings Momentum rows. Reproduce with
  `python3 backtest_5_strategies.py --universe live|full` (§4).
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
Satellite rows below are the **live (103-symbol) universe** on the standard
cache (yfinance, 6y, `2020-10-13 → 2026-07-02`). See §2.6 for the full
520-symbol universe.

| Strategy | Status | Window | Trades | Sharpe | Win rate | p-value | Gates passed | Verdict |
|---|---|---|---|---|---|---|---|---|
| **PEAD** | validated (failing) | 2024-05-14 → 2026-06-26 | 871 | 0.30 | — | 0.659 | trade_count only | Not trustworthy |
| **Mean Reversion** | validated (failing) | 2020-10-13 → 2026-07-02 | 347 | 0.87 | 51.6% | 0.039 | trade_count, significant | Significant but not trustworthy (overfit gate not evaluated) |
| **Breakout** | validated (failing) | 2020-10-13 → 2026-07-02 | 376 | 0.58 | 48.4% | 0.164 | trade_count | Not significant |
| **Earnings Momentum** | validated (failing) | 2020-10-13 → 2026-07-02 | 217 | 0.96 | 53.9% | 0.022 | trade_count, significant | Significant but not trustworthy (overfit gate not evaluated) |
| **Insider Buying** | blocked, paid tier | — | — | — | — | — | — | Untested — FMP `/stable/insider-trading` is a **paid-tier** endpoint (402/403 on the free plan), not a network issue |
| **Short Squeeze** | blocked, paid tier | — | — | — | — | — | — | Untested — FMP `/stable/short-interest` not on the free plan (404/403) |

**No backtestable strategy clears `trustworthy`.** With the standard cache all
three satellite strategies now clear the 50-trade floor, and Mean Reversion +
Earnings Momentum clear significance (p<0.05) on the live universe — a real
change from the 30-symbol result, where thin samples (12–27 trades) made every
number a coin flip. They still can't reach `trustworthy` because the satellite
runner calls `run_gates` with no in-sample/out-of-sample split, so `not_overfit`
never evaluates (shows as failing) — a runner limitation, not a strategy verdict
(see §2.5). Insider and Squeeze remain untested (paid FMP tier, §2.5).

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
  `core/meanrev_screener.py` math) walked over the standard cache.
- Result (live universe): **347 trades, Sharpe 0.87, 51.6% win, p=0.039** —
  clears trade_count and significance. The prior 30-symbol run gave only 13
  trades / p=0.142; the difference is sample size, not a rule change. It still
  does not `beat_spy` and can't reach `trustworthy` (overfit gate not
  evaluated, §2.5). A tradeable-looking edge that needs the overfit split
  before sizing.

### 2.3 Breakout

- Source: same harness / standard cache as Mean Reversion.
- Result (live universe): **376 trades, Sharpe 0.58, 48.4% win, p=0.164** —
  clears trade_count but misses significance on the live universe. It *does*
  reach significance on the full 520-symbol universe (p=0.039, §2.6), so it is
  no longer the flatly-negative result the 30-symbol run suggested (−0.38
  Sharpe) — that was a small-sample artifact. Still not trustworthy; treat as
  opt-in until it clears significance on the universe you actually trade.

### 2.4 Earnings Momentum (now backtestable)

- Previously reported `blocked_no_network`. That diagnosis was wrong: yfinance
  earnings dates are reachable, and the strategy only lacked a point-in-time
  signal generator. `backtest_harness/satellite_signals.py`
  (`get_historical_earnmom_signals`) now replicates
  `core.earnings_momentum_screener` day-by-day — most recent beat
  (surprise ≥ `EARNMOM_MIN_SURPRISE_PCT`) 8–`EARNMOM_MAX_DAYS_AGO` days ago that
  has drifted up ≥ `EARNMOM_MIN_DRIFT_PCT` — using earnings dates from
  `backtest_harness/earnings_data.py` (yfinance) and drift/price/volume from the
  OHLCV cache. `backtest_5_strategies.py` runs it alongside Breakout/MeanRev.
- Result (live universe): **217 trades, Sharpe 0.96, 53.9% win, p=0.022** —
  clears trade_count and significance, the best-looking of the three satellite
  strategies. Still fails `beats_spy` and can't reach `trustworthy` (overfit
  gate not evaluated). On the 30-symbol cache it was 12 trades / p=0.523 — the
  jump to significance is the standard cache doing its job.

### 2.5 Gate wiring, Insider Buying & Short Squeeze

- **`not_overfit` is never evaluated by the satellite runner.**
  `backtest_5_strategies.py` calls `run_gates(..., is_return=None,
  oos_return=None)`, so the overfit gate has no in-sample/out-of-sample split to
  score and reports `inf` (failing). This means **no satellite strategy can
  reach `trustworthy` today regardless of its edge** — a runner limitation to
  fix (split the window and pass the two returns) before any satellite strategy
  can be promoted. PEAD (§2.1) does pass real IS/OOS numbers and still fails, so
  this is not hiding a winner.
- **Insider Buying / Short Squeeze** need FMP fundamental endpoints that are
  **paid-tier**, not network-blocked: `/stable/insider-trading` returns 402 and
  `/stable/short-interest` returns 404/403 on the free plan (confirmed against
  both the local and production FMP keys, with network available). Even paid FMP
  short-interest is only bi-monthly FINRA snapshots.
- Treat their live code paths as **unvalidated**. Anyone enabling them via
  `STRATEGY_MODE` before a real backtest exists is running unvalidated logic
  with real capital.

### 2.6 Full 520-symbol universe (`--universe full`)

Same standard cache, all cached non-ETF symbols (not just the 103 production
trades) — more statistical power, but tests names production never scans:

| Strategy | Trades | Sharpe | Win rate | p-value | Gates |
|---|---|---|---|---|---|
| Breakout | 568 | 0.86 | 53.7% | 0.039 | trade_count, significant |
| Mean Reversion | 619 | 0.69 | 51.2% | 0.099 | trade_count |
| Earnings Momentum | 341 | 0.85 | 49.3% | 0.043 | trade_count, significant |

Significance flips between universes (Breakout significant on full but not live;
MeanRev the reverse) — a reminder that none of these is a robust, universe-stable
edge yet. Earnings Momentum is the only one significant on **both**.

- Both need FMP fundamental endpoints that are **paid-tier**, not
  network-blocked: `/stable/insider-trading` returns 402 Payment Required and
  `/stable/short-interest` returns 404/403 on the free plan (confirmed against
  both the local and production FMP keys, with network available). Even paid
  FMP short-interest is only bi-monthly FINRA snapshots.
- Treat their live code paths as **unvalidated**. Anyone enabling them via
  `STRATEGY_MODE` before a real backtest exists is running unvalidated logic
  with real capital.

---

## 3. Known caveats on the existing numbers

- The satellite numbers now come from the **standard cache**: yfinance daily
  bars, ~6y (`2020-10-13 → 2026-07-02`), gzipped and committed as
  `backtest_harness/cache/{SYM}.json.gz` (built by
  `backtest_harness/build_standard_cache.py` from
  `backtest_harness/standard_universe.json`). `--universe live` = the 103-symbol
  `SP80_UNIVERSE` production trades; `--universe full` = the ~520-symbol S&P 500
  power set. This replaces the earlier 30-symbol Alpaca cache (2.4y), whose thin
  samples (12–27 trades) failed trade_count outright.
- **Data source differs from live execution.** yfinance bars are
  split/dividend-adjusted; production trades the Alpaca IEX feed (raw). Fine for
  measuring an edge, but entry/stop prices will drift slightly vs live.
- Because `load_cached` prefers `{SYM}.json.gz`, any remaining legacy
  `{SYM}.json` are shadowed — the standard cache is what actually runs.
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

# Breakout / Mean Reversion / Earnings Momentum on the standard cache
python3 backtest_5_strategies.py --universe live   # 103-symbol production set
python3 backtest_5_strategies.py --universe full   # full ~520-symbol S&P 500 set

# Rebuild / extend the standard bar cache (yfinance, gzipped, committed):
python3 backtest_harness/build_standard_cache.py --years 5
```

Re-run whenever the bar cache is refreshed, the strategy's entry/exit rules
change, or before promoting a strategy into the recommended `STRATEGY_MODE`
set in `core/config.py`. Update the table above with the new date, gate
results, and verdict — do not silently overwrite the prior verdict without
noting what changed.

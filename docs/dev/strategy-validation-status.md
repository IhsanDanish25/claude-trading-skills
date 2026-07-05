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
| **Mean Reversion** | **trustworthy** | 2020-10-13 → 2026-07-02 | 347 | 0.87 | 51.6% | 0.039 | trade_count, not_overfit, significant | Trustworthy — survives all honesty gates; does **not** beat SPY buy-and-hold |
| **Breakout** | validated (failing) | 2020-10-13 → 2026-07-02 | 376 | 0.58 | 48.4% | 0.164 | trade_count, not_overfit | Not significant on the live universe (is on full, §2.6) |
| **Earnings Momentum** | **trustworthy** | 2020-10-13 → 2026-07-02 | 217 | 0.96 | 53.9% | 0.022 | trade_count, not_overfit, significant | Trustworthy on **both** universes; does not beat SPY buy-and-hold |
| **Insider Buying** | blocked, sandbox egress | — | — | — | — | — | — | Untested — Railway sandbox blocks egress to `financialmodelingprep.com`; `/stable/insider-trading` is unreachable (connection refused/timeout), not a 402/403 from FMP |
| **Short Squeeze** | blocked, sandbox egress | — | — | — | — | — | — | Untested — same egress block as Insider Buying; also pulls from `query1.finance.yahoo.com` (short interest) and `financialmodelingprep.com` (floats/SiS) |

**Two strategies now clear `trustworthy`** on the live universe — Mean Reversion
and Earnings Momentum both pass trade_count + not_overfit + significant (the
overfit gate now runs; see §2.5). This is the first time any satellite strategy
has survived the honesty gates — the standard cache gave the sample size and the
IS/OOS split gave the overfit check. **None clears `beats_spy`:** over this 6y
window SPY buy-and-hold returned +130%, and these are low-return / low-drawdown
strategies (maxDD −4% to −10% vs SPY −24%) — real, non-overfit edges, but they
trailed a raging bull on raw return. `trustworthy` ≠ "beats the market"; it means
"not fooling yourself." Do not size on the strength of a backtest that loses to
buy-and-hold. Insider and Squeeze remain untested (paid FMP tier, §2.5).

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
- Result (live universe): **347 trades, Sharpe 0.87, 51.6% win, p=0.039,
  IS/OOS overfit ratio 0.08 → TRUSTWORTHY**. Passes trade_count, not_overfit and
  significant. The prior 30-symbol run gave only 13 trades / p=0.142; the
  difference is sample size, not a rule change. It does **not** `beat_spy`
  (+20.6% vs SPY +130.1%), so it's a risk-reducer (maxDD −4.2% vs −24.5%), not a
  return-beater — trustworthy, but don't size it as if it beats buy-and-hold.

### 2.3 Breakout

- Source: same harness / standard cache as Mean Reversion.
- Result (live universe): **376 trades, Sharpe 0.58, 48.4% win, p=0.164** —
  clears trade_count and not_overfit but misses significance on the live
  universe, so not trustworthy there. It *is* trustworthy on the full 520-symbol
  universe (p=0.039, overfit 1.24, §2.6), so it is no longer the flatly-negative
  result the 30-symbol run suggested (−0.38 Sharpe) — that was a small-sample
  artifact. Universe-unstable: treat as opt-in until it clears significance on
  the universe you actually trade.

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
- Result (live universe): **217 trades, Sharpe 0.96, 53.9% win, p=0.022, IS/OOS
  overfit ratio 0.04 → TRUSTWORTHY** — and trustworthy on the full universe too
  (§2.6), the only strategy that survives the gates on **both**. On the 30-symbol
  cache it was 12 trades / p=0.523; the jump is the standard cache doing its job.
  Does not `beat_spy` (+23.4% vs +130.1%) — a trustworthy, low-drawdown edge
  (maxDD −10.2% vs −24.5%), not a buy-and-hold beater.

### 2.5 Gate wiring, Insider Buying & Short Squeeze

- **`not_overfit` now evaluates.** `backtest_5_strategies.py` previously called
  `run_gates(..., is_return=None, oos_return=None)`, so the overfit gate had no
  in-sample/out-of-sample split and reported `inf` (auto-fail) — which is why no
  satellite strategy could reach `trustworthy` no matter its edge. Fixed: the
  runner now splits each strategy's equity curve chronologically 50/50 and passes
  the earlier half as `is_return`, the later half as `oos_return`. These
  strategies have **fixed parameters** (nothing is fit to the data), so the split
  is a temporal-robustness check — did the first-half edge survive into the
  unseen second half — not a train/test on tuned parameters.
- **Insider Buying / Short Squeeze** are blocked by Railway sandbox network
  egress: `financialmodelingprep.com` and `query1.finance.yahoo.com` are
  unreachable from the sandbox (connection refused / timeout / DNS failure).
  This is a platform ingress restriction, not a 402/403 billing error from FMP.
  Even if the sandbox were opened, paid FMP short-interest is only bi-monthly
  FINRA snapshots — stale data for live trading.
- Treat their live code paths as **unvalidated**. Anyone enabling them via
  `STRATEGY_MODE` before a real backtest exists is running unvalidated logic
  with real capital.

### 2.6 Full 520-symbol universe (`--universe full`)

Same standard cache, all cached non-ETF symbols (not just the 103 production
trades) — more statistical power, but tests names production never scans:

| Strategy | Trades | Sharpe | Win rate | p-value | Overfit | Gates | Trustworthy |
|---|---|---|---|---|---|---|---|
| Breakout | 568 | 0.86 | 53.7% | 0.039 | 1.24 | trade_count, not_overfit, significant | **Yes** |
| Mean Reversion | 619 | 0.69 | 51.2% | 0.099 | 0.19 | trade_count, not_overfit | No (p≥0.05) |
| Earnings Momentum | 341 | 0.85 | 49.3% | 0.043 | 0.71 | trade_count, not_overfit, significant | **Yes** |

Trustworthy flips between universes (Breakout on full, MeanRev on live) — a
reminder that none is a robust, universe-stable edge yet. **Earnings Momentum is
trustworthy on both**, the strongest of the three. None `beats_spy` on either
universe (SPY +130%). Treat "trustworthy" as "the backtest isn't lying to you,"
not "this makes money vs holding SPY."

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

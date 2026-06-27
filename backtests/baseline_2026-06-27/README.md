# Baseline Backtest — 2026-06-27

Read-only measurement of the live composite strategy as wired today.
Reuses the real `core.screener.screen()`, `core.composite.build_context()`,
and `core.composite.compute_composite()` against a static point-in-time
universe (watchlist + ≥2y-history names, plus SPY/QQQ/IWM + 11 sector
ETFs for regime detection). FMP disabled to avoid look-ahead.

## Universe (30 names)

AAPL, MSFT, NVDA, AMD, META, GOOGL, AMZN, TSLA, NFLX, CRM, ADBE, PANW,
CRWD, SNOW, DDOG, MELI, SQ, SHOP, NET, ZS, CELH, ENPH, FSLR, ON, AEHR,
SMCI, AXON, COCO, DUOL, PINS.

Static across the full window — the live bot's dynamic Alpaca
actives/movers universe is excluded as non-reproducible point-in-time.

## Window

2024-05-14 → 2026-06-26 (531 trading days). One regime only — AI rally.
Multi-window validation is needed to draw broader conclusions.

## Scenarios

| Scenario | Slippage | Stop | Total Return | CAGR | Sharpe | Sortino | Max DD | Trades | Win% | Expectancy | Stop/Target |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **A** (control) | 0 bps | flat 2% (STOP_LOSS_PCT) | **+11.13%** | +5.11% | 0.87 | 1.29 | -5.32% | 1194 | 28.06% | +0.20% | 889 / 305 |
| **B** | 10 bps | ATR(14) × 1.5 | +4.41% | +2.06% | 0.30 | 0.42 | -8.92% | 871 | 40.87% | +0.11% | 541 / 330 |
| **C** | 20 bps | ATR(14) × 1.5 | -2.79% | -1.33% | -0.13 | -0.18 | -10.37% | 870 | 40.23% | -0.05% | 544 / 326 |
| SPY B&H (ref) | — | — | +39.38% | +16.99% | 1.06 | 1.55 | -18.98% | — | — | — | — |

USD totals: A $10,646.90 · B $3,701.53 · C -$3,304.49 · SPY $39,539.70.

## Key findings

1. **The 2% flat stop is doing real work.** ATR stops are wider on this
   universe (avg_loss goes -1.88% → -3.62%), so the VCP entry signals —
   tuned for tight stops — underperform. Trade count also drops from
   1194 → 871 because wider stops disqualify marginal candidates.

2. **Slippage is brutal.** 10 bps wipes ~60% of return. 20 bps flips it
   negative. Real-world execution (5-15 bps round-trip on these names)
   is the difference between profitable and losing.

3. **Strategy underperforms SPY by ~28 percentage points over 2 years**
   even in the best scenario. Risk-adjusted (Sharpe) is closer (0.87 vs
   1.06) but absolute return is decisively worse. The drawdown
   advantage (-5.32% vs -18.98%) is real but doesn't compensate.

4. **Win-rate jump in B/C is misleading.** Wider stops → more exits via
   4% trailing ratchet (tagged separately). Avg-loss magnitude is the
   real story, not the win-rate.

## Caveats

- **Single regime window.** 2024-25 AI rally; bear-market behavior
  unknown. Multi-window validation (2022, 2018) is the highest-value
  next iteration.
- **Over-trading.** 2.2 trades/day in backtest vs ~1/day in live bot.
  The 9:35-09:44 entry-timing window and 1-trade-per-ticker-per-week
  filters are NOT modeled. Adding them will likely drop trade count
  and may improve or worsen returns.
- **Survivorship bias.** Universe is a curated 30-name watchlist that
  has historically performed well.
- **FMP disabled.** Earnings-calendar + fundamental sub-scores (15%
  of composite weight) are zeroed out. Adding them with a point-in-time
  FMP cache is the second-highest-value next iteration.

## Artifacts

- `baseline_A_control_slip0_flat2pct.{json,png}` — production config, no slippage
- `baseline_B_slip10_atr.{json,png}` — 10 bps slippage, ATR stops
- `baseline_C_slip20_atr.{json,png}` — 20 bps slippage, ATR stops
- `baseline.json` + `equity_curve.png` — pre-suite baseline run

## Reproduce

```bash
.venv/bin/python -m backtest_harness.run_backtest --suite --years 2 --start-equity 100000
```
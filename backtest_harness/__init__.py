"""Read-only backtest harness for the live composite strategy.

This package never imports for side effects into live trading code and never
places orders. It reuses the REAL strategy functions (core.screener.screen,
core.composite.build_context / compute_composite) by feeding them point-in-time
data through a monkeypatched bar fetcher, so the backtest matches production
logic rather than re-implementing it.
"""

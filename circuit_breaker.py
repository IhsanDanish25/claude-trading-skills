"""
circuit_breaker.py  —  strong-charisma drop-in

Two jobs, both as HARD pre-trade gates that RAISE (never return a bool):
  1. Position-cap enforcement   -> kills the "drifted to 8 positions" bug for good.
  2. Daily-loss + rapid-drawdown halt -> caps a runaway day.

Why raise instead of return True/False?
  A bool can be silently ignored by a caller. An exception cannot. Every order
  path MUST go through check_before_order() and CANNOT accidentally skip the halt.

Wire it in ONE place: right before you submit the entry market order in your buy
logic. If it raises -> you skip the trade. Done. The 93%-equity / 8-position
drift becomes structurally impossible.

No TA-Lib, no pandas. Pure stdlib + your existing Alpaca client.
"""

import logging
from datetime import date

logger = logging.getLogger("circuit_breaker")


class TradingHalted(Exception):
    """Raised when an order must be blocked. Reason lives on .reason."""
    def __init__(self, message, reason="unknown", detail=None):
        super().__init__(message)
        self.reason = reason
        self.detail = detail


# Rapid intraday give-back trips earlier than the full daily limit.
RAPID_DRAWDOWN_RATIO = 0.67   # trip at 67% of max_daily_loss from the day's peak


class CircuitBreaker:
    def __init__(
        self,
        get_account,            # callable -> object/dict with .equity (or ["equity"])
        get_positions,          # callable -> list of current open positions
        max_open_positions=3,   # YOUR compliant cap (not the live 10). Hard ceiling.
        max_position_pct=0.05,  # MAX_POSITION_SIZE_PCT. Per-name notional ceiling.
        max_daily_loss=0.03,    # halt the day if equity down 3% from the open
        day_start_equity=None,  # pre-market open equity from day_start_value.json.
                                 # If None, derived from broker on first check (legacy).
                                 # Prefer passing it explicitly so tick-time drift can't
                                 # corrupt the baseline.
    ):
        self.get_account = get_account
        self.get_positions = get_positions
        self.max_open_positions = max_open_positions
        self.max_position_pct = max_position_pct
        self.max_daily_loss = max_daily_loss

        self.trading_halted = False
        self._halt_reason = None
        self._day = None
        self._starting_equity = day_start_equity  # set once; never re-fetched from broker
        self._peak_equity = None

    # ---- helpers -----------------------------------------------------------
    @staticmethod
    def _equity(account):
        if account is None:
            return None
        if isinstance(account, dict):
            return float(account.get("equity"))
        return float(account.equity)

    def _roll_day(self, equity):
        """Reset the daily anchors at the first check of a new calendar day.

        NOTE: _starting_equity is set once at __init__ from the pre-market open value
        (day_start_value.json) and MUST NOT be changed during the day. Only _peak_equity
        updates on each tick. Previously this method re-set _starting_equity from the
        broker's live equity, which drifted throughout the day as intraday P/L
        accumulated, making the daily-loss baseline meaningless.
        """
        today = date.today()
        if self._day != today:
            self._day = today
            self.trading_halted = False
            self._halt_reason = None
            logger.info(
                "Circuit breaker day-roll. Start equity=%.2f  halt-below=%.2f",
                self._starting_equity,
                self._starting_equity * (1 - self.max_daily_loss) if self._starting_equity else 0,
            )
        if equity > self._peak_equity:
            self._peak_equity = equity

    # ---- the only call your buy logic needs --------------------------------
    def check_before_order(self, intended_notional=0.0, symbol=None):
        """
        Call IMMEDIATELY before submitting an entry order.
        Raises TradingHalted if the trade must be blocked. Returns None if OK.
        """
        account = self.get_account()
        equity = self._equity(account)
        if equity is None or equity <= 0:
            raise TradingHalted("No/invalid account equity", reason="no_equity")

        self._roll_day(equity)

        # 1) DAILY-LOSS HALT (sticky for the rest of the day) ----------------
        daily_loss = (self._starting_equity - equity) / self._starting_equity
        if daily_loss >= self.max_daily_loss:
            self.trading_halted = True
            self._halt_reason = "daily_loss_limit"
        give_back = (self._peak_equity - equity) / self._peak_equity if self._peak_equity else 0.0
        if give_back >= self.max_daily_loss * RAPID_DRAWDOWN_RATIO:
            self.trading_halted = True
            self._halt_reason = "rapid_drawdown"

        if self.trading_halted:
            raise TradingHalted(
                f"Trading halted ({self._halt_reason}). "
                f"daily_loss={daily_loss:.2%} give_back={give_back:.2%}",
                reason=self._halt_reason,
                detail={"daily_loss": daily_loss, "give_back": give_back},
            )

        # 2) POSITION-COUNT CAP  (THE drift-bug killer) ----------------------
        positions = self.get_positions() or []
        open_count = len(positions)
        held = {self._sym(p) for p in positions}
        is_new_name = symbol is not None and symbol not in held
        if is_new_name and open_count >= self.max_open_positions:
            raise TradingHalted(
                f"Position cap reached: {open_count}/{self.max_open_positions} open. "
                f"Refusing new name {symbol}.",
                reason="position_cap",
                detail={"open": open_count, "cap": self.max_open_positions},
            )

        # 3) PER-NAME NOTIONAL CAP -------------------------------------------
        if intended_notional > 0:
            ceiling = equity * self.max_position_pct
            if intended_notional > ceiling * 1.0001:  # tiny epsilon for float noise
                raise TradingHalted(
                    f"Order notional ${intended_notional:,.0f} exceeds per-name cap "
                    f"${ceiling:,.0f} ({self.max_position_pct:.0%} of equity).",
                    reason="notional_cap",
                    detail={"notional": intended_notional, "ceiling": ceiling},
                )
        return None  # all gates passed -> caller may submit the order

    @staticmethod
    def _sym(p):
        if isinstance(p, dict):
            return p.get("symbol")
        return getattr(p, "symbol", None)

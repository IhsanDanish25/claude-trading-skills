"""
circuit_breaker.py  —  strong-charisma drop-in

Four jobs, all as HARD pre-trade gates that RAISE (never return a bool):
  1. Position-cap enforcement   -> kills the "drifted to 8 positions" bug for good.
  2. Daily-loss + rapid-drawdown halt -> caps a runaway day.
  3. Emergency liquidation -> force-close all if equity drops > 2× max_daily_loss.
  4. Pyramiding gap guard -> rejects pyramid entries that would exceed per-name cap.

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


# Rapid intraday give-back trips earlier than the full daily limit.
RAPID_DRAWDOWN_RATIO = 0.67   # trip at 67% of max_daily_loss from the day's peak

# Emergency liquidation threshold: liquidate if equity drops more than this
# multiple of max_daily_loss from day-start (e.g. 2.0 × 5% = -10% day loss).
EMERGENCY_LIQUIDATION_MULT = 2.0


def default_max_daily_loss():
    """Read from config at runtime so env-var changes take effect without code edits."""
    try:
        from core.config import CIRCUIT_BREAKER_PCT, MAX_OPEN_POSITIONS, MAX_POSITION_SIZE_PCT
        return CIRCUIT_BREAKER_PCT, MAX_OPEN_POSITIONS, MAX_POSITION_SIZE_PCT
    except Exception:
        return 0.05, 10, 0.05  # safe fallback


class TradingHalted(Exception):
    """Raised when an order must be blocked. Reason lives on .reason."""
    def __init__(self, message, reason="unknown", detail=None):
        super().__init__(message)
        self.reason = reason
        self.detail = detail


class EmergencyLiquidation(Exception):
    """Raised when equity has dropped so far the bot must close everything."""
    def __init__(self, message, reason="emergency_liquidation", detail=None):
        super().__init__(message)
        self.reason = reason
        self.detail = detail


class CircuitBreaker:
    def __init__(
        self,
        get_account,            # callable -> object/dict with .equity (or ["equity"])
        get_positions,          # callable -> list of current open positions
        max_open_positions=None, # hard ceiling. Defaults to config.MAX_OPEN_POSITIONS.
        max_position_pct=None,   # per-name notional ceiling. Defaults to config.
        max_daily_loss=None,    # halt the day if equity down N% from the open. Default: config.
        day_start_equity=None,  # pre-market open equity from day_start_value.json.
    ):
        # Resolve None args from config at import time (not lazy, so errors surface early)
        cb_pct, mo_positions, mp_pct = default_max_daily_loss()
        self.max_open_positions = max_open_positions if max_open_positions is not None else mo_positions
        self.max_position_pct = max_position_pct if max_position_pct is not None else mp_pct
        self.max_daily_loss = max_daily_loss if max_daily_loss is not None else cb_pct

        self.get_account = get_account
        self.get_positions = get_positions

        self.trading_halted = False
        self._halt_reason = None
        self._day = None
        # _starting_equity is set once at __init__ from the pre-market open value
        # (day_start_value.json). Prevents tick-time drift from corrupting the
        # daily-loss baseline.
        self._starting_equity = day_start_equity
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
            self._peak_equity = equity  # reset peak on day-roll
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
        Raises EmergencyLiquidation if equity has cratered and everything must close.
        """
        account = self.get_account()
        equity = self._equity(account)
        if equity is None or equity <= 0:
            raise TradingHalted("No/invalid account equity", reason="no_equity")

        self._roll_day(equity)

        # 1) EMERGENCY LIQUIDATION — equity has cratered 2× past the daily limit
        #    (e.g. -10% on a 5% circuit). Close everything immediately.
        emergency_threshold = self._starting_equity * (1 - self.max_daily_loss * EMERGENCY_LIQUIDATION_MULT)
        if self._starting_equity and equity < emergency_threshold:
            raise EmergencyLiquidation(
                f"Emergency liquidation: equity ${equity:,.2f} below emergency "
                f"threshold ${emergency_threshold:,.2f} "
                f"({(equity / self._starting_equity - 1) * 100:+.2f}% from day-start). "
                f"Closing all positions.",
                reason="emergency_liquidation",
                detail={
                    "equity": equity,
                    "starting": self._starting_equity,
                    "threshold": emergency_threshold,
                    "drawdown_pct": (equity / self._starting_equity - 1) * 100,
                },
            )

        # 2) DAILY-LOSS HALT (sticky for the rest of the day)
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

        # 3) POSITION-COUNT CAP  (THE drift-bug killer)
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

        # 4) PER-NAME NOTIONAL CAP (including pyramids — closes the pyramiding gap)
        if intended_notional > 0 and symbol is not None:
            ceiling = equity * self.max_position_pct
            # Sum existing notional for this symbol (if any) so pyramids are capped
            existing_notional = 0.0
            for p in positions:
                if self._sym(p) == symbol:
                    existing_notional = abs(float(getattr(p, "market_value", 0) or 0))
                    break
            total_notional = intended_notional + existing_notional
            if total_notional > ceiling * 1.0001:  # tiny epsilon for float noise
                raise TradingHalted(
                    f"Order notional ${total_notional:,.0f} exceeds per-name cap "
                    f"${ceiling:,.0f} ({self.max_position_pct:.0%} of equity). "
                    f"(existing=${existing_notional:,.0f} + new=${intended_notional:,.0f})",
                    reason="notional_cap",
                    detail={
                        "existing": existing_notional,
                        "intended": intended_notional,
                        "total": total_notional,
                        "ceiling": ceiling,
                    },
                )
        return None  # all gates passed -> caller may submit the order

    @staticmethod
    def _sym(p):
        if isinstance(p, dict):
            return p.get("symbol")
        return getattr(p, "symbol", None)

    def liquidation_required(self) -> bool:
        """True when equity has crossed the emergency liquidation threshold.
        Check this instead of relying on check_before_order() raising so
        callers can explicitly close positions before the exception propagates."""
        if not self._starting_equity:
            return False
        account = self.get_account()
        equity = self._equity(account)
        if equity is None:
            return False
        emergency_threshold = self._starting_equity * (1 - self.max_daily_loss * EMERGENCY_LIQUIDATION_MULT)
        return equity < emergency_threshold
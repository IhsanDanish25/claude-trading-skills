"""
kelly_sizing.py  —  strong-charisma drop-in

Replaces the flat MAX_POSITION_SIZE_PCT=5% with EDGE-AWARE sizing:
  bet bigger when the strategy's edge is real, smaller when it's thin.

Formula (Kelly):   f* = (b*p - q) / b
    p = win rate, q = 1 - p, b = avg_win / avg_loss   (payoff ratio)

Then HALF-Kelly for safety (full Kelly is famously too violent for real trading),
clamped between a floor and your 5% ceiling. So it can NEVER size bigger than your
existing cap — it only ever sizes DOWN when the edge is weak. Pure upside, no new risk.

Feed it your realized closed-trade history (win rate + avg win/avg loss). With < min_trades
of history it falls back to your flat default so a cold-start bot behaves exactly like today.

No deps. Pure stdlib.
"""

from dataclasses import dataclass


@dataclass
class SizingResult:
    fraction: float        # fraction of equity to deploy on this name
    notional: float        # equity * fraction
    reason: str            # human-readable why
    raw_kelly: float       # un-clamped half-Kelly (for logging/diagnostics)


class KellySizer:
    def __init__(
        self,
        kelly_fraction=0.5,     # 0.5 = Half Kelly. 0.25 = Quarter (more conservative).
        max_position_pct=0.05,  # YOUR ceiling. Result is clamped to this. Never exceeds it.
        min_position_pct=0.01,  # floor so a valid signal still gets a real (tiny) position
        min_trades=20,          # below this, no trustworthy edge -> fall back to default
        default_pct=0.05,       # cold-start size = today's flat behavior
    ):
        self.kelly_fraction = kelly_fraction
        self.max_position_pct = max_position_pct
        self.min_position_pct = min_position_pct
        self.min_trades = min_trades
        self.default_pct = default_pct

    def size(self, equity, win_rate=None, avg_win=None, avg_loss=None, n_trades=0):
        """
        equity     : current account equity
        win_rate   : 0..1 realized win rate
        avg_win    : average WIN as a positive decimal return (e.g. 0.06 = +6%)
        avg_loss   : average LOSS as a positive decimal magnitude (e.g. 0.02 = -2%)
        n_trades   : number of closed trades behind those stats
        """
        # Cold start / not enough data -> behave exactly like the flat default today.
        if n_trades < self.min_trades or not win_rate or not avg_win or not avg_loss:
            frac = self.default_pct
            return SizingResult(frac, equity * frac,
                                f"fallback flat {frac:.0%} (n={n_trades} < {self.min_trades})", 0.0)

        b = avg_win / avg_loss            # payoff ratio
        p = win_rate
        q = 1.0 - p
        kelly = (b * p - q) / b           # full Kelly
        half = kelly * self.kelly_fraction

        # Negative edge -> Kelly says bet nothing. Sit out.
        if half <= 0:
            return SizingResult(0.0, 0.0,
                                f"negative edge (kelly={kelly:.3f}) -> no trade", half)

        # Clamp into [floor, your ceiling]. Cannot exceed your 5% cap.
        frac = max(self.min_position_pct, min(half, self.max_position_pct))
        return SizingResult(
            frac, equity * frac,
            f"half-kelly={half:.3f} clamped->{frac:.2%} (p={p:.0%}, b={b:.2f}, n={n_trades})",
            half,
        )


def stats_from_trades(trade_returns):
    """
    Helper: turn a list of closed-trade returns (decimals, e.g. +0.06, -0.02)
    into (win_rate, avg_win, avg_loss, n). avg_loss returned as POSITIVE magnitude.
    """
    if not trade_returns:
        return None, None, None, 0
    wins = [r for r in trade_returns if r > 0]
    losses = [-r for r in trade_returns if r < 0]
    n = len(trade_returns)
    win_rate = len(wins) / n if n else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    return win_rate, avg_win, avg_loss, n

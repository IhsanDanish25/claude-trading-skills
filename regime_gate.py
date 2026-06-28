"""
regime_gate.py  —  strong-charisma drop-in

THE likely #1 fix for your SPY underperformance.

VCP momentum bleeds money in choppy/ranging markets. This gate tells your bot
WHEN to fire and when to sit in cash, using SPY as the market proxy:

  Trend direction : SMA-50 vs SMA-200  (golden/death cross logic)
  Trend strength  : ADX(14)
                      ADX > 25  -> strong trend  (momentum / VCP works)
                      ADX < 20  -> ranging chop   (momentum DIES here -> stand down)

Decision:
  GO    -> SMA50 > SMA200  AND  ADX > 25         (uptrend + real strength)
  STAND_DOWN -> ranging (ADX < 20)  OR  downtrend (SMA50 < SMA200)
  NEUTRAL -> the in-between zone (trend up but ADX 20-25): allow, but you may
             choose to size down here.

Feed it daily SPY bars (your Alpaca IEX bars already give you this).
Pure-python ADX/SMA — no TA-Lib, lean for your M1.
"""

from dataclasses import dataclass

ADX_TRENDING = 25.0
ADX_RANGING = 20.0


@dataclass
class Regime:
    state: str          # "GO" | "NEUTRAL" | "STAND_DOWN"
    trend: str          # "up" | "down" | "flat"
    adx: float
    sma50: float
    sma200: float
    reason: str

    @property
    def can_trade(self):
        return self.state in ("GO", "NEUTRAL")


def _sma(values, n):
    if len(values) < n:
        return sum(values) / len(values)
    return sum(values[-n:]) / n


def _adx(highs, lows, closes, period=14):
    """
    Wilder's ADX, pure python. Needs ~2*period+1 bars to be meaningful.
    Returns 0.0 if not enough data (treated as 'ranging' -> safe default).
    """
    n = len(closes)
    if n < period * 2 + 1:
        return 0.0

    plus_dm, minus_dm, tr = [], [], []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))

    def wilder_smooth(seq, p):
        out = [sum(seq[:p])]
        for v in seq[p:]:
            out.append(out[-1] - out[-1] / p + v)
        return out

    if len(tr) < period:
        return 0.0
    str_ = wilder_smooth(tr, period)
    sp = wilder_smooth(plus_dm, period)
    sm = wilder_smooth(minus_dm, period)

    dx = []
    for i in range(len(str_)):
        if str_[i] == 0:
            dx.append(0.0)
            continue
        pdi = 100 * sp[i] / str_[i]
        mdi = 100 * sm[i] / str_[i]
        denom = pdi + mdi
        dx.append(100 * abs(pdi - mdi) / denom if denom else 0.0)

    if len(dx) < period:
        return dx[-1] if dx else 0.0
    adx = sum(dx[:period]) / period
    for v in dx[period:]:
        adx = (adx * (period - 1) + v) / period
    return adx


def classify(spy_highs, spy_lows, spy_closes):
    """
    Pass aligned lists of SPY daily HIGH/LOW/CLOSE (oldest -> newest),
    ideally >= 200 bars. Returns a Regime.
    """
    if len(spy_closes) < 50:
        return Regime("NEUTRAL", "flat", 0.0, 0.0, 0.0,
                      "insufficient SPY history -> neutral")

    sma50 = _sma(spy_closes, 50)
    sma200 = _sma(spy_closes, 200) if len(spy_closes) >= 200 else sma50
    adx = _adx(spy_highs, spy_lows, spy_closes, 14)

    trend = "up" if sma50 > sma200 else "down" if sma50 < sma200 else "flat"

    if trend == "up" and adx > ADX_TRENDING:
        return Regime("GO", trend, adx, sma50, sma200,
                      f"uptrend + strong trend (ADX {adx:.1f} > {ADX_TRENDING})")
    if trend == "down" or adx < ADX_RANGING:
        why = "downtrend" if trend == "down" else f"ranging chop (ADX {adx:.1f} < {ADX_RANGING})"
        return Regime("STAND_DOWN", trend, adx, sma50, sma200,
                      f"{why} -> VCP momentum stands down, hold cash")
    return Regime("NEUTRAL", trend, adx, sma50, sma200,
                  f"uptrend but ADX {adx:.1f} in 20-25 grey zone -> allow, size down")

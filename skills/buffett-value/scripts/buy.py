#!/usr/bin/env python3
"""
Buffett Value Buy Agent - Entry timing with candlestick patterns.

This agent handles entry decisions for stocks that have passed the fundamental
screening (analyst agent). It adds technical timing using candlestick patterns
and enforces a $10 minimum profit floor requirement.

Uses yfinance for price/volume data only (no FMP per user requirement).
Outputs buy signals with entry prices and position sizing recommendations.
"""

import yfinance as yf
import pandas as pd
import numpy as np
from typing import List, Dict, Any, Optional, Tuple
import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# Buy Agent Configuration
MIN_PROFIT_FLOOR = 10.0  # $10 minimum profit per share required
MIN_CONVICTION_SCORE = 0.7  # Minimum analyst conviction score to consider
MAX_DAYS_TO_ENTRY = 5  # Maximum days to wait for candlestick pattern after screen pass

# Candlestick pattern definitions
BULLISH_PATTERNS = [
    'bullish_engulfing',
    'hammer',
    'inverted_hammer',
    'morning_star',
    'piercing_line',
    'three_white_soldiers'
]

def generate_buy_signals(
    analyst_candidates: List[Dict[str, Any]],
    lookback_days: int = 20
) -> List[Dict[str, Any]]:
    """
    Generate buy signals for analyst-approved candidates using candlestick timing.

    Args:
        analyst_candidates: List of candidates from analyst agent that passed screening
        lookback_days: Days of price data to analyze for candlestick patterns

    Returns:
        List of buy signal dictionaries sorted by conviction score (highest first)
    """
    log.info(f"Generating buy signals for {len(analyst_candidates)} analyst candidates...")

    buy_signals = []

    for candidate in analyst_candidates:
        symbol = candidate['symbol']

        # Skip if analyst conviction is too low
        if candidate['conviction_score'] < MIN_CONVICTION_SCORE:
            log.debug(f"{symbol}: Analyst conviction {candidate['conviction_score']:.2f} < {MIN_CONVICTION_SCORE}")
            continue

        try:
            # Analyze for candlestick entry signals
            buy_signal = analyze_buy_signal(symbol, candidate, lookback_days)

            if buy_signal and buy_signal['is_buy_signal']:
                # Add analyst data to the signal
                buy_signal.update({
                    'analyst_conviction': candidate['conviction_score'],
                    'analyst_scores': candidate.get('fundamentals', {}),
                    'analyst_valuation': candidate.get('valuation', {}),
                    'company_name': candidate.get('company_name', symbol),
                    'sector': candidate.get('sector', 'Unknown'),
                    'industry': candidate.get('industry', 'Unknown')
                })

                buy_signals.append(buy_signal)
                log.info(f"{symbol}: BUY signal generated (conviction: {buy_signal['conviction_score']:.2f})")
            else:
                log.debug(f"{symbol}: No valid buy signal")

        except Exception as e:
            log.warning(f"Error generating buy signal for {symbol}: {e}")
            continue

    # Sort by conviction score (highest first)
    buy_signals.sort(key=lambda x: x['conviction_score'], reverse=True)

    log.info(f"Generated {len(buy_signals)} buy signals")
    return buy_signals

def analyze_buy_signal(
    symbol: str,
    analyst_candidate: Dict[str, Any],
    lookback_days: int = 20
) -> Optional[Dict[str, Any]]:
    """
    Analyze a single symbol for buy signals using candlestick patterns.

    Returns:
        Dictionary with buy signal details or None if no signal
    """
    try:
        # Get price data
        end_date = datetime.now()
        start_date = end_date - timedelta(days=lookback_days)

        stock = yf.Ticker(symbol)
        hist = stock.history(start=start_date, end=end_date)

        if hist.empty or len(hist) < 10:  # Need sufficient data for patterns
            log.debug(f"{symbol}: Insufficient price data")
            return None

        # Get current price
        current_price = hist['Close'].iloc[-1]

        # Check $10 minimum profit floor requirement
        # This assumes we're buying at current price and need $10 upside potential
        # In practice, this would be checked against fair value target from analyst
        profit_floor_met = check_profit_floor(symbol, current_price, analyst_candidate)
        if not profit_floor_met:
            log.debug(f"{symbol}: Profit floor requirement not met")
            return None

        # Detect candlestick patterns
        patterns_detected = detect_candlestick_patterns(hist)

        if not patterns_detected:
            log.debug(f"{symbol}: No bullish candlestick patterns detected")
            return None

        # Calculate conviction score based on pattern strength and analyst conviction
        pattern_score = calculate_pattern_score(patterns_detected)
        analyst_score = analyst_candidate['conviction_score']

        # Combined conviction (weighted average)
        conviction_score = (pattern_score * 0.4) + (analyst_score * 0.6)

        # Determine position sizing based on conviction
        position_size_pct = calculate_position_size(conviction_score)

        # Create buy signal
        signal = {
            'symbol': symbol,
            'is_buy_signal': True,
            'signal_price': round(current_price, 2),
            'signal_date': hist.index[-1].strftime('%Y-%m-%d'),
            'conviction_score': round(conviction_score, 3),
            'pattern_score': round(pattern_score, 3),
            'analyst_score': round(analyst_score, 3),
            'patterns_detected': patterns_detected,
            'position_size_pct': position_size_pct,
            'profit_floor_met': profit_floor_met,
            'stop_loss_price': round(current_price * 0.98, 2),  # 2% stop loss
            'max_hold_days': None,  # No calendar exit - hold until sell signal
            'entry_reason': f"Bullish {', '.join(patterns_detected)} pattern with {analyst_score:.1%} analyst conviction"
        }

        return signal

    except Exception as e:
        log.error(f"Failed to analyze buy signal for {symbol}: {e}")
        return None

def check_profit_floor(symbol: str, current_price: float, analyst_candidate: Dict[str, Any]) -> bool:
    """
    Check if the stock meets the $10 minimum profit floor requirement.

    This compares current price to analyst's fair value estimate to ensure
    there's at least $10 per share of profit potential.
    """
    try:
        # Try to get fair value from analyst's valuation
        valuation = analyst_candidate.get('valuation', {})
        valuation_details = valuation.get('valuation_details', {})

        # Use Graham number if available as fair value estimate
        graham_number = valuation_details.get('graham_number')
        if graham_number and graham_number > 0:
            profit_potential = graham_number - current_price
            return profit_potential >= MIN_PROFIT_FLOOR

        # Fallback: use a simple P/E based estimate if Graham not available
        # This is conservative - in production would use analyst's DCF or other IV calc
        pe_ratio = valuation_details.get('pe_ratio')
        eps = None

        # Try to get EPS from analyst fundamentals
        fundamentals = analyst_candidate.get('fundamentals', {})
        business_quality = fundamentals.get('business_quality', {})
        # This is simplified - real implementation would have EPS data

        # For now, use a heuristic: require at least 20% upside to meet $10 floor
        # assuming average stock price around $50 (so $10 is ~20%)
        # In practice, this should be calculated properly from IV
        upside_potential = 0.20  # Placeholder - would be calculated from IV

        # Conservative approach: if we can't calculate precisely, be restrictive
        # Only allow if current price is relatively low (making $10 floor easier)
        return current_price <= 50.0  # Conservative filter for $10 floor

    except Exception as e:
        log.debug(f"Profit floor check failed for {symbol}: {e}")
        # Fail safe - if we can't verify, don't allow the trade
        return False

def detect_candlestick_patterns(hist: pd.DataFrame) -> List[str]:
    """
    Detect bullish candlestick patterns in historical data.

    Returns:
        List of pattern names detected on the most recent candle
    """
    if len(hist) < 2:
        return []

    patterns = []

    # Get last few candles for pattern analysis
    if len(hist) >= 3:
        # Three-candle patterns (morning star)
        if is_morning_star(hist.iloc[-3:]):
            patterns.append('morning_star')

    if len(hist) >= 2:
        # Two-candle patterns
        if is_bullish_engulfing(hist.iloc[-2:]):
            patterns.append('bullish_engulfing')
        if is_piercing_line(hist.iloc[-2:]):
            patterns.append('piercing_line')

    # Single-candle patterns (always check last candle)
    last_candle = hist.iloc[-1]
    prev_candle = hist.iloc[-2] if len(hist) >= 2 else None

    if is_hammer(last_candle, prev_candle):
        patterns.append('hammer')
    if is_inverted_hammer(last_candle, prev_candle):
        patterns.append('inverted_hammer')
    if is_doji(last_candle):  # Doji needs confirmation but we'll flag it
        patterns.append('doji')

    # Three white soldiers (need 3 candles)
    if len(hist) >= 3:
        if is_three_white_soldiers(hist.iloc[-3:]):
            patterns.append('three_white_soldiers')

    return patterns

def is_bullish_engulfing(candles: pd.DataFrame) -> bool:
    """Check for bullish engulfing pattern."""
    if len(candles) < 2:
        return False

    prev = candles.iloc[-2]  # Previous candle
    curr = candles.iloc[-1]  # Current candle

    # Previous candle should be bearish (red)
    prev_bearish = prev['Close'] < prev['Open']
    # Current candle should be bullish (green)
    curr_bullish = curr['Close'] > curr['Open']
    # Current body should completely engulf previous body
    engulfs = (curr['Open'] <= prev['Close']) and (curr['Close'] >= prev['Open'])

    return prev_bearish and curr_bullish and engulfs

def is_hammer(candle: pd.Series, prev_candle: Optional[pd.Series]) -> bool:
    """Check for hammer pattern."""
    body_size = abs(candle['Close'] - candle['Open'])
    lower_shadow = candle['Open'] - candle['Low'] if candle['Close'] > candle['Open'] else candle['Close'] - candle['Low']
    upper_shadow = candle['High'] - candle['Close'] if candle['Close'] > candle['Open'] else candle['High'] - candle['Open']

    # Hammer: small body, long lower shadow (at least 2x body), little or no upper shadow
    return (body_size > 0 and
            lower_shadow >= 2 * body_size and
            upper_shadow <= body_size * 0.1)

def is_inverted_hammer(candle: pd.Series, prev_candle: Optional[pd.Series]) -> bool:
    """Check for inverted hammer pattern."""
    body_size = abs(candle['Close'] - candle['Open'])
    lower_shadow = candle['Open'] - candle['Low'] if candle['Close'] > candle['Open'] else candle['Close'] - candle['Low']
    upper_shadow = candle['High'] - candle['Close'] if candle['Close'] > candle['Open'] else candle['High'] - candle['Open']

    # Inverted hammer: small body, long upper shadow (at least 2x body), little or no lower shadow
    return (body_size > 0 and
            upper_shadow >= 2 * body_size and
            lower_shadow <= body_size * 0.1)

def is_doji(candle: pd.Series) -> bool:
    """Check for doji pattern (open ≈ close)."""
    body_size = abs(candle['Close'] - candle['Open'])
    total_range = candle['High'] - candle['Low']

    # Doji: very small body relative to total range
    return total_range > 0 and (body_size / total_range) <= 0.1

def is_morning_star(candles: pd.DataFrame) -> bool:
    """Check for morning star pattern (3 candles)."""
    if len(candles) < 3:
        return False

    first = candles.iloc[-3]   # First candle (bearish)
    second = candles.iloc[-2]  # Second candle (small body/doji)
    third = candles.iloc[-1]   # Third candle (bullish)

    # First candle: bearish
    first_bearish = first['Close'] < first['Open']
    # Second candle: small body (could be doji)
    second_small_body = abs(second['Close'] - second['Open']) <= (second['High'] - second['Low']) * 0.3
    # Third candle: bullish
    third_bullish = third['Close'] > third['Open']
    # Third candle should close well into first candle's body
    third_close_into_first = third['Close'] > (first['Open'] + first['Close']) / 2

    return first_bearish and second_small_body and third_bullish and third_close_into_first

def is_piercing_line(candles: pd.DataFrame) -> bool:
    """Check for piercing line pattern."""
    if len(candles) < 2:
        return False

    prev = candles.iloc[-2]  # First candle (bearish)
    curr = candles.iloc[-1]  # Second candle (bullish)

    # First candle: bearish
    prev_bearish = prev['Close'] < prev['Open']
    # Second candle: bullish
    curr_bullish = curr['Close'] > curr['Open']
    # Second candle opens below first's low
    opens_below_low = curr['Open'] < prev['Low']
    # Second candle closes at least halfway into first candle's body
    closes_halfway = curr['Close'] > (prev['Open'] + prev['Close']) / 2

    return prev_bearish and curr_bullish and opens_below_low and closes_halfway

def is_three_white_soldiers(candles: pd.DataFrame) -> bool:
    """Check for three white soldiers pattern."""
    if len(candles) < 3:
        return False

    first = candles.iloc[-3]
    second = candles.iloc[-2]
    third = candles.iloc[-1]

    # All three should be bullish with higher closes
    all_bullish = (first['Close'] > first['Open'] and
                   second['Close'] > second['Open'] and
                   third['Close'] > third['Open'])

    # Each should close higher than previous
    higher_closes = second['Close'] > first['Close'] and third['Close'] > second['Close']

    # Should have small upper shadows (showing strength)
    small_upper_shadows = ((first['High'] - first['Close']) <= (first['Close'] - first['Open']) * 0.5 and
                          (second['High'] - second['Close']) <= (second['Close'] - second['Open']) * 0.5 and
                          (third['High'] - third['Close']) <= (third['Close'] - third['Open']) * 0.5)

    return all_bullish and higher_closes and small_upper_shadows

def calculate_pattern_score(patterns: List[str]) -> float:
    """
    Calculate a score for the detected candlestick patterns.

    Returns:
        Score between 0.0 and 1.0
    """
    if not patterns:
        return 0.0

    # Pattern weights (more reliable patterns get higher scores)
    pattern_weights = {
        'three_white_soldiers': 0.9,
        'morning_star': 0.85,
        'bullish_engulfing': 0.8,
        'piercing_line': 0.75,
        'hammer': 0.7,
        'inverted_hammer': 0.65,
        'doji': 0.5  # Doji is neutral until confirmed
    }

    # Take the highest scoring pattern
    max_weight = max(pattern_weights.get(pattern, 0.5) for pattern in patterns)

    # Bonus for multiple patterns
    if len(patterns) > 1:
        max_weight = min(1.0, max_weight + 0.1 * (len(patterns) - 1))

    return max_weight

def calculate_position_size(conviction_score: float) -> float:
    """
    Calculate position size percentage based on conviction score.

    Returns:
        Position size as percentage of portfolio (e.g., 0.05 for 5%)
    """
    # Map conviction score (0-1) to position size (0-0.10 range for 5-10% positions)
    # Higher conviction = larger position size
    min_size = 0.03   # 3% minimum position
    max_size = 0.10   # 10% maximum position

    # Linear mapping: conviction 0.0 -> 3%, conviction 1.0 -> 10%
    position_size = min_size + (conviction_score * (max_size - min_size))

    return round(position_size, 3)

def get_top_buy_signals(
    analyst_candidates: List[Dict[str, Any]],
    limit: int = 10
) -> List[Dict[str, Any]]:
    """
    Convenience function to get top buy signals.

    Args:
        analyst_candidates: List of candidates from analyst agent
        limit: Maximum number of signals to return

    Returns:
        Top buy signals sorted by conviction score
    """
    signals = generate_buy_signals(analyst_candidates)
    return signals[:limit]

if __name__ == "__main__":
    # Test with mock analyst candidates
    test_candidates = [
        {
            'symbol': 'AAPL',
            'conviction_score': 0.75,
            'company_name': 'Apple Inc.',
            'sector': 'Technology',
            'industry': 'Consumer Electronics',
            'current_price': 175.0,
            'valuation': {
                'valuation_details': {
                    'graham_number': 200.0,
                    'pe_ratio': 25.0
                }
            },
            'fundamentals': {
                'business_quality': {'score': 0.8},
                'consistency': {'score': 0.7},
                'valuation': {'score': 0.75}
            }
        }
    ]

    logging.basicConfig(level=logging.INFO)

    print("Testing Buffett Value Buy Agent...")
    signals = get_top_buy_signals(test_candidates, limit=5)

    print(f"\nGenerated {len(signals)} buy signals:")
    for i, signal in enumerate(signals, 1):
        print(f"{i:2d}. {signal['symbol']} - {signal['company_name']}")
        print(f"    Signal Price: ${signal['signal_price']:.2f}")
        print(f"    Conviction: {signal['conviction_score']:.2f}")
        print(f"    Patterns: {', '.join(signal['patterns_detected'])}")
        print(f"    Position Size: {signal['position_size_pct']:.1%}")
        print(f"    Reason: {signal['entry_reason']}")
        print()
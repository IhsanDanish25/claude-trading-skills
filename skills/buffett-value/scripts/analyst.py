#!/usr/bin/env python3
"""
Buffett Value Analyst Agent - Fundamental screening for high-quality businesses.

This agent screens for stocks that meet Warren Buffett's investment criteria:
- Business Quality: High ROE, ROIC, low debt, stable margins
- Consistency: Years of positive EPS growth, no dividend cuts
- Valuation: Margin of safety via intrinsic value calculation

Uses yfinance for fundamental data only (no FMP per user requirement).
Outputs ranked candidates for the buy agent to evaluate.
"""

import yfinance as yf
import pandas as pd
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional
import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# Buffett Value Screening Criteria
MIN_ROE = 0.15          # 15% minimum ROE
MIN_ROIC = 0.12         # 12% minimum ROIC
MAX_DEBT_TO_EQUITY = 0.5 # Maximum debt-to-equity ratio
MIN_INTEREST_COVERAGE = 8.0 # Minimum interest coverage (EBIT/Interest)
MIN_EPS_GROWTH_YEARS = 8   # Minimum years of positive EPS growth (out of last 10)
MAX_PE_RATIO = 25.0     # Maximum P/E ratio (relative to historical average)
MIN_MARGIN_OF_SAFETY = 0.25 # 25% minimum discount to intrinsic value

SCREEN_MAX_WORKERS = 10  # concurrent yfinance requests -- high enough to turn a
# multi-minute serial screen into seconds, conservative enough to avoid
# tripping Yahoo's undocumented soft rate-limiting on very high concurrency.


def screen_for_buffett_candidates(
    symbols: List[str],
    lookback_years: int = 10,
    max_workers: int = SCREEN_MAX_WORKERS,
) -> List[Dict[str, Any]]:
    """
    Screen a list of symbols for Buffett-value candidates.

    Each symbol requires 4 separate yfinance round trips (.info,
    .financials, .balance_sheet, .cashflow) -- for a ~100-symbol universe
    that's ~400 sequential HTTP calls if run one at a time, which can take
    several minutes. These calls are I/O-bound (network wait, not CPU), so
    a thread pool gives a large real speedup with no change in behavior:
    each symbol is still analyzed independently, one failure doesn't affect
    another, and the final ordering is identical (sorted by conviction
    score afterward, not by completion order).

    Args:
        symbols: List of stock symbols to screen
        lookback_years: Years of historical data to analyze
        max_workers: Concurrent yfinance requests

    Returns:
        List of candidate dictionaries sorted by conviction score (highest first)
    """
    log.info(f"Screening {len(symbols)} symbols for Buffett value candidates "
             f"(concurrency={max_workers})...")

    candidates = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_symbol = {
            executor.submit(analyze_symbol, symbol, lookback_years): symbol
            for symbol in symbols
        }
        for future in as_completed(future_to_symbol):
            symbol = future_to_symbol[future]
            try:
                candidate = future.result()
                if candidate and candidate['passes_screen']:
                    candidates.append(candidate)
                    log.debug(f"{symbol}: PASSED screen (score: {candidate['conviction_score']:.1f})")
                else:
                    log.debug(f"{symbol}: FAILED screen")
            except Exception as e:
                log.warning(f"Error analyzing {symbol}: {e}")
                continue

    # Sort by conviction score (highest first) -- deterministic regardless
    # of the concurrent completion order above.
    candidates.sort(key=lambda x: x['conviction_score'], reverse=True)

    log.info(f"Found {len(candidates)} Buffett value candidates")
    return candidates

def analyze_symbol(symbol: str, lookback_years: int = 10) -> Optional[Dict[str, Any]]:
    """
    Analyze a single symbol against Buffett criteria.

    Returns:
        Dictionary with analysis results or None if analysis failed
    """
    try:
        # Get stock data
        stock = yf.Ticker(symbol)

        # Get fundamental data
        info = stock.info
        financials = stock.financials
        balance_sheet = stock.balance_sheet
        cashflow = stock.cashflow

        if not info or financials.empty:
            log.debug(f"{symbol}: Insufficient data")
            return None

        # Extract key metrics
        analysis = {
            'symbol': symbol,
            'company_name': info.get('longName', symbol),
            'sector': info.get('sector', 'Unknown'),
            'industry': info.get('industry', 'Unknown'),
            'current_price': info.get('currentPrice', info.get('regularMarketPrice', 0)),
            'market_cap': info.get('marketCap', 0),
            'passes_screen': False,
            'conviction_score': 0.0,
            'criteria_met': {},
            'criteria_failed': {},
            'valuation': {},
            'fundamentals': {}
        }

        # 1. BUSINESS QUALITY ANALYSIS
        business_quality_score = analyze_business_quality(stock, info, financials, balance_sheet)
        analysis['criteria_met']['business_quality'] = business_quality_score['passed']
        analysis['criteria_failed']['business_quality'] = not business_quality_score['passed']
        analysis['fundamentals']['business_quality'] = business_quality_score

        # 2. CONSISTENCY ANALYSIS
        consistency_score = analyze_consistency(financials, info, lookback_years)
        analysis['criteria_met']['consistency'] = consistency_score['passed']
        analysis['criteria_failed']['consistency'] = not consistency_score['passed']
        analysis['fundamentals']['consistency'] = consistency_score

        # 3. VALUATION ANALYSIS
        valuation_score = analyze_valuation(stock, info, financials)
        analysis['criteria_met']['valuation'] = valuation_score['passed']
        analysis['criteria_failed']['valuation'] = not valuation_score['passed']
        analysis['valuation'] = valuation_score

        # Calculate overall conviction score (weighted average)
        scores = [
            business_quality_score['score'] * 0.4,  # 40% weight
            consistency_score['score'] * 0.3,       # 30% weight
            valuation_score['score'] * 0.3          # 30% weight
        ]

        conviction_score = sum(scores)
        analysis['conviction_score'] = conviction_score

        # Determine if passes screen (minimum threshold)
        # All three categories must meet minimum thresholds
        min_threshold = 0.6  # 60% minimum in each category
        passes = (
            business_quality_score['score'] >= min_threshold and
            consistency_score['score'] >= min_threshold and
            valuation_score['score'] >= min_threshold
        )

        analysis['passes_screen'] = passes

        if passes:
            log.info(f"{symbol}: Buffett candidate found (score: {conviction_score:.2f})")

        return analysis

    except Exception as e:
        log.error(f"Failed to analyze {symbol}: {e}")
        return None

def analyze_business_quality(stock, info, financials, balance_sheet) -> Dict[str, Any]:
    """
    Analyze business quality criteria.

    Returns:
        Dict with 'score' (0-1) and 'passed' (bool) fields
    """
    score_components = []
    failed_criteria = []

    # ROE (Return on Equity)
    roe = info.get('returnOnEquity')
    if roe is not None:
        roe_score = min(roe / MIN_ROE, 1.0) if roe >= 0 else 0.0
        score_components.append(('ROE', roe_score, roe >= MIN_ROE))
        if roe < MIN_ROE:
            failed_criteria.append(f"ROE {roe:.1%} < {MIN_ROE:.0%}")
    else:
        score_components.append(('ROE', 0.0, False))
        failed_criteria.append("ROE data unavailable")

    # ROIC (Return on Invested Capital) - approximate using available data
    roic = info.get('returnOnAssets')  # Using ROA as approximation
    if roic is not None:
        roic_score = min(roic / MIN_ROIC, 1.0) if roic >= 0 else 0.0
        score_components.append(('ROIC', roic_score, roic >= MIN_ROIC))
        if roic < MIN_ROIC:
            failed_criteria.append(f"ROIC {roic:.1%} < {MIN_ROIC:.0%}")
    else:
        score_components.append(('ROIC', 0.0, False))
        failed_criteria.append("ROIC data unavailable")

    # Debt-to-Equity Ratio
    debt_to_equity = info.get('debtToEquity')
    if debt_to_equity is not None:
        # Lower is better - score inversely
        if debt_to_equity <= MAX_DEBT_TO_EQUITY:
            debt_score = 1.0 - (debt_to_equity / MAX_DEBT_TO_EQUITY) * 0.5  # Better score for lower debt
            debt_score = min(max(debt_score, 0.0), 1.0)
        else:
            debt_score = 0.0
        score_components.append(('Debt/Equity', debt_score, debt_to_equity <= MAX_DEBT_TO_EQUITY))
        if debt_to_equity > MAX_DEBT_TO_EQUITY:
            failed_criteria.append(f"Debt/Equity {debt_to_equity:.2f} > {MAX_DEBT_TO_EQUITY}")
    else:
        score_components.append(('Debt/Equity', 0.5, True))  # Neutral if data unavailable
        # Don't fail on missing debt data

    # Interest Coverage (EBIT/Interest Expense) - approximate
    try:
        if not financials.empty and 'EBIT' in financials.index and 'Interest Expense' in financials.index:
            ebit = financials.loc['EBIT'].iloc[0] if not financials.loc['EBIT'].empty else 0
            interest_expense = abs(financials.loc['Interest Expense'].iloc[0]) if not financials.loc['Interest Expense'].empty else 1
            if interest_expense > 0:
                interest_coverage = ebit / interest_expense
                interest_score = min(interest_coverage / MIN_INTEREST_COVERAGE, 1.0) if interest_coverage >= 0 else 0.0
                score_components.append(('Interest Coverage', interest_score, interest_coverage >= MIN_INTEREST_COVERAGE))
                if interest_coverage < MIN_INTEREST_COVERAGE:
                    failed_criteria.append(f"Interest Coverage {interest_coverage:.1f}x < {MIN_INTEREST_COVERAGE:.0f}x")
            else:
                score_components.append(('Interest Coverage', 1.0, True))  # No interest expense is good
        else:
            score_components.append(('Interest Coverage', 0.5, True))  # Neutral if data unavailable
    except:
        score_components.append(('Interest Coverage', 0.5, True))  # Neutral on error

    # Calculate weighted average
    if score_components:
        total_score = sum(score for _, score, _ in score_components) / len(score_components)
        passed = all(passed for _, _, passed in score_components)
    else:
        total_score = 0.0
        passed = False

    return {
        'score': total_score,
        'passed': passed,
        'failed_criteria': failed_criteria,
        'details': {name: {'score': score, 'passed': passed} for name, score, passed in score_components}
    }

def analyze_consistency(financials, info, lookback_years: int) -> Dict[str, Any]:
    """
    Analyze consistency criteria (EPS growth, dividend history).

    Returns:
        Dict with 'score' (0-1) and 'passed' (bool) fields
    """
    score_components = []
    failed_criteria = []

    # EPS Growth Consistency
    try:
        if not financials.empty and 'Diluted EPS' in financials.index:
            eps_series = financials.loc['Diluted EPS']
            # Get last 10 years of annual data
            recent_years = eps_series.dropna().tail(lookback_years)
            if len(recent_years) >= lookback_years:
                # Count years with positive EPS growth
                positive_growth_years = 0
                for i in range(1, len(recent_years)):
                    if recent_years.iloc[i] > recent_years.iloc[i-1]:
                        positive_growth_years += 1

                eps_growth_ratio = positive_growth_years / (len(recent_years) - 1) if len(recent_years) > 1 else 0
                eps_score = eps_growth_ratio  # Linear scaling
                score_components.append(('EPS Growth Consistency', eps_score, eps_growth_ratio >= (MIN_EPS_GROWTH_YEARS / lookback_years)))
                if eps_growth_ratio < (MIN_EPS_GROWTH_YEARS / lookback_years):
                    failed_criteria.append(f"EPS growth only {positive_growth_years}/{len(recent_years)-1} years")
            else:
                score_components.append(('EPS Growth Consistency', 0.0, False))
                failed_criteria.append("Insufficient EPS history")
        else:
            score_components.append(('EPS Growth Consistency', 0.0, False))
            failed_criteria.append("EPS data unavailable")
    except Exception as e:
        log.debug(f"EPS consistency analysis failed: {e}")
        score_components.append(('EPS Growth Consistency', 0.0, False))
        failed_criteria.append("EPS consistency analysis error")

    # Dividend Consistency (if dividend payer)
    dividend_yield = info.get('dividendYield', 0)
    if dividend_yield and dividend_yield > 0:
        # Check payout ratio sustainability
        payout_ratio = info.get('payoutRatio')
        if payout_ratio is not None:
            # Sustainable payout ratio < 0.75 (75%)
            if payout_ratio <= 0.75:
                dividend_score = 1.0
            else:
                # Penalize high payout ratios
                dividend_score = max(0.0, 1.0 - ((payout_ratio - 0.75) / 0.25))  # 0.75-1.0 range maps to 1.0-0.0
            score_components.append(('Dividend Sustainability', dividend_score, payout_ratio <= 0.75))
            if payout_ratio > 0.75:
                failed_criteria.append(f"Dividend payout ratio {payout_ratio:.1%} > 75% (unsustainable)")
        else:
            score_components.append(('Dividend Sustainability', 0.5, True))  # Neutral if data unavailable
    else:
        # Non-dividend payer - neutral (not a requirement for Buffett)
        score_components.append(('Dividend Consistency', 0.5, True))

    # Calculate weighted average
    if score_components:
        total_score = sum(score for _, score, _ in score_components) / len(score_components)
        passed = all(passed for _, _, passed in score_components)
    else:
        total_score = 0.0
        passed = False

    return {
        'score': total_score,
        'passed': passed,
        'failed_criteria': failed_criteria,
        'details': {name: {'score': score, 'passed': passed} for name, score, passed in score_components}
    }

def analyze_valuation(stock, info, financials) -> Dict[str, Any]:
    """
    Analyze valuation using intrinsic value methods (Graham formula simplified).

    Returns:
        Dict with 'score' (0-1) and 'passed' (bool) fields
    """
    score_components = []
    failed_criteria = []
    valuation_details = {}

    current_price = info.get('currentPrice', info.get('regularMarketPrice', 0))
    if not current_price or current_price <= 0:
        score_components.append(('Valuation', 0.0, False))
        failed_criteria.append("Invalid current price")
        return {
            'score': 0.0,
            'passed': False,
            'failed_criteria': failed_criteria,
            'valuation_details': valuation_details
        }

    # Method 1: Graham Number (simplified)
    try:
        eps = info.get('trailingEps')
        book_value_per_share = info.get('bookValue')

        if eps and eps > 0 and book_value_per_share and book_value_per_share > 0:
            # Graham Number = sqrt(22.5 * EPS * Book Value per Share)
            graham_number = (22.5 * eps * book_value_per_share) ** 0.5
            graham_ratio = graham_number / current_price if current_price > 0 else 0

            # Score based on margin of safety
            if graham_ratio >= 1.0 + MIN_MARGIN_OF_SAFETY:  # At least 25% undervalued
                graham_score = min((graham_ratio - 1.0) / 0.5, 1.0)  # Cap at 1.0 for 75%+ undervaluation
            else:
                graham_score = 0.0

            score_components.append(('Graham Number', graham_score, graham_ratio >= 1.0 + MIN_MARGIN_OF_SAFETY))
            valuation_details['graham_number'] = graham_number
            valuation_details['graham_ratio'] = graham_ratio

            if graham_ratio < 1.0 + MIN_MARGIN_OF_SAFETY:
                failed_criteria.append(f"Graham number {graham_number:.2f} implies < {MIN_MARGIN_OF_SAFETY:.0%} margin of safety")
        else:
            score_components.append(('Graham Number', 0.0, False))
            failed_criteria.append("EPS or Book Value data unavailable for Graham Number")
    except Exception as e:
        log.debug(f"Graham Number calculation failed: {e}")
        score_components.append(('Graham Number', 0.0, False))
        failed_criteria.append("Graham Number calculation error")

    # Method 2: P/E Ratio Analysis (vs historical)
    try:
        pe_ratio = info.get('trailingPE')
        if pe_ratio and pe_ratio > 0:
            # Buffett likes companies with P/E below historical average
            # For simplicity, using absolute threshold - in practice would compare to 10-year avg
            pe_score = max(0.0, 1.0 - ((pe_ratio - 10.0) / 15.0))  # 10 PE = 1.0 score, 25 PE = 0.0 score
            pe_score = min(max(pe_score, 0.0), 1.0)

            # Require P/E < 25 for Buffett-style value
            pe_passed = pe_ratio <= MAX_PE_RATIO
            score_components.append(('P/E Ratio', pe_score, pe_passed))
            valuation_details['pe_ratio'] = pe_ratio

            if not pe_passed:
                failed_criteria.append(f"P/E ratio {pe_ratio:.1f} > {MAX_PE_RATIO}")
        else:
            score_components.append(('P/E Ratio', 0.0, False))
            failed_criteria.append("P/E ratio data unavailable")
    except Exception as e:
        log.debug(f"P/E analysis failed: {e}")
        score_components.append(('P/E Ratio', 0.0, False))
        failed_criteria.append("P/E analysis error")

    # Method 3: Price-to-Book Ratio
    try:
        price_to_book = info.get('priceToBook')
        if price_to_book and price_to_book > 0:
            # Buffett prefers PB < 3.0 typically
            pb_score = max(0.0, 1.0 - ((price_to_book - 1.0) / 2.0))  # 1.0 PB = 1.0 score, 3.0 PB = 0.0 score
            pb_score = min(max(pb_score, 0.0), 1.0)

            pb_passed = price_to_book <= 3.0
            score_components.append(('Price/Book', pb_score, pb_passed))
            valuation_details['price_to_book'] = price_to_book

            if not pb_passed:
                failed_criteria.append(f"Price/Book {price_to_book:.1f} > 3.0")
        else:
            score_components.append(('Price/Book', 0.0, False))
            failed_criteria.append("Price/Book ratio data unavailable")
    except Exception as e:
        log.debug(f"P/B analysis failed: {e}")
        score_components.append(('Price/Book', 0.0, False))
        failed_criteria.append("P/B analysis error")

    # Calculate weighted average (equal weighting for simplicity)
    if score_components:
        total_score = sum(score for _, score, _ in score_components) / len(score_components)
        passed = all(passed for _, _, passed in score_components)
    else:
        total_score = 0.0
        passed = False

    return {
        'score': total_score,
        'passed': passed,
        'failed_criteria': failed_criteria,
        'valuation_details': valuation_details
    }

def get_top_candidates(symbols: List[str], limit: int = 20) -> List[Dict[str, Any]]:
    """
    Convenience function to get top Buffett candidates.

    Args:
        symbols: List of symbols to screen
        limit: Maximum number of candidates to return

    Returns:
        Top candidates sorted by conviction score
    """
    candidates = screen_for_buffett_candidates(symbols)
    return candidates[:limit]

if __name__ == "__main__":
    # Test with some known quality companies
    test_symbols = ['AAPL', 'MSFT', 'JNJ', 'PG', 'KO', 'WMT', 'DIS', 'V', 'MA', 'HD']
    logging.basicConfig(level=logging.INFO)

    print("Testing Buffett Value Analyst...")
    candidates = get_top_candidates(test_symbols, limit=10)

    print(f"\nFound {len(candidates)} candidates:")
    for i, candidate in enumerate(candidates, 1):
        print(f"{i:2d}. {candidate['symbol']} - {candidate['company_name']}")
        print(f"    Score: {candidate['conviction_score']:.2f} | Price: ${candidate['current_price']:.2f}")
        print(f"    Passed: {candidate['passes_screen']}")
        if candidate['criteria_failed']:
            failed_areas = [area for area, failed in candidate['criteria_failed'].items() if failed]
            print(f"    Failed: {', '.join(failed_areas)}")
        print()
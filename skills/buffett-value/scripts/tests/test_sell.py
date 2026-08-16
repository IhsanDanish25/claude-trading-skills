"""Unit tests for sell.py -- Buffett Value Sell Agent."""

import sell

from conftest import make_entry_snapshot


def make_position(symbol="AAPL", qty=10, entry_price=150.0, graham_number=220.0):
    return {
        "symbol": symbol,
        "qty": qty,
        "entry_price": entry_price,
        "entry_snapshot": make_entry_snapshot(symbol=symbol, graham_number=graham_number),
    }


def make_clean_monitor_result():
    return {
        "fundamentals_deteriorated": False,
        "deterioration_reasons": [],
        "better_opportunity": None,
    }


def test_check_profit_target_reached_true():
    assert sell.check_profit_target_reached(225.0, 220.0) is True


def test_check_profit_target_reached_false():
    assert sell.check_profit_target_reached(200.0, 220.0) is False


def test_check_profit_target_reached_no_target():
    assert sell.check_profit_target_reached(225.0, None) is False


def test_evaluate_exit_profit_target_met_with_floor():
    position = make_position(entry_price=150.0, graham_number=220.0)
    monitor_result = make_clean_monitor_result()

    decision = sell.evaluate_exit(position, monitor_result, current_price=222.0)

    assert decision is not None
    assert decision["reason_code"] == "profit_target"
    assert decision["order"]["order_class"] == "simple"


def test_evaluate_exit_profit_target_met_but_below_floor_holds():
    # graham target reached but only $2/share profit -- below $10 floor
    position = make_position(entry_price=218.5, graham_number=220.0)
    monitor_result = make_clean_monitor_result()

    decision = sell.evaluate_exit(position, monitor_result, current_price=220.5)

    assert decision is None


def test_evaluate_exit_thesis_broken():
    position = make_position(entry_price=150.0, graham_number=220.0)
    monitor_result = {
        "fundamentals_deteriorated": True,
        "deterioration_reasons": ["ROE score dropped from 1.00 to 0.50"],
        "better_opportunity": None,
    }

    decision = sell.evaluate_exit(position, monitor_result, current_price=160.0)

    assert decision is not None
    assert decision["reason_code"] == "thesis_broken"
    assert "ROE" in decision["reason_detail"]


def test_evaluate_exit_better_opportunity():
    position = make_position(entry_price=150.0, graham_number=220.0)
    monitor_result = {
        "fundamentals_deteriorated": False,
        "deterioration_reasons": [],
        "better_opportunity": {"symbol": "MSFT", "conviction_score": 0.95},
    }

    decision = sell.evaluate_exit(position, monitor_result, current_price=160.0)

    assert decision is not None
    assert decision["reason_code"] == "better_opportunity"
    assert "MSFT" in decision["reason_detail"]


def test_evaluate_exit_none_when_all_clear():
    position = make_position(entry_price=150.0, graham_number=220.0)
    monitor_result = make_clean_monitor_result()

    decision = sell.evaluate_exit(position, monitor_result, current_price=160.0)

    assert decision is None


def test_build_sell_order_is_simple_not_bracket():
    order = sell.build_sell_order("AAPL", 10, 225.0)

    assert order["order_class"] == "simple"
    assert order["side"] == "sell"
    assert order["type"] == "limit"
    assert "stop_price" not in order
    assert "take_profit" not in order
    assert "stop_loss" not in order


def test_generate_sell_signals_skips_missing_price():
    positions = [make_position(symbol="AAPL"), make_position(symbol="MSFT")]
    monitor_results = {
        "AAPL": make_clean_monitor_result(),
        "MSFT": {
            "fundamentals_deteriorated": True,
            "deterioration_reasons": ["thesis broken"],
            "better_opportunity": None,
        },
    }
    # No price for AAPL -- must be skipped, not error
    current_prices = {"MSFT": 100.0}

    exits = sell.generate_sell_signals(positions, monitor_results, current_prices)

    assert len(exits) == 1
    assert exits[0]["symbol"] == "MSFT"


def test_generate_sell_signals_returns_no_exits_when_all_positions_healthy():
    positions = [make_position(symbol="AAPL", entry_price=150.0, graham_number=220.0)]
    monitor_results = {"AAPL": make_clean_monitor_result()}
    current_prices = {"AAPL": 160.0}

    exits = sell.generate_sell_signals(positions, monitor_results, current_prices)

    assert exits == []

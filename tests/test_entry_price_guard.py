"""Regression tests for stale signal protection at order time."""
from live_trading.trading.entry_guard import validate_entry_price_distance


def test_sell_far_from_signal_is_blocked():
    result = validate_entry_price_distance(
        "SELL",
        signal_price=4368.59,
        execution_price=4350.39,
        atr=5.0,
        max_atr_distance=0.75,
    )

    assert result.allowed is False
    assert result.distance_atr == 3.64
    assert "stale" in result.reason


def test_buy_far_from_signal_is_blocked():
    result = validate_entry_price_distance(
        "BUY",
        signal_price=4350.00,
        execution_price=4360.00,
        atr=4.0,
        max_atr_distance=0.75,
    )

    assert result.allowed is False
    assert result.distance_atr == 2.5


def test_both_directions_allow_small_drift():
    sell = validate_entry_price_distance("SELL", 4368.59, 4366.00, 5.0, 0.75)
    buy = validate_entry_price_distance("BUY", 4350.00, 4352.00, 4.0, 0.75)

    assert sell.allowed is True
    assert buy.allowed is True


def test_invalid_atr_fails_closed():
    result = validate_entry_price_distance("SELL", 100.0, 99.0, 0.0, 0.75)

    assert result.allowed is False
    assert "invalid" in result.reason
"""Regression tests for stale signal protection at order time."""
from live_trading.trading.entry_guard import (
    staleness_limit_for_regime,
    validate_entry_price_distance,
)


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


def test_staleness_limit_is_selected_by_regime():
    limits = (1.30, 1.00, 0.75)
    assert staleness_limit_for_regime("STRONG_TREND_BULL", *limits) == 1.30
    assert staleness_limit_for_regime("STRONG_TREND_BEAR", *limits) == 1.30
    assert staleness_limit_for_regime("HIGH_VOLATILITY", *limits) == 1.30
    assert staleness_limit_for_regime("WEAK_TREND_BULL", *limits) == 1.00
    assert staleness_limit_for_regime("WEAK_TREND_BEAR", *limits) == 1.00
    assert staleness_limit_for_regime("RANGE", *limits) == 0.75
    assert staleness_limit_for_regime("LOW_VOLATILITY", *limits) == 0.75
    assert staleness_limit_for_regime("DISTRIBUTION", *limits) == 0.75
    assert staleness_limit_for_regime("PULLBACK_BULL", *limits) == 0.75


def test_strong_trend_limit_allows_drift_that_range_limit_blocks():
    strong_limit = staleness_limit_for_regime(
        "STRONG_TREND_BULL", 1.30, 1.00, 0.75
    )
    strong = validate_entry_price_distance(
        "BUY", 100.0, 104.0, 4.0, strong_limit,
        regime="STRONG_TREND_BULL",
    )
    range_result = validate_entry_price_distance(
        "BUY", 100.0, 104.0, 4.0, 0.75, regime="RANGE",
    )

    assert strong.allowed is True
    assert range_result.allowed is False
    assert "limit is 3.00 (0.75 ATR, regime=RANGE)" in range_result.reason


def test_stale_signal_log_includes_regime_and_applied_limit():
    result = validate_entry_price_distance(
        "BUY", 100.0, 106.0, 4.0, 1.30,
        regime="STRONG_TREND_BULL",
    )

    assert result.allowed is False
    assert "limit is 5.20 (1.30 ATR, regime=STRONG_TREND_BULL)" in result.reason
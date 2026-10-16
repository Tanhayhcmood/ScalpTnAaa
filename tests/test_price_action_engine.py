"""Regression tests for the live Price Action signal gate."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import live_trading.signals.price_action_engine as price_action_engine
from live_trading.signals.decision_engine import _entry_location_block_reason
from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.price_action_engine import (
    CFG_M5,
    _detect_breakout_pullback,
    _detect_inside_bar_breakout,
    _detect_sr_levels,
    _detect_supply_demand,
    analyze_price_action,
)


def _base_candles(count: int = 60, price: float = 2500.0) -> list[OHLCV]:
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    candles: list[OHLCV] = []
    for index in range(count):
        close = price + (0.02 if index % 2 else -0.02)
        candles.append(
            OHLCV(
                time=(start + timedelta(minutes=5 * index)).isoformat(),
                open=price,
                high=price + 0.15,
                low=price - 0.15,
                close=close,
                volume=100.0,
            )
        )
        price = close
    return candles


def _analyze_with_levels(
    monkeypatch,
    candle: OHLCV,
    *,
    support_levels=None,
    resistance_levels=None,
    demand_zones=None,
    supply_zones=None,
):
    candles = _base_candles()
    candles[-1] = candle
    monkeypatch.setattr(
        price_action_engine,
        "_detect_patterns",
        lambda *_args: (False, False, False, False, False, False),
    )
    monkeypatch.setattr(
        price_action_engine,
        "_detect_sr_levels",
        lambda *_args, **_kwargs: (
            support_levels or [],
            resistance_levels or [],
        ),
    )
    monkeypatch.setattr(
        price_action_engine,
        "_detect_supply_demand",
        lambda *_args: (
            demand_zones or [],
            supply_zones or [],
        ),
    )
    monkeypatch.setattr(
        price_action_engine,
        "_detect_breakout_pullback",
        lambda *_args: (
            False, False, False, False, False, False,
            False, False, False, 0, None, 0.0,
        ),
    )
    return analyze_price_action(candles)


def test_standalone_bullish_engulfing_is_not_hidden_by_old_score_denominator():
    candles = _base_candles()
    candles[-2] = OHLCV(
        candles[-2].time, 2499.90, 2500.05, 2499.65, 2499.75, 100.0
    )
    candles[-1] = OHLCV(
        candles[-1].time, 2499.74, 2500.15, 2499.35, 2500.05, 120.0
    )

    result = analyze_price_action(candles, timeframe="M5")

    assert result.bullish_engulf
    assert result.pa_signal == "BUY"
    assert result.pa_score >= 0.20


def test_pin_bar_without_relevant_level_is_context_only():
    candles = _base_candles()
    candles[-1] = OHLCV(
        candles[-1].time, 2500.00, 2500.10, 2499.20, 2500.04, 120.0
    )

    result = analyze_price_action(candles)

    assert result.bullish_pin_bar
    assert result.pa_signal == "NEUTRAL"


def test_inside_bar_breakout_is_recognized_without_a_clustered_sr_level():
    candles = _base_candles()
    candles[-3] = OHLCV(
        candles[-3].time, 2499.70, 2500.40, 2499.40, 2499.85, 100.0
    )
    candles[-2] = OHLCV(
        candles[-2].time, 2499.82, 2500.10, 2499.65, 2499.90, 100.0
    )
    candles[-1] = OHLCV(
        candles[-1].time, 2499.90, 2500.55, 2499.85, 2500.45, 130.0
    )

    result = analyze_price_action(candles)

    assert result.bullish_inside_breakout
    assert result.pa_signal == "BUY"
    assert result.inside_bar_detected
    assert result.inside_bar_depth == 1
    assert result.inside_bar_breakout_level == 2500.40


def test_inside_bar_does_not_break_until_price_closes_beyond_mother_range():
    candles = _base_candles()
    candles[-3] = OHLCV(
        candles[-3].time, 2499.70, 2500.40, 2499.40, 2499.85, 100.0
    )
    candles[-2] = OHLCV(
        candles[-2].time, 2499.82, 2500.10, 2499.65, 2499.90, 100.0
    )
    # This clears the inside candle's high, but remains inside the mother
    # candle. It must not be treated as an inside-bar breakout.
    candles[-1] = OHLCV(
        candles[-1].time, 2499.90, 2500.28, 2499.85, 2500.20, 130.0
    )

    result = analyze_price_action(candles)

    assert result.inside_bar_detected
    assert not result.bullish_inside_breakout
    assert result.pa_signal != "BUY"


def test_nested_inside_bar_breakout_uses_outer_mother_boundary():
    candles = _base_candles()
    candles[-4] = OHLCV(
        candles[-4].time, 2499.60, 2500.50, 2499.30, 2499.90, 100.0
    )
    candles[-3] = OHLCV(
        candles[-3].time, 2499.82, 2500.30, 2499.55, 2500.00, 100.0
    )
    candles[-2] = OHLCV(
        candles[-2].time, 2499.92, 2500.18, 2499.70, 2500.04, 100.0
    )
    candles[-1] = OHLCV(
        candles[-1].time, 2500.04, 2500.72, 2499.98, 2500.62, 140.0
    )

    result = analyze_price_action(candles)

    assert result.bullish_inside_breakout
    assert result.inside_bar_depth == 2
    assert result.inside_bar_breakout_level == 2500.50


def test_extended_inside_bar_breakout_is_marked_for_retest_instead_of_chasing():
    candles = _base_candles()
    candles[-3] = OHLCV(
        candles[-3].time, 2499.70, 2500.40, 2499.40, 2499.85, 100.0
    )
    candles[-2] = OHLCV(
        candles[-2].time, 2499.82, 2500.10, 2499.65, 2499.90, 100.0
    )
    candles[-1] = OHLCV(
        candles[-1].time, 2499.90, 2501.40, 2499.85, 2501.25, 160.0
    )

    result = analyze_price_action(candles)

    assert result.bullish_inside_breakout
    assert result.breakout_level == 2500.40
    assert result.breakout_extension_atr > 0.75
    assert result.breakout_overextended is True


def test_tiny_engulfing_body_is_not_a_directional_signal():
    candles = _base_candles()
    candles[-2] = OHLCV(
        candles[-2].time, 2500.00, 2500.10, 2499.90, 2499.98, 100.0
    )
    candles[-1] = OHLCV(
        candles[-1].time, 2499.98, 2500.10, 2499.90, 2500.01, 100.0
    )

    result = analyze_price_action(candles)

    assert not result.bullish_engulf
    assert result.pa_signal != "BUY"


def test_price_action_fails_closed_on_invalid_volume():
    candles = _base_candles()
    candles[-2] = OHLCV(
        candles[-2].time, 2499.90, 2500.05, 2499.65, 2499.75, 100.0
    )
    candles[-1] = OHLCV(
        candles[-1].time, 2499.74, 2500.15, 2499.35, 2500.05, -1.0
    )

    result = analyze_price_action(candles)

    assert result.pa_signal == "NEUTRAL"
    assert result.pa_score == 0.0


def test_equal_range_bar_is_not_treated_as_an_inside_bar():
    bars = [
        SimpleNamespace(open=100.0, high=101.0, low=99.0, close=100.5),
        SimpleNamespace(open=100.0, high=101.0, low=99.0, close=100.5),
        SimpleNamespace(open=100.5, high=101.8, low=100.4, close=101.6),
    ]

    detected, depth, bullish, bearish, level, strength = (
        _detect_inside_bar_breakout(bars, CFG_M5, atr=1.0)
    )

    assert (detected, depth, bullish, bearish, level, strength) == (
        False, 0, False, False, None, 0.0
    )


def test_small_close_through_level_is_rejected_relative_to_atr():
    def bar(open_, high, low, close):
        return SimpleNamespace(open=open_, high=high, low=low, close=close)

    bars = [bar(99.8, 100.0, 99.7, 99.9), bar(99.98, 100.32, 99.95, 100.28)]
    small = _detect_breakout_pullback(
        bars, [], [100.0], [], [], CFG_M5, atr=3.0
    )
    bars[-1] = bar(100.0, 100.9, 99.95, 100.8)
    large = _detect_breakout_pullback(
        bars, [], [100.0], [], [], CFG_M5, atr=3.0
    )

    assert small[0] is False
    assert large[0] is True


def test_bullish_pullback_needs_a_close_reclaim_above_support():
    bars = [
        SimpleNamespace(open=98.8, high=99.0, low=98.6, close=98.8)
        for _ in range(10)
    ]
    bars.extend(
        [
            SimpleNamespace(open=99.8, high=100.1, low=99.7, close=99.9),
            SimpleNamespace(open=99.6, high=99.9, low=99.5, close=99.8),
        ]
    )

    result = _detect_breakout_pullback(
        bars, [100.0], [], [], [], CFG_M5, atr=1.0
    )

    assert result[4] is False


def test_bullish_pullback_is_valid_after_a_close_reclaims_support():
    bars = [
        SimpleNamespace(open=98.0, high=98.2, low=97.8, close=98.0)
        for _ in range(10)
    ]
    bars.extend(
        [
            SimpleNamespace(open=99.8, high=100.1, low=99.7, close=99.9),
            SimpleNamespace(open=99.9, high=100.5, low=99.85, close=100.3),
        ]
    )

    result = _detect_breakout_pullback(
        bars, [100.0], [], [], [], CFG_M5, atr=1.0
    )

    assert result[4] is True


def test_bullish_pullback_uses_atr_not_fixed_price_percentage():
    def bar(open_, high, low, close):
        return SimpleNamespace(open=open_, high=high, low=low, close=close)

    # The same 2.4-ATR move should be treated consistently at both price
    # scales. At 2500, the former fixed 0.2% rule incorrectly needed a $5 move.
    bars = [bar(2498.9, 2499.1, 2498.7, 2498.9) for _ in range(10)]
    bars.extend(
        [
            bar(2500.0, 2500.05, 2499.8, 2499.9),
            bar(2499.9, 2500.3, 2499.8, 2500.1),
        ]
    )
    result = _detect_breakout_pullback(
        bars, [2500.0], [], [], [], CFG_M5, atr=0.5
    )

    assert result[4] is True


def test_bearish_pullback_needs_a_close_reclaim_below_resistance():
    bars = [
        SimpleNamespace(open=101.2, high=101.4, low=101.0, close=101.2)
        for _ in range(10)
    ]
    bars.extend(
        [
            SimpleNamespace(open=100.2, high=100.3, low=99.9, close=100.1),
            SimpleNamespace(open=100.4, high=100.5, low=100.1, close=100.2),
        ]
    )

    result = _detect_breakout_pullback(
        bars, [], [100.0], [], [], CFG_M5, atr=1.0
    )

    assert result[5] is False


def test_bearish_pullback_uses_atr_normalized_move_at_high_price():
    def bar(open_, high, low, close):
        return SimpleNamespace(open=open_, high=high, low=low, close=close)

    bars = [bar(2501.1, 2501.3, 2500.9, 2501.1) for _ in range(10)]
    bars.extend(
        [
            bar(2500.0, 2500.2, 2499.8, 2500.1),
            bar(2500.1, 2500.2, 2499.6, 2499.75),
        ]
    )
    result = _detect_breakout_pullback(
        bars, [], [2500.0], [], [], CFG_M5, atr=0.5
    )

    assert result[5] is True


def test_sr_touch_clustering_groups_multiple_pivots_across_the_tolerance_band():
    bars = [
        SimpleNamespace(open=97.0, high=99.0, low=95.0, close=97.0)
        for _ in range(30)
    ]
    for index, high in ((5, 100.0), (12, 100.4), (19, 100.8)):
        bars[index] = SimpleNamespace(
            open=97.0, high=high, low=95.0, close=97.0
        )

    support, resistance = _detect_sr_levels(
        bars, CFG_M5, tolerance=0.5
    )

    assert support == []
    assert len(resistance) == 1
    assert abs(resistance[0] - 100.4) < 1e-9


def test_demand_zone_is_removed_after_a_closed_candle_invalidates_it():
    bars = [
        OHLCV(f"t{index}", 100.0, 100.2, 99.8, 100.0, 100.0)
        for index in range(30)
    ]
    for index in (10, 11, 12):
        bars[index] = OHLCV(f"t{index}", 100.0, 100.5, 99.5, 100.0, 100.0)
    bars[13] = OHLCV("t13", 100.5, 102.0, 100.4, 101.8, 120.0)
    bars[20] = OHLCV("t20", 99.4, 99.8, 98.8, 99.0, 120.0)

    demand, _ = _detect_supply_demand(bars, CFG_M5, atr=1.0)

    assert demand == []


def test_fresh_bullish_impulse_creates_a_demand_zone():
    bars = [
        OHLCV(f"t{index}", 100.0, 100.2, 99.8, 100.0, 100.0)
        for index in range(30)
    ]
    for index in (10, 11, 12):
        bars[index] = OHLCV(f"t{index}", 100.0, 100.5, 99.5, 100.0, 100.0)
    bars[13] = OHLCV("t13", 100.5, 102.0, 100.4, 101.8, 120.0)

    demand, _ = _detect_supply_demand(bars, CFG_M5, atr=1.0)

    assert demand
    assert demand[-1] == {"top": 100.5, "bottom": 99.5}


def test_resistance_wick_touch_blocks_unconfirmed_buy_even_when_close_is_far(
    monkeypatch,
):
    candle = OHLCV(
        "2026-09-02T00:00:00+00:00",
        2499.65,
        2500.05,
        2499.30,
        2499.50,
        100.0,
    )

    result = _analyze_with_levels(
        monkeypatch,
        candle,
        resistance_levels=[2500.0],
    )

    assert abs(candle.close - 2500.0) > 0.35
    assert result.near_resistance is True
    assert _entry_location_block_reason("BUY", result) is not None


def test_support_wick_touch_blocks_unconfirmed_sell_even_when_close_is_far(
    monkeypatch,
):
    candle = OHLCV(
        "2026-09-02T00:00:00+00:00",
        2500.35,
        2500.70,
        2499.95,
        2500.50,
        100.0,
    )

    result = _analyze_with_levels(
        monkeypatch,
        candle,
        support_levels=[2500.0],
    )

    assert abs(candle.close - 2500.0) > 0.35
    assert result.near_support is True
    assert _entry_location_block_reason("SELL", result) is not None


def test_wick_touches_supply_and_demand_zones_even_when_close_is_outside(
    monkeypatch,
):
    demand_candle = OHLCV(
        "2026-09-02T00:00:00+00:00",
        2499.65,
        2500.05,
        2499.30,
        2499.50,
        100.0,
    )
    demand_result = _analyze_with_levels(
        monkeypatch,
        demand_candle,
        demand_zones=[{"top": 2500.10, "bottom": 2500.0}],
    )

    supply_candle = OHLCV(
        "2026-09-02T00:00:00+00:00",
        2500.35,
        2500.70,
        2499.95,
        2500.50,
        100.0,
    )
    supply_result = _analyze_with_levels(
        monkeypatch,
        supply_candle,
        supply_zones=[{"top": 2500.0, "bottom": 2499.90}],
    )

    assert abs(demand_candle.close - 2500.0) > 0.35
    assert demand_result.near_demand_zone is True
    assert abs(supply_candle.close - 2500.0) > 0.35
    assert supply_result.near_supply_zone is True

"""Regression tests for the live Price Action signal gate."""

from datetime import datetime, timedelta, timezone

from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.price_action_engine import analyze_price_action


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

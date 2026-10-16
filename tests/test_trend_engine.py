"""Regression tests for the adaptive trend engine."""

from datetime import datetime, timedelta, timezone

from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.trend_engine import analyze_trend


def _candles(closes: list[float]) -> list[OHLCV]:
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return [
        OHLCV(
            time=(start + timedelta(minutes=5 * index)).isoformat(),
            open=close,
            high=close + 0.25,
            low=close - 0.25,
            close=close,
            volume=100.0,
        )
        for index, close in enumerate(closes)
    ]


def test_strong_directional_market_is_bullish_and_trending():
    result = analyze_trend(_candles([2400.0 + index * 0.5 for index in range(260)]))

    assert result.trend == "BULLISH"
    assert result.state == "TRENDING"
    assert result.strength == "STRONG"
    assert result.score > 55
    assert result.adx >= 25


def test_flat_market_is_not_mislabeled_as_a_trend():
    result = analyze_trend(_candles([2500.0] * 260))

    assert result.trend == "NEUTRAL"
    assert result.state == "RANGE"
    assert result.quality < 15.0


def test_shallow_retracement_keeps_the_higher_timeframe_bias():
    closes = [2400.0 + index * 0.45 for index in range(250)]
    last_close = closes[-1]
    closes.extend(last_close - index * 0.20 for index in range(1, 11))
    result = analyze_trend(_candles(closes))

    assert result.trend == "BULLISH"
    assert result.plus_di > result.minus_di
    assert result.pullback or result.state in {"TRENDING", "DEVELOPING"}


def test_bearish_directional_pressure_invalidates_a_stale_bullish_pullback():
    closes = [2400.0 + index * 0.35 for index in range(230)]
    peak = closes[-1]
    closes.extend(peak - index * 0.8 for index in range(1, 12))

    result = analyze_trend(_candles(closes))

    assert result.minus_di > result.plus_di
    assert result.rsi < 35.0
    assert result.trend == "NEUTRAL"
    assert result.state == "TRANSITION"
    assert result.pullback is False


def test_insufficient_history_is_neutral_and_safe():
    result = analyze_trend(_candles([2500.0 + index for index in range(50)]))

    assert result.trend == "NEUTRAL"
    assert result.strength == "WEAK"


def test_non_finite_candle_data_is_neutral_with_finite_scores():
    candles = _candles([2400.0 + index * 0.5 for index in range(260)])
    last = candles[-1]
    candles[-1] = OHLCV(
        last.time,
        float("nan"),
        last.high,
        last.low,
        float("nan"),
        last.volume,
    )

    result = analyze_trend(candles)

    assert result.trend == "NEUTRAL"
    assert result.score == 0.0
    assert result.quality == 0.0
    assert result.ema50 == 0.0


def test_invalid_ohlc_bounds_are_neutral():
    candles = _candles([2400.0 + index * 0.5 for index in range(260)])
    last = candles[-1]
    candles[-1] = OHLCV(
        last.time,
        last.open,
        last.low,
        last.high,
        last.close,
        last.volume,
    )

    result = analyze_trend(candles)

    assert result.trend == "NEUTRAL"
    assert result.score == 0.0

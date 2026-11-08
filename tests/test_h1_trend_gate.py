from datetime import datetime, timedelta, timezone

from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.h1_trend_gate import (
    H1_BIAS_UNAVAILABLE,
    detect_h1_bias,
    evaluate_h1_trend_gate,
)


def _candles(closes):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        OHLCV(
            time=(start + timedelta(hours=index)).isoformat(),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=1,
        )
        for index, close in enumerate(closes)
    ]


def test_detects_bull_bear_and_neutral_biases():
    assert detect_h1_bias(_candles([100.0 + i for i in range(210)])) == "BULL"
    assert detect_h1_bias(_candles([310.0 - i for i in range(210)])) == "BEAR"
    assert detect_h1_bias(_candles([100.0] * 210)) == "NEUTRAL"


def test_insufficient_or_invalid_h1_closes_are_unavailable():
    assert detect_h1_bias(_candles([100.0] * 209)) == H1_BIAS_UNAVAILABLE
    invalid = _candles([100.0] * 210)
    invalid[-1].close = float("nan")
    assert detect_h1_bias(invalid) == H1_BIAS_UNAVAILABLE


def test_counter_strictness_blocks_only_opposing_directions():
    assert evaluate_h1_trend_gate("BUY", "BULL", "counter")[0]
    assert evaluate_h1_trend_gate("SELL", "BEAR", "counter")[0]
    assert not evaluate_h1_trend_gate("BUY", "BEAR", "counter")[0]
    assert not evaluate_h1_trend_gate("SELL", "BULL", "counter")[0]
    assert evaluate_h1_trend_gate("BUY", "NEUTRAL", "counter")[0]
    assert evaluate_h1_trend_gate("SELL", "NEUTRAL", "counter")[0]


def test_aligned_strictness_also_blocks_neutral():
    for direction in ("BUY", "SELL"):
        allowed, reasons = evaluate_h1_trend_gate(
            direction, "NEUTRAL", "aligned"
        )
        assert not allowed
        assert "NEUTRAL" in reasons[0]


def test_unavailable_h1_bias_is_blocking_and_preserves_detail():
    allowed, reasons = evaluate_h1_trend_gate(
        "BUY",
        H1_BIAS_UNAVAILABLE,
        "counter",
        unavailable_reason="Only 180 closed H1 candles",
    )
    assert not allowed
    assert reasons == ["Only 180 closed H1 candles"]

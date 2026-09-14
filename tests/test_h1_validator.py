"""Option 1: H1 data integrity and EMA consistency tests."""
from datetime import datetime, timedelta, timezone

from live_trading.signals.gold_engine import OHLCV, calc_ema
from live_trading.signals.h1_validator import validate_h1_candles


def _candles(count: int = 240) -> list[OHLCV]:
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    result = []
    for index in range(count):
        close = 2300.0 + index * 0.35
        result.append(
            OHLCV(
                time=(start + timedelta(hours=index)).isoformat(),
                open=close - 0.08,
                high=close + 0.12,
                low=close - 0.15,
                close=close,
                volume=1000.0 + index,
            )
        )
    return result


def test_h1_validator_accepts_closed_hourly_data_and_reports_matching_emas():
    candles = _candles()
    result = validate_h1_candles(
        candles,
        now=datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc),
    )

    assert result.valid is True
    closes = [c.close for c in candles]
    assert result.ema50 == calc_ema(closes, 50)
    assert result.ema100 == calc_ema(closes, 100)
    assert result.ema200 == calc_ema(closes, 200)
    assert result.ema_alignment == "BULLISH"


def test_h1_validator_rejects_duplicate_or_sub_hour_candles():
    candles = _candles()
    candles[100] = candles[99]

    result = validate_h1_candles(candles)

    assert result.valid is False
    assert "timestamps" in result.reason


def test_h1_validator_rejects_impossible_ohlc():
    candles = _candles()
    candles[150] = OHLCV(
        time=candles[150].time,
        open=2300.0,
        high=2299.0,
        low=2298.0,
        close=2300.5,
        volume=100.0,
    )

    result = validate_h1_candles(candles)

    assert result.valid is False
    assert "OHLC" in result.reason


def test_h1_validator_rejects_stale_weekday_data():
    candles = _candles()
    result = validate_h1_candles(
        candles,
        now=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc),
    )

    assert result.valid is False
    assert "old" in result.reason
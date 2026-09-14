"""Option 1: only completed H1 candles may feed the HTF EMA calculation."""
from datetime import datetime, timedelta, timezone

from live_trading.mt5.connector import _parse_time
from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.h1_validator import validate_h1_candles


def _candle(at: datetime, close: float = 2300.0) -> OHLCV:
    return OHLCV(
        time=at.isoformat(),
        open=close - 0.1,
        high=close + 0.2,
        low=close - 0.2,
        close=close,
        volume=1000,
    )


def test_h1_validator_rejects_a_current_open_bar():
    now = datetime(2026, 8, 12, 10, 30, tzinfo=timezone.utc)
    start = now - timedelta(hours=209)
    candles = [
        _candle(start + timedelta(hours=index))
        for index in range(210)
    ]

    result = validate_h1_candles(
        candles,
        now=now,
    )

    assert result.valid is False
    assert "still open" in result.reason


def test_candle_timestamp_parser_normalizes_iso_and_unix_seconds():
    expected = datetime(2026, 8, 12, 9, 0, tzinfo=timezone.utc)

    assert _parse_time("2026-08-12T09:00:00Z") == expected
    assert _parse_time(str(expected.timestamp())) == expected
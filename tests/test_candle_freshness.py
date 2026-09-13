"""Regression tests for live closed-candle synchronization."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from live_trading.mt5 import connector


def test_parse_time_accepts_epoch_seconds():
    parsed = connector._parse_time(1789343100)
    assert parsed == datetime.fromtimestamp(1789343100, timezone.utc)


def test_parse_time_accepts_epoch_milliseconds():
    parsed = connector._parse_time("1789343100000")
    assert parsed == datetime.fromtimestamp(1789343100, timezone.utc)


def test_parse_time_rejects_invalid_values_instead_of_fabricating_now():
    assert connector._parse_time("not-a-candle-time") is None


def test_last_completed_bar_is_latest_closed_candle():
    latest = datetime(2026, 9, 14, 3, 5, tzinfo=timezone.utc)
    previous = datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc)

    async def run_check():
        with patch.object(
            connector,
            "fetch_candles",
            new=AsyncMock(
                return_value=[
                    connector.OHLCV(previous, 1, 1, 1, 1, 1),
                    connector.OHLCV(latest, 1, 1, 1, 1, 1),
                ]
            ),
        ):
            return await connector.get_last_completed_bar_time("XAUUSD", "M5")

    assert asyncio.run(run_check()) == latest
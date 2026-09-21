"""Regression tests for live closed-candle synchronization."""

import asyncio
from datetime import datetime, timedelta, timezone
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


def test_fetch_candles_requests_latest_window_without_worker_clock_anchor():
    """History is requested relative to now, not from the start of the day."""
    latest_open = datetime(2026, 9, 14, 3, 10, tzinfo=timezone.utc)
    latest_closed = datetime(2026, 9, 14, 3, 5, tzinfo=timezone.utc)
    history = [
        {
            "time": latest_closed.isoformat(),
            "open": 1,
            "high": 2,
            "low": 0,
            "close": 1.5,
            "tickVolume": 10,
        },
        {
            "time": latest_open.isoformat(),
            "open": 1.5,
            "high": 2.5,
            "low": 1,
            "close": 2,
            "tickVolume": 11,
        },
    ]

    fake_account = type(
        "FakeAccount",
        (),
        {
            "get_historical_candles": AsyncMock(return_value=history)
        },
    )()

    async def run_fetch():
        with (
            patch.object(connector, "is_connected", return_value=True),
            patch.object(connector, "_account", fake_account),
        ):
            return await connector.fetch_candles("XAUUSD", "M5", count=1)

    result = asyncio.run(run_fetch())
    assert [c.time for c in result] == [latest_closed]
    call = fake_account.get_historical_candles.await_args.kwargs
    assert call["symbol"] == "XAUUSD"
    assert call["timeframe"] == "5m"
    assert call["limit"] == 205
    assert isinstance(call["start_time"], datetime)
    now = datetime.now(timezone.utc)
    assert now - timedelta(minutes=1030) < call["start_time"]
    assert call["start_time"] < now - timedelta(minutes=1020)
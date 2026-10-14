"""Regression tests for suppressing already-closed bars after a restart."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from live_trading.trading import live_loop
from live_trading.trading.live_loop import GoldScalperLive


@pytest.mark.asyncio
async def test_restart_baselines_latest_m5_bar_then_processes_only_the_next_bar(
    monkeypatch,
):
    latest_bar = datetime.now(timezone.utc).replace(
        second=0, microsecond=0
    ) - timedelta(minutes=5)
    next_bar = latest_bar + timedelta(minutes=5)

    monkeypatch.setattr(live_loop, "TRADE_TIMEFRAMES", ["5m"])
    monkeypatch.setattr(live_loop, "MTF_ENABLED", False)
    monkeypatch.setattr(live_loop, "SL_ATR_TIMEFRAME", "5m")
    monkeypatch.setattr(
        live_loop,
        "get_last_completed_bar_time",
        AsyncMock(side_effect=[latest_bar, latest_bar, next_bar]),
    )
    robot = GoldScalperLive()

    assert await robot._check_new_bars() == []
    assert await robot._check_new_bars() == []
    assert await robot._check_new_bars() == [
        ("5m", next_bar.replace(tzinfo=None))
    ]

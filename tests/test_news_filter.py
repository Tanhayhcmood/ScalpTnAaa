import asyncio
from datetime import datetime, timezone

import pytest

from live_trading.signals import news_filter


@pytest.mark.parametrize(
    ("now", "blocked"),
    [
        (datetime(2026, 11, 4, 11, 45, tzinfo=timezone.utc), True),
        (datetime(2026, 11, 4, 12, 30, tzinfo=timezone.utc), True),
        (datetime(2026, 11, 4, 11, 44, 59, tzinfo=timezone.utc), False),
        (datetime(2026, 11, 4, 12, 30, 1, tzinfo=timezone.utc), False),
    ],
)
def test_news_window_edges_are_inclusive(monkeypatch, now, blocked):
    event = {
        "country": "USD",
        "impact": "High",
        "title": "CPI",
        "date": "2026-11-04",
        "time": "12:00pm",
    }

    async def fake_events():
        return [event]

    monkeypatch.setattr(news_filter, "_get_events", fake_events)
    monkeypatch.setattr(news_filter, "_BLOCK_BEFORE_S", 15 * 60)
    monkeypatch.setattr(news_filter, "_BLOCK_AFTER_S", 30 * 60)

    result = asyncio.run(news_filter.check_news_filter(now))

    assert result.blocked is blocked
    if blocked:
        assert result.event_name == "CPI"


def test_news_feed_error_fails_open(monkeypatch):
    async def failed_events():
        raise RuntimeError("feed unavailable")

    monkeypatch.setattr(news_filter, "_get_events", failed_events)

    result = asyncio.run(
        news_filter.check_news_filter(
            datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
        )
    )

    assert result.blocked is False

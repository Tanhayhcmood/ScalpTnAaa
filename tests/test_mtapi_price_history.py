"""Regression tests for the hosted MTAPI PriceHistory candle contract."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from live_trading.mt5 import connector
from live_trading.trading import live_loop


class _FakeResponse:
    def __init__(self, status: int, body: object):
        self.status = status
        self._body = body if isinstance(body, str) else json.dumps(body)

    async def text(self) -> str:
        return self._body


class _FakeRequest:
    def __init__(self, response: _FakeResponse):
        self.response = response

    async def __aenter__(self) -> _FakeResponse:
        return self.response

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        return False


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict, object]] = []

    def get(self, url: str, *, params: dict, timeout: object) -> _FakeRequest:
        self.calls.append((url, dict(params), timeout))
        return _FakeRequest(self.responses.pop(0))


class _FrozenDateTime(datetime):
    fixed_now = datetime(2026, 10, 5, 12, 12, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.fixed_now.replace(tzinfo=None)
        return cls.fixed_now.astimezone(tz)


def _bar(at: datetime, offset: int) -> dict:
    return {
        "time": at.isoformat().replace("+00:00", "Z"),
        "openPrice": 2300.0 + offset,
        "highPrice": 2302.0 + offset,
        "lowPrice": 2299.0 + offset,
        "closePrice": 2301.0 + offset,
        "tickVolume": 20 + offset,
        "volume": 10 + offset,
    }


def _fetch_and_get_latest(session: _FakeSession):
    async def run():
        with (
            patch.object(connector, "_conn_id", "session-token-do-not-log"),
            patch.object(connector, "_base_url", "https://mt5.mtapi.io"),
            patch.object(connector, "datetime", _FrozenDateTime),
            patch.object(connector, "_get_session", return_value=session),
            patch.object(connector, "log", MagicMock()),
        ):
            candles = await connector.fetch_candles("XAUUSD", "M5", count=3)
            latest = await connector.get_last_completed_bar_time("XAUUSD", "M5")
            return candles, latest

    return asyncio.run(run())


def test_price_history_fetch_parses_closed_bars_and_advances_bar_time():
    now = _FrozenDateTime.fixed_now
    older = now.replace(minute=0)
    latest_closed = older + timedelta(minutes=5)
    forming = older + timedelta(minutes=10)
    response_body = [
        _bar(forming, 2),
        _bar(latest_closed, 1),
        _bar(older, 0),
    ]
    session = _FakeSession(
        [_FakeResponse(200, response_body), _FakeResponse(200, response_body)]
    )

    candles, latest_time = _fetch_and_get_latest(session)

    assert [c.time for c in candles] == [
        "2026-10-05T12:00:00Z",
        "2026-10-05T12:05:00Z",
    ]
    assert (candles[-1].open, candles[-1].high, candles[-1].low, candles[-1].close) == (
        2301.0,
        2303.0,
        2300.0,
        2302.0,
    )
    assert candles[-1].volume == 21.0
    assert latest_time == latest_closed

    url, params, _ = session.calls[0]
    assert url == "https://mt5.mtapi.io/PriceHistory"
    assert params["id"] == "session-token-do-not-log"
    assert params["symbol"] == "XAUUSD"
    assert params["timeFrame"] == 5
    assert type(params["timeFrame"]) is int
    assert params["from"].endswith("Z")
    assert params["to"] == "2026-10-05T12:12:00Z"
    assert all(call[0].endswith("/PriceHistory") for call in session.calls)


def test_price_history_falls_back_for_old_bridge_and_redacts_session_id():
    now = _FrozenDateTime.fixed_now
    latest_closed = now.replace(minute=5)
    body = [_bar(latest_closed, 0)]
    session_id = "session-token-do-not-log"
    session = _FakeSession([
        _FakeResponse(404, {"message": f"no route for id={session_id}"}),
        _FakeResponse(200, body),
    ])
    test_logger = MagicMock()

    async def run():
        with (
            patch.object(connector, "_conn_id", session_id),
            patch.object(connector, "_base_url", "https://old-bridge.invalid"),
            patch.object(connector, "datetime", _FrozenDateTime),
            patch.object(connector, "_get_session", return_value=session),
            patch.object(connector, "log", test_logger),
        ):
            return await connector.fetch_candles("XAUUSD", "M5", count=1)

    candles = asyncio.run(run())

    assert len(candles) == 1
    assert [call[0] for call in session.calls] == [
        "https://old-bridge.invalid/PriceHistory",
        "https://old-bridge.invalid/PriceHistoryV2",
    ]
    logged = " ".join(
        str(arg)
        for call in test_logger.warning.call_args_list
        for arg in call.args
    )
    assert "404" in logged
    assert "/PriceHistory" in logged
    assert "no route" in logged
    assert "[REDACTED]" in logged
    assert session_id not in logged


def test_live_loop_emits_one_new_bar_when_latest_closed_time_advances():
    current_bucket = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    current_bucket = current_bucket.replace(
        minute=(current_bucket.minute // 5) * 5
    )
    previous_bar = current_bucket - timedelta(minutes=5)
    loop = object.__new__(live_loop.GoldScalperLive)
    loop._last_bar_times = {"5m": None}
    loop._latest_completed_bar_times = {}
    get_bar_time = AsyncMock(
        side_effect=[previous_bar, previous_bar, current_bucket]
    )

    async def run():
        with (
            patch.object(live_loop, "TRADE_TIMEFRAMES", ["5m"]),
            patch.object(live_loop, "MTF_ENABLED", False),
            patch.object(live_loop, "SL_ATR_TIMEFRAME", "5m"),
            patch.object(live_loop, "get_last_completed_bar_time", get_bar_time),
        ):
            first = await loop._check_new_bars()
            unchanged = await loop._check_new_bars()
            advanced = await loop._check_new_bars()
            return first, unchanged, advanced

    first, unchanged, advanced = asyncio.run(run())

    assert first == [("5m", previous_bar.replace(tzinfo=None))]
    assert unchanged == []
    assert advanced == [("5m", current_bucket.replace(tzinfo=None))]
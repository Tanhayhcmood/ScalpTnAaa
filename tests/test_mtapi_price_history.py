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

    async def json(self, content_type=None) -> object:
        return json.loads(self._body)


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
        quote = {
            "bid": 2300.0,
            "ask": 2300.1,
            "server_time_raw": _FrozenDateTime.fixed_now.isoformat().replace(
                "+00:00", "Z"
            ),
        }
        with (
            patch.object(connector, "_conn_id", "session-token-do-not-log"),
            patch.object(connector, "_base_url", "https://mt5.mtapi.io"),
            patch.object(connector, "datetime", _FrozenDateTime),
            patch.object(connector, "_get_session", return_value=session),
            patch.object(
                connector, "get_current_quote", AsyncMock(return_value=quote)
            ),
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
    assert params["from"] == "2026-10-05T10:12:00"
    assert params["to"] == "2026-10-05T12:12:00"
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
        quote = {
            "bid": 2300.0,
            "ask": 2300.1,
            "server_time_raw": _FrozenDateTime.fixed_now.isoformat().replace(
                "+00:00", "Z"
            ),
        }
        with (
            patch.object(connector, "_conn_id", session_id),
            patch.object(connector, "_base_url", "https://old-bridge.invalid"),
            patch.object(connector, "datetime", _FrozenDateTime),
            patch.object(connector, "_get_session", return_value=session),
            patch.object(
                connector, "get_current_quote", AsyncMock(return_value=quote)
            ),
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


def test_empty_price_history_falls_back_to_today_then_month_and_parses_aliases():
    now = _FrozenDateTime.fixed_now
    body = [
        {
            "Time": "2026-10-05T11:55:00Z",
            "Open": 1.0,
            "High": 2.0,
            "Low": 0.5,
            "Close": 1.5,
            "Volume": 7,
        },
        {
            "openTime": "2026-10-05T12:00:00Z",
            "o": 2.0,
            "h": 3.0,
            "l": 1.5,
            "c": 2.5,
            "tickVolume": 9,
        },
        {
            "time": "2026-10-05T12:10:00Z",
            "openPrice": 3.0,
            "highPrice": 4.0,
            "lowPrice": 2.5,
            "closePrice": 3.5,
        },
    ]
    session_id = "session-token-do-not-log"
    session = _FakeSession([
        _FakeResponse(200, []),
        _FakeResponse(200, []),
        _FakeResponse(200, body),
    ])
    test_logger = MagicMock()
    quote = {
        "bid": 2300.0,
        "ask": 2300.1,
        "server_time_raw": now.isoformat().replace("+00:00", "Z"),
    }

    async def run():
        with (
            patch.object(connector, "_conn_id", session_id),
            patch.object(connector, "_base_url", "https://mt5.mtapi.io"),
            patch.object(connector, "datetime", _FrozenDateTime),
            patch.object(connector, "_get_session", return_value=session),
            patch.object(
                connector, "get_current_quote", AsyncMock(return_value=quote)
            ),
            patch.object(connector, "log", test_logger),
        ):
            return await connector.fetch_candles("XAUUSD", "M5", count=1)

    candles = asyncio.run(run())

    assert len(candles) == 1
    assert candles[0].time == "2026-10-05T12:00:00Z"
    assert (candles[0].open, candles[0].high, candles[0].low, candles[0].close) == (
        2.0,
        3.0,
        1.5,
        2.5,
    )
    assert [call[0].rsplit("/", 1)[-1] for call in session.calls] == [
        "PriceHistory",
        "PriceHistoryToday",
        "PriceHistoryMonth",
    ]
    primary_params = session.calls[0][1]
    assert primary_params["from"] == "2026-10-05T10:12:00"
    assert primary_params["to"] == "2026-10-05T12:12:00"
    assert type(primary_params["timeFrame"]) is int
    month_params = session.calls[2][1]
    assert (month_params["year"], month_params["month"], month_params["day"]) == (
        2026,
        10,
        5,
    )
    assert "from" not in month_params and "to" not in month_params
    logged = " ".join(
        str(arg)
        for calls in (
            test_logger.info.call_args_list,
            test_logger.warning.call_args_list,
            test_logger.error.call_args_list,
        )
        for call in calls
        for arg in call.args
    )
    assert session_id not in logged
    assert "history succeeded" in logged
    assert "PriceHistoryMonth" in logged


def test_calibration_and_polling_use_bounded_history_ranges():
    assert connector._candle_history_minutes(5, 500) == 5 * 24 * 60
    assert connector._candle_history_minutes(5, 3) == 2 * 60


def test_resolve_symbol_name_uses_symbols_map_and_unique_suffix():
    session = _FakeSession([
        _FakeResponse(200, {"XAUUSD.a": {}, "EURUSD": {}}),
    ])

    async def run():
        with (
            patch.object(connector, "_conn_id", "session-token-do-not-log"),
            patch.object(connector, "_base_url", "https://mt5.mtapi.io"),
            patch.object(connector, "_get_session", return_value=session),
            patch.object(connector, "log", MagicMock()),
        ):
            return await connector.resolve_symbol_name("XAUUSD")

    assert asyncio.run(run()) == "XAUUSD.a"
    assert session.calls[0][0] == "https://mt5.mtapi.io/Symbols"


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
"""Regression tests for MTAPI SymbolParams parsing and tick-size fallbacks."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from live_trading.mt5 import connector
from live_trading.risk.aggregate_risk import build_symbol_risk_spec


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


def _symbol_info(**fields) -> dict:
    return {
        "digits": 2,
        "points": 0.01,
        "tickSize": 0.01,
        "contractSize": 100.0,
        "currencyProfit": "USD",
        **fields,
    }


def _run_with_session(session: _FakeSession, logger=None):
    async def run():
        with (
            patch.object(connector, "_conn_id", "session-token-do-not-log"),
            patch.object(connector, "_base_url", "https://mt5.mtapi.io"),
            patch.object(connector, "_get_session", return_value=session),
            patch.object(
                connector, "ensure_connected", AsyncMock(return_value=True)
            ),
            patch.object(connector.asyncio, "sleep", AsyncMock()),
            patch.object(connector, "_SYMBOL_TICK_SIZE_CACHE", {}),
            patch.object(connector, "XAUUSD_TICK_SIZE", 0.01),
            patch.object(connector, "log", logger or MagicMock()),
        ):
            return await connector.get_symbol_params("XAUUSD")

    return asyncio.run(run())


def test_symbol_params_uses_documented_request_and_camel_case_fields():
    session = _FakeSession([
        _FakeResponse(200, {
            "symbol": "XAUUSD",
            "symbolInfo": _symbol_info(tickSize=0.05),
            "symbolGroup": {"sl": 10, "tp": 10},
        })
    ])

    params = _run_with_session(session)

    assert params["_symbol_params_fetch_ok"] is True
    assert params["_tick_size_source"] == "broker"
    assert params["symbolInfo"]["tickSize"] == 0.05
    assert params["symbolInfo"]["digits"] == 2
    assert params["symbolInfo"]["points"] == 0.01
    assert params["symbolInfo"]["contractSize"] == 100.0
    url, request_params, _ = session.calls[0]
    assert url == "https://mt5.mtapi.io/SymbolParams"
    assert request_params == {
        "id": "session-token-do-not-log",
        "symbol": "XAUUSD",
    }


def test_symbol_params_normalizes_wrapped_snake_case_response():
    params = connector._normalize_symbol_params(
        {
            "data": {
                "symbol": "XAUUSD",
                "info": {
                    "tick_size": "0.05",
                    "precision": "2",
                    "point_size": "0.01",
                    "contract_size": "100",
                    "profit_currency": "USD",
                },
            }
        },
        "XAUUSD",
    )

    assert params["symbol"] == "XAUUSD"
    assert params["symbolInfo"]["tickSize"] == "0.05"
    assert params["symbolInfo"]["digits"] == "2"
    assert params["symbolInfo"]["points"] == "0.01"
    assert params["symbolInfo"]["contractSize"] == "100"
    assert params["symbolInfo"]["currencyProfit"] == "USD"


def test_raw_zero_sizing_fields_survive_canonical_tick_size_fallback():
    params = connector._normalize_symbol_params(
        {
            "symbol": "XAUUSD",
            "symbolInfo": {
                "tickSize": 0,
                "tickValue": 0,
                "contractSize": 100,
                "volumeMin": 0.1,
                "volumeStep": 0.1,
                "volumeMax": 50,
            },
        },
        "XAUUSD",
    )
    loaded = connector._with_tick_size(
        params,
        "XAUUSD",
        0.01,
        "env:XAUUSD_TICK_SIZE",
        fetched_from_broker=False,
    )

    assert loaded["symbolInfo"]["tickSize"] == 0.01
    assert loaded["_raw_symbol_fields"]["symbolInfo"] == {
        "tickSize": 0,
        "tickValue": 0,
        "contractSize": 100,
        "volumeMin": 0.1,
        "volumeStep": 0.1,
        "volumeMax": 50,
    }


def test_mtapi_201_exception_is_logged_retried_and_uses_explicit_default():
    error_body = {
        "code": "SYMBOL_PARAMS_FAILED",
        "message": "upstream symbol metadata unavailable",
        "stackTrace": "diagnostic traceback",
    }
    session = _FakeSession([
        _FakeResponse(201, error_body),
        _FakeResponse(201, error_body),
        _FakeResponse(201, error_body),
    ])
    logger = MagicMock()

    params = _run_with_session(session, logger)

    assert params["_symbol_params_fetch_ok"] is False
    assert params["_tick_size_source"] == "env:XAUUSD_TICK_SIZE"
    assert params["symbolInfo"]["tickSize"] == 0.01
    assert params["symbolInfo"]["points"] == 0.01
    assert params["symbolInfo"]["digits"] == 2
    spec = build_symbol_risk_spec(
        {
            **params,
            "symbolInfo": {
                **params["symbolInfo"],
                "contractSize": 100.0,
                "currencyProfit": "USD",
            },
        },
        "USD",
    )
    assert spec.tick_size == 0.01
    assert len(session.calls) == 3

    logged = " ".join(
        str(arg)
        for calls in (logger.warning.call_args_list, logger.error.call_args_list)
        for call in calls
        for arg in call.args
    )
    assert "status=%s" in logged
    assert "201" in logged
    assert "upstream symbol metadata unavailable" in logged
    assert "fallback=configured_env" in logged
    assert "session-token-do-not-log" not in logged


def test_cached_broker_tick_size_is_used_before_retrying_bad_metadata():
    session = _FakeSession([
        _FakeResponse(200, {
            "symbol": "XAUUSD",
            "symbolInfo": _symbol_info(tickSize=0.05),
        }),
        _FakeResponse(200, {
            "symbol": "XAUUSD",
            "symbolInfo": {
                "digits": 2,
                "points": 0.01,
                "contractSize": 100.0,
                "currencyProfit": "USD",
            },
        }),
    ])
    logger = MagicMock()

    async def run():
        cache = {}
        with (
            patch.object(connector, "_conn_id", "session-token-do-not-log"),
            patch.object(connector, "_base_url", "https://mt5.mtapi.io"),
            patch.object(connector, "_get_session", return_value=session),
            patch.object(
                connector, "ensure_connected", AsyncMock(return_value=True)
            ),
            patch.object(connector.asyncio, "sleep", AsyncMock()),
            patch.object(connector, "_SYMBOL_TICK_SIZE_CACHE", cache),
            patch.object(connector, "XAUUSD_TICK_SIZE", 0.01),
            patch.object(connector, "log", logger),
        ):
            first = await connector.get_symbol_params("XAUUSD")
            second = await connector.get_symbol_params("XAUUSD")
            return first, second

    first, second = asyncio.run(run())

    assert first["symbolInfo"]["tickSize"] == 0.05
    assert second["symbolInfo"]["tickSize"] == 0.05
    assert second["_tick_size_source"] == "cache"
    assert second["symbolInfo"]["contractSize"] == 100.0
    assert len(session.calls) == 2
    assert "fallback=cache" in " ".join(
        str(arg)
        for call in logger.warning.call_args_list
        for arg in call.args
    )


def test_missing_tick_size_retries_shortly_before_accepting_a_later_response():
    session = _FakeSession([
        _FakeResponse(200, {"symbol": "XAUUSD", "symbolInfo": {"digits": 2}}),
        _FakeResponse(200, {"symbol": "XAUUSD", "symbolInfo": {"digits": 2}}),
        _FakeResponse(200, {
            "symbol": "XAUUSD",
            "symbolInfo": _symbol_info(tickSize=0.05),
        }),
    ])
    sleep = AsyncMock()

    async def run():
        with (
            patch.object(connector, "_conn_id", "session-token-do-not-log"),
            patch.object(connector, "_base_url", "https://mt5.mtapi.io"),
            patch.object(connector, "_get_session", return_value=session),
            patch.object(
                connector, "ensure_connected", AsyncMock(return_value=True)
            ),
            patch.object(connector.asyncio, "sleep", sleep),
            patch.object(connector, "_SYMBOL_TICK_SIZE_CACHE", {}),
            patch.object(connector, "log", MagicMock()),
        ):
            return await connector.get_symbol_params("XAUUSD")

    params = asyncio.run(run())

    assert params["_tick_size_source"] == "broker"
    assert params["symbolInfo"]["tickSize"] == 0.05
    assert len(session.calls) == 3
    assert sleep.await_count == 2

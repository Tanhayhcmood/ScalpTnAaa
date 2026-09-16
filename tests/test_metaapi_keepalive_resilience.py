"""Regression tests for transient MetaAPI health-check failures."""

import asyncio

from live_trading.mt5 import connector


class _Connection:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls = 0

    async def get_account_information(self):
        self.calls += 1
        if self.error:
            raise self.error
        return {"balance": 375.72}

    async def get_positions(self):
        raise TimeoutError("temporary position timeout")


def test_single_health_check_failure_keeps_session_connected():
    async def run():
        connection = _Connection(TimeoutError("temporary timeout"))
        connector._connection = connection
        connector._connected = True
        connector._consecutive_health_failures = 0

        assert await connector.keepalive_metaapi() is False
        assert connector._connected is True
        assert connector._consecutive_health_failures == 1

    try:
        asyncio.run(run())
    finally:
        connector._connection = None
        connector._connected = False
        connector._consecutive_health_failures = 0


def test_three_consecutive_health_check_failures_mark_session_disconnected():
    async def run():
        connection = _Connection(TimeoutError("broker timeout"))
        connector._connection = connection
        connector._connected = True
        connector._consecutive_health_failures = 0

        for _ in range(2):
            assert await connector.keepalive_metaapi() is False
            assert connector._connected is True

        assert await connector.keepalive_metaapi() is False
        assert connector._connected is False
        assert connector._consecutive_health_failures == 3

    try:
        asyncio.run(run())
    finally:
        connector._connection = None
        connector._connected = False
        connector._consecutive_health_failures = 0


def test_successful_health_check_resets_failure_streak():
    async def run():
        connection = _Connection()
        connector._connection = connection
        connector._connected = True
        connector._consecutive_health_failures = 2

        assert await connector.keepalive_metaapi() is True
        assert connector._connected is True
        assert connector._consecutive_health_failures == 0

    try:
        asyncio.run(run())
    finally:
        connector._connection = None
        connector._connected = False
        connector._consecutive_health_failures = 0


def test_position_timeout_does_not_replace_session_by_itself():
    async def run():
        connection = _Connection()
        connector._connection = connection
        connector._connected = True

        try:
            await connector.get_open_positions("XAUUSD")
        except RuntimeError as exc:
            assert "timed out" in str(exc).lower()
        else:
            raise AssertionError("position timeout did not surface")

        assert connector._connected is True

    try:
        asyncio.run(run())
    finally:
        connector._connection = None
        connector._connected = False
        connector._consecutive_health_failures = 0


def test_historical_timeout_does_not_replace_healthy_session():
    async def run():
        class _Account:
            async def get_historical_candles(self, **_kwargs):
                raise TimeoutError("temporary historical timeout")

        connector._account = _Account()
        connector._connection = object()
        connector._connected = True
        connector._consecutive_health_failures = 0

        candles = await connector.fetch_candles("XAUUSD", "1m", count=50)

        assert candles == []
        assert connector._connected is True
        assert connector._consecutive_health_failures == 0

    try:
        asyncio.run(run())
    finally:
        connector._account = None
        connector._connection = None
        connector._connected = False
        connector._consecutive_health_failures = 0

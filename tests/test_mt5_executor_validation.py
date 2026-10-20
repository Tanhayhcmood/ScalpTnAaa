import asyncio
from unittest.mock import MagicMock, patch

from live_trading.mt5 import executor
from live_trading.mt5.executor import _normalise_lot


def test_normalise_lot_rejects_non_finite_or_non_positive_values():
    for value in (0, -0.01, float("nan"), float("inf")):
        try:
            _normalise_lot(value, 0.01, 0.01, 100.0)
        except ValueError:
            continue
        raise AssertionError(f"invalid lot value was accepted: {value!r}")


def test_normalise_lot_keeps_broker_step():
    assert _normalise_lot(0.014, 0.01, 0.01, 100.0) == 0.01
    assert _normalise_lot(0.016, 0.01, 0.01, 100.0) == 0.01
    assert _normalise_lot(0.026, 0.01, 0.01, 100.0) == 0.02


class _FakeResponse:
    status = 200

    async def json(self, content_type=None):
        return {"ticket": 123}


class _FakeRequest:
    async def __aenter__(self):
        return _FakeResponse()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _FakeSession:
    def __init__(self):
        self.url = None
        self.params = None

    def get(self, url, *, params, timeout):
        self.url = url
        self.params = dict(params)
        return _FakeRequest()


def test_order_send_passes_standard_lots_without_extended_volume_scaling():
    session = _FakeSession()

    async def run():
        with (
            patch.object(
                executor, "get_connection", return_value="https://mt5.mtapi.io"
            ),
            patch.object(executor, "get_conn_id", return_value="test-session"),
            patch.object(executor, "_get_session", return_value=session),
            patch.object(executor, "log", MagicMock()),
        ):
            result = await executor.place_market_order(
                symbol="XAUUSD",
                direction="BUY",
                lot_size=0.05,
                sl=2300.0,
                tp=2310.0,
                volume_min=0.01,
                volume_step=0.01,
                volume_max=100.0,
            )
        return result

    result = asyncio.run(run())

    assert result.success
    assert session.url == "https://mt5.mtapi.io/OrderSendSafe"
    assert session.params["volume"] == 0.05


def test_order_send_blocks_when_volume_constraints_are_missing():
    async def run():
        with (
            patch.object(
                executor, "get_connection", return_value="https://mt5.mtapi.io"
            ),
            patch.object(executor, "get_conn_id", return_value="test-session"),
        ):
            return await executor.place_market_order(
                symbol="XAUUSD",
                direction="BUY",
                lot_size=0.05,
                sl=2300.0,
                tp=2310.0,
            )

    result = asyncio.run(run())

    assert not result.success
    assert "refusing to guess lot limits" in result.message
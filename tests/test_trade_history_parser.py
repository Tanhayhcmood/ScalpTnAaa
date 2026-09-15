"""Regression tests for defensive MetaAPI close-history parsing."""

from datetime import datetime, timezone

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_deals",
    [
        ["not-a-deal", {"entryType": "DEAL_ENTRY_OUT", "price": 99.5, "time": "2026-09-15T00:00:00Z"}],
        {"deals": [{"entryType": "DEAL_ENTRY_OUT", "price": 99.5, "time": "2026-09-15T00:00:00Z"}]},
        {"entryType": "DEAL_ENTRY_OUT", "price": 99.5, "time": "2026-09-15T00:00:00Z"},
    ],
)
async def test_close_history_ignores_malformed_entries_and_accepts_wrappers(
    monkeypatch, raw_deals
):
    from live_trading.mt5 import connector

    class FakeConnection:
        async def get_deals_by_position(self, _position_id):
            return raw_deals

    monkeypatch.setattr(connector, "_connection", FakeConnection())
    monkeypatch.setattr(connector, "is_connected", lambda: True)

    result = await connector.get_closed_position_history("ticket-1")

    assert result["closePrice"] == 99.5
    assert result["closeTime"] == "2026-09-15T00:00:00Z"


def test_history_records_group_partial_closes_by_position():
    from live_trading.trading.live_loop import GoldScalperLive

    records = GoldScalperLive._history_records_from_deals(
        [
            {
                "id": "in-1",
                "positionId": "position-1",
                "symbol": "XAUUSD",
                "entryType": "DEAL_ENTRY_IN",
                "type": "DEAL_TYPE_BUY",
                "volume": 0.02,
                "price": 2350.0,
                "time": "2026-09-15T10:00:00Z",
            },
            {
                "id": "out-1",
                "positionId": "position-1",
                "symbol": "XAUUSD",
                "entryType": "DEAL_ENTRY_OUT",
                "type": "DEAL_TYPE_SELL",
                "volume": 0.01,
                "price": 2354.0,
                "profit": 4.0,
                "commission": -0.2,
                "time": "2026-09-15T10:15:00Z",
            },
            {
                "id": "out-2",
                "positionId": "position-1",
                "symbol": "XAUUSD",
                "entryType": "DEAL_ENTRY_OUT",
                "type": "DEAL_TYPE_SELL",
                "volume": 0.01,
                "price": 2356.0,
                "profit": 6.0,
                "commission": -0.2,
                "time": "2026-09-15T10:20:00Z",
            },
        ]
    )

    assert len(records) == 1
    assert records[0]["position_id"] == "position-1"
    assert records[0]["direction"] == "BUY"
    assert records[0]["status"] == "CLOSED"
    assert records[0]["profit_gross"] == 10.0
    assert records[0]["commission"] == -0.4
    assert records[0]["profit"] == 9.6


@pytest.mark.asyncio
async def test_deals_by_time_range_uses_metaapi_rpc_and_normalizes_wrappers(
    monkeypatch,
):
    from live_trading.mt5 import connector

    class FakeConnection:
        async def get_deals_by_time_range(self, *, start_time, end_time):
            assert start_time.tzinfo == timezone.utc
            assert end_time.tzinfo == timezone.utc
            return {
                "deals": [
                    {"positionId": "1", "entryType": "DEAL_ENTRY_IN"},
                    "malformed",
                ]
            }

    monkeypatch.setattr(connector, "_connection", FakeConnection())
    monkeypatch.setattr(connector, "is_connected", lambda: True)

    result = await connector.get_deals_by_time_range(
        datetime(2026, 9, 15, tzinfo=timezone.utc),
        datetime(2026, 9, 16, tzinfo=timezone.utc),
    )

    assert result == [{"positionId": "1", "entryType": "DEAL_ENTRY_IN"}]


@pytest.mark.asyncio
async def test_history_sync_merges_broker_record_without_erasing_strategy_data(
    monkeypatch, tmp_path
):
    from live_trading.trading import live_loop

    robot = live_loop.GoldScalperLive()
    robot.trade_history = [
        {
            "position_id": "position-1",
            "confidence": 82.0,
            "strategy_slots": ["price_action"],
            "status": "OPEN",
        }
    ]
    async def fake_get_deals_by_time_range(_start, _end):
        return _closed_deal_batch()

    monkeypatch.setattr(
        live_loop, "get_deals_by_time_range", fake_get_deals_by_time_range
    )
    monkeypatch.setattr(robot, "_write_state", lambda *args, **kwargs: None)

    await robot._sync_broker_trade_history(force=True)

    assert len(robot.trade_history) == 1
    assert robot.trade_history[0]["confidence"] == 82.0
    assert robot.trade_history[0]["strategy_slots"] == ["price_action"]
    assert robot.trade_history[0]["status"] == "CLOSED"
    assert robot._history_sync_status["status"] == "SYNCED"
    assert robot._history_sync_status["closed_positions"] == 1


def _closed_deal_batch():
    return [
        {
            "positionId": "position-1",
            "symbol": "XAUUSD",
            "entryType": "DEAL_ENTRY_IN",
            "type": "DEAL_TYPE_BUY",
            "volume": 0.01,
            "price": 2350.0,
            "time": "2026-09-15T10:00:00Z",
        },
        {
            "positionId": "position-1",
            "symbol": "XAUUSD",
            "entryType": "DEAL_ENTRY_OUT",
            "type": "DEAL_TYPE_SELL",
            "volume": 0.01,
            "price": 2355.0,
            "profit": 5.0,
            "time": "2026-09-15T10:15:00Z",
            "comment": "take profit",
        },
    ]
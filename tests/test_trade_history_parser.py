"""Regression tests for defensive MetaAPI close-history parsing."""

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
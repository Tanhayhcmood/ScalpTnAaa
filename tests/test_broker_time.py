from datetime import datetime, timezone

from live_trading.utils import broker_time


def _set_auto(monkeypatch):
    monkeypatch.setattr(broker_time, "BROKER_UTC_OFFSET_HOURS", "auto")
    monkeypatch.setattr(broker_time, "_last_good_offset_hours", None)


def test_broker_time_to_utc_uses_winter_offset(monkeypatch):
    _set_auto(monkeypatch)
    now = datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc)

    assert broker_time.observe_broker_server_time(
        datetime(2026, 1, 15, 20, 0), now=now
    ) == 2.0
    assert broker_time.broker_time_to_utc(
        datetime(2026, 1, 15, 20, 0)
    ) == now


def test_broker_time_to_utc_uses_summer_offset(monkeypatch):
    _set_auto(monkeypatch)
    now = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)

    assert broker_time.observe_broker_server_time(
        datetime(2026, 7, 15, 21, 0), now=now
    ) == 3.0
    assert broker_time.broker_time_to_utc(
        datetime(2026, 7, 15, 21, 0)
    ) == now


def test_stale_quote_reuses_last_good_offset(monkeypatch):
    _set_auto(monkeypatch)
    first_now = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)
    stale_now = datetime(2026, 7, 16, 18, 0, tzinfo=timezone.utc)

    broker_time.observe_broker_server_time(
        datetime(2026, 7, 15, 21, 0), now=first_now
    )
    offset = broker_time.observe_broker_server_time(
        datetime(2026, 7, 15, 21, 0), now=stale_now
    )

    assert offset == 3.0
    assert broker_time.broker_time_to_utc(
        datetime(2026, 7, 16, 21, 0)
    ) == stale_now

from datetime import datetime, timezone

from live_trading.signals.quality_filter import get_session_quality


def test_every_valid_utc_hour_is_tradeable():
    """Session quality must never hard-block a valid UTC hour."""
    for hour in range(24):
        timestamp = datetime(2026, 9, 14, hour, 30, tzinfo=timezone.utc)
        assert get_session_quality(timestamp.isoformat()) in {"PRIME", "MODERATE"}


def test_malformed_timestamp_stays_fail_closed():
    assert get_session_quality("not-a-timestamp") == "BLOCKED"

def test_datetime_object_is_accepted():
    timestamp = datetime(2026, 9, 14, 3, 45, tzinfo=timezone.utc)
    assert get_session_quality(timestamp) in {"PRIME", "MODERATE"}


def test_epoch_milliseconds_are_accepted():
    assert get_session_quality(1789357500000) in {"PRIME", "MODERATE"}

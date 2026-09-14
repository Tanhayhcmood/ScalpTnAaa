from datetime import datetime, timezone

from live_trading.signals.quality_filter import get_session_quality


def test_every_valid_utc_hour_is_tradeable():
    """Session quality must never hard-block a valid UTC hour."""
    for hour in range(24):
        timestamp = datetime(2026, 9, 14, hour, 30, tzinfo=timezone.utc)
        assert get_session_quality(timestamp.isoformat()) in {"PRIME", "MODERATE"}


def test_malformed_timestamp_stays_fail_closed():
    assert get_session_quality("not-a-timestamp") == "BLOCKED"
"""Regression tests for the robot liveness health check."""

from datetime import datetime, timedelta, timezone

from live_trading import server


def test_invalid_heartbeat_is_not_liveness_proof():
    assert not server._heartbeat_is_fresh("not-a-timestamp")


def test_fresh_heartbeat_proves_liveness():
    now = datetime(2026, 7, 27, 18, 0, tzinfo=timezone.utc)
    heartbeat = (now - timedelta(seconds=server._HEARTBEAT_MAX_AGE_SECONDS)).isoformat()

    assert server._heartbeat_is_fresh(heartbeat, now=now)


def test_stale_heartbeat_is_not_liveness_proof():
    now = datetime(2026, 7, 27, 18, 0, tzinfo=timezone.utc)
    heartbeat = (
        now - timedelta(seconds=server._HEARTBEAT_MAX_AGE_SECONDS + 1)
    ).isoformat()

    assert not server._heartbeat_is_fresh(heartbeat, now=now)


def test_missing_or_invalid_heartbeat_is_unhealthy():
    now = datetime(2026, 7, 27, 18, 0, tzinfo=timezone.utc)

    assert not server._heartbeat_is_fresh(None, now=now)
    assert not server._heartbeat_is_fresh("not-a-timestamp", now=now)
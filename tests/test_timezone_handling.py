"""Regression tests for timezone-aware panel timestamps."""

from datetime import datetime, timedelta, timezone

from telegram_panel.config.constants import NotificationType
from telegram_panel.models.notification import NotificationSetting
from telegram_panel.models.session import UserSession
from telegram_panel.utils.time import parse_utc, utc_now


def test_utc_now_is_timezone_aware():
    assert utc_now().tzinfo == timezone.utc


def test_parse_utc_normalizes_legacy_naive_timestamp():
    parsed = parse_utc("2026-07-27T18:00:00")
    assert parsed is not None
    assert parsed.tzinfo == timezone.utc
    assert parsed.isoformat() == "2026-07-27T18:00:00+00:00"


def test_parse_utc_normalizes_z_timestamp():
    parsed = parse_utc("2026-07-27T18:00:00Z")
    assert parsed is not None
    assert parsed.tzinfo == timezone.utc
    assert parsed.isoformat() == "2026-07-27T18:00:00+00:00"


def test_session_defaults_and_touch_are_timezone_aware():
    session = UserSession(id=None, telegram_id=123)
    assert session.started_at.tzinfo == timezone.utc
    assert session.last_activity_at.tzinfo == timezone.utc
    assert session.expires_at.tzinfo == timezone.utc

    session.touch(timeout_minutes=15)
    assert session.last_activity_at.tzinfo == timezone.utc
    assert session.expires_at.tzinfo == timezone.utc
    assert session.expires_at - session.last_activity_at == timedelta(minutes=15)


def test_session_accepts_legacy_naive_expiry():
    session = UserSession(
        id=None,
        telegram_id=123,
        expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=1),
    )
    assert session.is_expired() is False


def test_notification_setting_default_timestamps_are_timezone_aware():
    setting = NotificationSetting(notification_type=NotificationType.TRADE_OPEN)
    assert setting.created_at.tzinfo == timezone.utc
    assert setting.updated_at.tzinfo == timezone.utc
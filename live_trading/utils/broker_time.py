"""Convert broker-server wall times to UTC for session-time calculations."""

from datetime import datetime, timedelta, timezone

from live_trading.config import BROKER_UTC_OFFSET_HOURS

_DEFAULT_OFFSET_HOURS = 2.0
_MAX_AUTO_OFFSET_HOURS = 4.0
_MAX_TICK_AGE_SECONDS = 5 * 60
_last_good_offset_hours: float | None = None


def _configured_offset() -> float | None:
    value = BROKER_UTC_OFFSET_HOURS
    if isinstance(value, str) and value.strip().lower() == "auto":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def observe_broker_server_time(
    server_wall_time: datetime | None,
    *,
    now: datetime | None = None,
) -> float:
    """Update the auto offset from a quote's parsed broker wall-clock time."""
    global _last_good_offset_hours

    configured = _configured_offset()
    if configured is not None:
        return configured
    if not isinstance(server_wall_time, datetime):
        return (
            _last_good_offset_hours
            if _last_good_offset_hours is not None
            else _DEFAULT_OFFSET_HOURS
        )

    current_utc = now or datetime.now(timezone.utc)
    if current_utc.tzinfo is None:
        current_utc = current_utc.replace(tzinfo=timezone.utc)
    else:
        current_utc = current_utc.astimezone(timezone.utc)
    server_wall = server_wall_time.replace(tzinfo=None)
    current_wall = current_utc.replace(tzinfo=None)

    raw_offset = (server_wall - current_wall).total_seconds() / 3600.0
    candidate = round(raw_offset * 2.0) / 2.0
    if abs(candidate) > _MAX_AUTO_OFFSET_HOURS:
        return (
            _last_good_offset_hours
            if _last_good_offset_hours is not None
            else _DEFAULT_OFFSET_HOURS
        )

    corrected_tick = server_wall - timedelta(hours=candidate)
    tick_age = (current_wall - corrected_tick).total_seconds()
    if 0 <= tick_age < _MAX_TICK_AGE_SECONDS:
        _last_good_offset_hours = candidate
    return (
        _last_good_offset_hours
        if _last_good_offset_hours is not None
        else _DEFAULT_OFFSET_HOURS
    )


def broker_time_to_utc(dt: datetime) -> datetime:
    """Interpret a datetime's clock fields as broker time and return UTC."""
    if not isinstance(dt, datetime):
        raise TypeError("broker_time_to_utc expects a datetime")
    configured = _configured_offset()
    offset = (
        configured
        if configured is not None
        else (
            _last_good_offset_hours
            if _last_good_offset_hours is not None
            else _DEFAULT_OFFSET_HOURS
        )
    )
    broker_wall = dt.replace(tzinfo=None)
    return (broker_wall - timedelta(hours=offset)).replace(tzinfo=timezone.utc)

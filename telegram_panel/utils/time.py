"""UTC timestamp helpers used by panel persistence and services."""

from datetime import datetime, timezone
from typing import Optional


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def parse_utc(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO timestamp and normalize legacy naive values to UTC."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
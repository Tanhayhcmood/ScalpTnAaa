"""Stable identity and broker comment tagging for live entry signals."""

import hashlib
import re
from datetime import datetime, timezone


_TIMEFRAME_PREFIX = re.compile(r"^([mhd])(\d+)$", re.IGNORECASE)
_TIMEFRAME_SUFFIX = re.compile(r"^(\d+)([mhd])$", re.IGNORECASE)
_BROKER_COMMENT_LIMIT = 32
_SIGNAL_TAG_PREFIX = "|B="


def _canonical_timeframe(timeframe: str) -> str:
    """Treat aliases such as M5 and 5m as the same candle timeframe."""
    value = str(timeframe or "").strip()
    prefix_match = _TIMEFRAME_PREFIX.fullmatch(value)
    if prefix_match:
        return f"{int(prefix_match.group(2))}{prefix_match.group(1).lower()}"
    suffix_match = _TIMEFRAME_SUFFIX.fullmatch(value)
    if suffix_match:
        return f"{int(suffix_match.group(1))}{suffix_match.group(2).lower()}"
    return value.lower()


def entry_signal_id(symbol: str, timeframe: str, bar_time: datetime) -> str:
    """Return an alias- and timezone-stable ID for one closed candle signal."""
    if not isinstance(bar_time, datetime):
        raise TypeError("bar_time must be a datetime")
    normalized_time = bar_time
    if normalized_time.tzinfo is not None:
        normalized_time = normalized_time.astimezone(timezone.utc).replace(tzinfo=None)
    timestamp = normalized_time.isoformat(timespec="seconds")
    return (
        f"{str(symbol or '').strip().upper()}|"
        f"{_canonical_timeframe(timeframe)}|{timestamp}"
    )


def entry_signal_tag(signal_id: str) -> str:
    """Return a compact deterministic tag that fits in an MT5 comment."""
    digest = hashlib.blake2s(
        str(signal_id).encode("utf-8"),
        digest_size=5,
    ).hexdigest()
    return f"{_SIGNAL_TAG_PREFIX}{digest}"


def entry_signal_comment(base_comment: str, signal_id: str) -> str:
    """Append a signal tag without exceeding the broker's 32-char comment cap."""
    tag = entry_signal_tag(signal_id)
    prefix_length = max(0, _BROKER_COMMENT_LIMIT - len(tag))
    return f"{str(base_comment or '')[:prefix_length]}{tag}"

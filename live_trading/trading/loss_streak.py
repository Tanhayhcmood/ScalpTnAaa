"""Restart-safe loss-streak pause derived from persisted closed trade history."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Iterable, Optional

from live_trading.utils.broker_time import broker_time_to_utc


@dataclass(frozen=True)
class LossStreakPause:
    consecutive_losses: int
    pause_until: Optional[datetime]
    active: bool
    reason: str = ""


def _parse_close_time(value: object) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            return None
        timestamp = float(value)
        if abs(timestamp) > 100_000_000_000:
            timestamp /= 1000.0
        try:
            parsed = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            timestamp = float(raw)
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return None
        else:
            if not math.isfinite(timestamp):
                return None
            if abs(timestamp) > 100_000_000_000:
                timestamp /= 1000.0
            try:
                parsed = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None

    if parsed.tzinfo is None:
        return broker_time_to_utc(parsed)
    return parsed.astimezone(timezone.utc)


def _net_profit(value: object) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def evaluate_loss_streak_pause(
    trade_history: Iterable[dict],
    *,
    pause_count: int,
    pause_minutes: int,
    now_utc: datetime,
) -> LossStreakPause:
    """Compute the trailing closed-loss streak and its time-based entry pause.

    ``trade_history`` is the live loop's persisted/reconciled history, restored
    from Redis on restart and refreshed from broker history.
    """
    if pause_count < 1 or pause_minutes < 1:
        raise ValueError("pause_count and pause_minutes must be positive")
    now = (
        now_utc.replace(tzinfo=timezone.utc)
        if now_utc.tzinfo is None
        else now_utc.astimezone(timezone.utc)
    )

    closed: list[tuple[datetime, int, Optional[float]]] = []
    for index, trade in enumerate(trade_history):
        if not isinstance(trade, dict):
            continue
        status = str(trade.get("status") or "").strip().upper()
        close_time_value = trade.get("close_time", trade.get("closeTime"))
        if status not in {"CLOSED"} and close_time_value in (None, ""):
            continue
        if status in {"OPEN", "PENDING"}:
            continue
        close_time = _parse_close_time(close_time_value)
        if close_time is None:
            continue
        closed.append((close_time, index, _net_profit(trade.get("profit"))))

    closed.sort(key=lambda row: (row[0], row[1]))
    streak = 0
    latest_loss_time: Optional[datetime] = None
    for close_time, _, profit in closed:
        if profit is not None and profit < 0:
            streak += 1
            latest_loss_time = close_time
        else:
            streak = 0
            latest_loss_time = None

    if streak < pause_count or latest_loss_time is None:
        return LossStreakPause(streak, None, False)

    pause_until = latest_loss_time + timedelta(minutes=pause_minutes)
    active = now < pause_until
    reason = (
        f"{streak} consecutive losing closed trades; new entries paused "
        f"until {pause_until.isoformat()}"
        if active
        else ""
    )
    return LossStreakPause(streak, pause_until, active, reason)

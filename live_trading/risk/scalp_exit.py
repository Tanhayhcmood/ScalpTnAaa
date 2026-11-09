"""Pure calculations for the opt-in scalp exit profile."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import math
from typing import Iterable, Optional


def split_close_volume(
    volume: object,
    volume_min: object,
    volume_step: object,
) -> Optional[float]:
    """Return a broker-valid close volume near half, or None if unsplittable."""
    try:
        total = Decimal(str(volume))
        minimum = Decimal(str(volume_min))
        step = Decimal(str(volume_step))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if (
        not all(value.is_finite() for value in (total, minimum, step))
        or total <= 0
        or minimum <= 0
        or step <= 0
        or total < minimum * 2
    ):
        return None

    def _on_step(value: Decimal) -> bool:
        if value < minimum:
            return False
        units = (value - minimum) / step
        return units == units.to_integral_value()

    if not _on_step(total):
        return None
    target = total / Decimal("2")
    if target < minimum:
        return None
    steps = ((target - minimum) / step).to_integral_value(
        rounding=ROUND_FLOOR
    )
    partial = minimum + steps * step
    remainder = total - partial
    if not _on_step(partial) or not _on_step(remainder):
        return None
    return float(partial)


def breakeven_stop_price(
    entry_price: object,
    direction: str,
    spread_buffer: object,
    digits: int = 8,
) -> Optional[float]:
    """Move the stop to entry plus one spread in the profitable direction."""
    try:
        entry = float(entry_price)
        spread = float(spread_buffer)
        normalized_digits = max(0, min(8, int(digits)))
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(entry) or not math.isfinite(spread) or entry <= 0 or spread < 0:
        return None
    normalized_direction = str(direction).upper().strip()
    if normalized_direction == "BUY":
        return round(entry + spread, normalized_digits)
    if normalized_direction == "SELL":
        return round(entry - spread, normalized_digits)
    return None


def _utc_datetime(value: object) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
        if not math.isfinite(timestamp):
            return None
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
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def closed_m5_bars_since_entry(
    candles: Iterable[object],
    opened_at: object,
) -> int:
    """Count unique completed M5 candle timestamps after the entry time."""
    opened = _utc_datetime(opened_at)
    if opened is None:
        return 0
    timestamps = set()
    for candle in candles:
        if isinstance(candle, dict):
            candle_time = candle.get("time")
        else:
            candle_time = getattr(candle, "time", None)
        parsed = _utc_datetime(candle_time)
        if parsed is not None and parsed > opened:
            timestamps.add(parsed)
    return len(timestamps)


def best_favorable_price_since_entry(
    candles: Iterable[object],
    opened_at: object,
    direction: str,
    entry_price: object,
    current_price: object,
) -> Optional[float]:
    try:
        entry = float(entry_price)
        current = float(current_price)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(entry) or not math.isfinite(current):
        return None
    normalized_direction = str(direction).upper().strip()
    if normalized_direction == "BUY":
        extreme = max(entry, current)
        field = "high"
    elif normalized_direction == "SELL":
        extreme = min(entry, current)
        field = "low"
    else:
        return None
    opened = _utc_datetime(opened_at)
    if opened is None:
        return extreme
    for candle in candles:
        candle_time = (
            candle.get("time")
            if isinstance(candle, dict)
            else getattr(candle, "time", None)
        )
        parsed = _utc_datetime(candle_time)
        if parsed is None or parsed <= opened:
            continue
        value = (
            candle.get(field)
            if isinstance(candle, dict)
            else getattr(candle, field, None)
        )
        try:
            price = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(price):
            continue
        extreme = max(extreme, price) if normalized_direction == "BUY" else min(extreme, price)
    return extreme


def scalp_time_stop_due(
    closed_bars: object,
    reached_half_r: bool,
    time_stop_bars: int = 6,
) -> bool:
    try:
        bars = int(closed_bars)
        threshold = int(time_stop_bars)
    except (TypeError, ValueError, OverflowError):
        return False
    return threshold > 0 and bars >= threshold and not reached_half_r

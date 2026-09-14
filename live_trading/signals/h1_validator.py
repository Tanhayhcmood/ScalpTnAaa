"""
H1 market-data integrity and EMA consistency checks.

This is intentionally a read-only, fail-closed guard.  It validates the
completed H1 candle series before the higher-timeframe engines are allowed to
use it.  It does not change a signal, position, risk limit, or broker state.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from live_trading.signals.gold_engine import OHLCV, calc_ema


H1_MIN_CANDLES = 210  # EMA-200 plus a meaningful warm-up window.
H1_MAX_GAP_HOURS = 96  # Allows normal weekend/holiday market closures.
H1_MAX_LIVE_AGE_HOURS = 4
H1_CLOSE_GRACE_SECONDS = 2
EMA_MATCH_TOLERANCE = 0.0002
EMA_COLLAPSE_TOLERANCE = 0.0001


@dataclass
class H1ValidationResult:
    """Evidence that an H1 series is safe to pass to the HTF analyzers."""

    valid: bool
    reason: str
    candle_count: int
    latest_time: Optional[datetime] = None
    ema50: float = 0.0
    ema100: float = 0.0
    ema200: float = 0.0
    ema_alignment: str = "UNKNOWN"
    issues: List[str] = field(default_factory=list)

    def matches_ema(self, ema50: float, ema100: float, ema200: float) -> bool:
        """Confirm the analyzer used the same H1 close series for its EMAs."""
        return (
            math.isclose(self.ema50, ema50, rel_tol=0.0, abs_tol=EMA_MATCH_TOLERANCE)
            and math.isclose(self.ema100, ema100, rel_tol=0.0, abs_tol=EMA_MATCH_TOLERANCE)
            and math.isclose(self.ema200, ema200, rel_tol=0.0, abs_tol=EMA_MATCH_TOLERANCE)
        )

    @property
    def summary(self) -> str:
        latest = self.latest_time.isoformat() if self.latest_time else "unknown"
        return (
            f"H1 data verified: {self.candle_count} closed candles, latest={latest}; "
            f"EMA50={self.ema50:.4f}, EMA100={self.ema100:.4f}, "
            f"EMA200={self.ema200:.4f}, alignment={self.ema_alignment}"
        )


def _parse_time(value: object) -> Optional[datetime]:
    """Parse ISO-8601 or Unix-second/millisecond candle timestamps as UTC-naive."""
    raw = str(value).strip()
    if not raw:
        return None

    try:
        numeric = float(raw)
        if math.isfinite(numeric):
            if abs(numeric) > 100_000_000_000:
                numeric /= 1000.0
            return datetime.fromtimestamp(numeric, tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        pass

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _ema_alignment(ema50: float, ema100: float, ema200: float) -> str:
    if ema50 > ema100 > ema200:
        return "BULLISH"
    if ema50 < ema100 < ema200:
        return "BEARISH"
    return "MIXED"


def validate_h1_candles(
    candles: List[OHLCV],
    *,
    min_candles: int = H1_MIN_CANDLES,
    now: Optional[datetime] = None,
) -> H1ValidationResult:
    """
    Validate completed H1 OHLCV data and the three production EMAs.

    The function never raises.  Any malformed, duplicated, out-of-order,
    non-hourly, stale, or impossible OHLC data returns ``valid=False``.
    Normal weekend/holiday gaps up to 96 hours are allowed.
    """
    issues: List[str] = []
    count = len(candles)
    if count < min_candles:
        issues.append(f"only {count} candles (minimum {min_candles})")

    parsed_times: List[Optional[datetime]] = [_parse_time(c.time) for c in candles]
    if any(value is None for value in parsed_times):
        issues.append("one or more candle timestamps are invalid")

    valid_times = [value for value in parsed_times if value is not None]
    latest_time = valid_times[-1] if valid_times else None
    for previous, current in zip(valid_times, valid_times[1:]):
        delta_seconds = (current - previous).total_seconds()
        if delta_seconds <= 0:
            issues.append("timestamps are not strictly increasing")
            break
        if delta_seconds < 3600:
            issues.append("timestamps contain duplicate or sub-hour bars")
            break
        if delta_seconds > H1_MAX_GAP_HOURS * 3600:
            issues.append(f"timestamp gap exceeds {H1_MAX_GAP_HOURS} hours")
            break
        if delta_seconds % 3600 != 0:
            issues.append("timestamp spacing is not an integer number of hours")
            break

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is not None:
        current_time = current_time.astimezone(timezone.utc).replace(tzinfo=None)
    if latest_time is not None:
        age_hours = (current_time - latest_time).total_seconds() / 3600.0
        if age_hours < -0.05:
            issues.append("latest candle is in the future")
        elif latest_time + timedelta(hours=1) > current_time + timedelta(
            seconds=H1_CLOSE_GRACE_SECONDS
        ):
            issues.append("latest candle is still open")
        elif age_hours > H1_MAX_LIVE_AGE_HOURS and current_time.weekday() < 5:
            issues.append(
                f"latest candle is {age_hours:.1f} hours old "
                f"(maximum {H1_MAX_LIVE_AGE_HOURS})"
            )

    for candle_time in valid_times:
        if (
            candle_time.minute != 0
            or candle_time.second != 0
            or candle_time.microsecond != 0
        ):
            issues.append("timestamps are not aligned to H1 boundaries")
            break

    for index, candle in enumerate(candles):
        values = (candle.open, candle.high, candle.low, candle.close, candle.volume)
        if not all(math.isfinite(float(value)) for value in values):
            issues.append(f"candle {index} contains a non-finite value")
            break
        if min(candle.open, candle.high, candle.low, candle.close) <= 0:
            issues.append(f"candle {index} contains a non-positive price")
            break
        if candle.volume < 0:
            issues.append(f"candle {index} contains negative volume")
            break
        if candle.high < max(candle.open, candle.close) or candle.low > min(
            candle.open, candle.close
        ):
            issues.append(f"candle {index} violates OHLC high/low bounds")
            break
        if candle.high < candle.low:
            issues.append(f"candle {index} has high below low")
            break

    ema50 = ema100 = ema200 = 0.0
    alignment = "UNKNOWN"
    if not issues and count >= 200:
        closes = [float(candle.close) for candle in candles]
        ema50 = calc_ema(closes, 50)
        ema100 = calc_ema(closes, 100)
        ema200 = calc_ema(closes, 200)
        alignment = _ema_alignment(ema50, ema100, ema200)
        if not all(math.isfinite(value) for value in (ema50, ema100, ema200)):
            issues.append("EMA calculation produced a non-finite value")
        elif max(ema50, ema100, ema200) - min(ema50, ema100, ema200) <= EMA_COLLAPSE_TOLERANCE:
            issues.append("EMA50, EMA100, and EMA200 collapsed to the same value")

    if issues:
        return H1ValidationResult(
            valid=False,
            reason="H1 validation failed: " + "; ".join(dict.fromkeys(issues)),
            candle_count=count,
            latest_time=latest_time,
            ema50=ema50,
            ema100=ema100,
            ema200=ema200,
            ema_alignment=alignment,
            issues=list(dict.fromkeys(issues)),
        )

    return H1ValidationResult(
        valid=True,
        reason="",
        candle_count=count,
        latest_time=latest_time,
        ema50=ema50,
        ema100=ema100,
        ema200=ema200,
        ema_alignment=alignment,
    )
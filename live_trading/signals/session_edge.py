"""Asian-session range context and daily anchored VWAP location filter.

This module is deliberately observational unless SESSION_EDGE_MODE=enforce.
It accepts closed M5 candles; naive broker wall times are converted by the
shared broker-time helper, while timestamps carrying an offset are respected.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import date, datetime, time, timezone
from typing import Any, Iterable

from live_trading.utils.broker_time import broker_time_to_utc

_LOG = logging.getLogger(__name__)
UTC = timezone.utc
ASIAN_START = time(0, 0)
ASIAN_END = time(7, 0)
EDGE_START = time(7, 0)
EDGE_END = time(11, 0)
MIN_RANGE_PERCENT = 0.30
MAX_RANGE_PERCENT = 1.60


def get_session_edge_mode(value: str | None = None) -> str:
    """Return off, shadow (default), or enforce; invalid values fail safe to off."""
    raw = os.getenv("SESSION_EDGE_MODE", "shadow") if value is None else value
    mode = str(raw).strip().lower()
    if mode in {"off", "shadow", "enforce"}:
        return mode
    _LOG.warning("Invalid SESSION_EDGE_MODE=%r; treating it as off", raw)
    return "off"


def utc_datetime(value: Any) -> datetime:
    """Parse a timestamp and convert it to UTC without assuming broker offset."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        raise TypeError("candle time must be datetime or ISO-8601 text")
    if parsed.tzinfo is None:
        return broker_time_to_utc(parsed)
    return parsed.astimezone(UTC)


def _field(candle: Any, name: str, default: Any = None) -> Any:
    if isinstance(candle, dict):
        return candle.get(name, default)
    return getattr(candle, name, default)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _bars(candles: Iterable[Any] | None) -> list[dict[str, Any]]:
    by_time: dict[datetime, dict[str, Any]] = {}
    for candle in candles or []:
        try:
            timestamp = utc_datetime(_field(candle, "time"))
        except (TypeError, ValueError, OverflowError):
            continue
        open_price = _number(_field(candle, "open"))
        high = _number(_field(candle, "high"))
        low = _number(_field(candle, "low"))
        close = _number(_field(candle, "close"))
        volume = _number(
            _field(candle, "volume", _field(candle, "tick_volume", 0.0))
        )
        if (
            min(open_price, high, low, close) <= 0
            or volume < 0
            or high < max(open_price, close)
            or low > min(open_price, close)
            or high < low
        ):
            continue
        by_time[timestamp] = {
            "time": timestamp,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    return [by_time[key] for key in sorted(by_time)]


def _date_value(value: date | str | None, rows: list[dict[str, Any]]) -> date | None:
    if isinstance(value, datetime):
        return utc_datetime(value).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return rows[-1]["time"].date() if rows else None


def compute_asian_range(
    candles: Iterable[Any], session_date: date | str | None = None
) -> dict[str, Any]:
    """Compute UTC 00:00-07:00 high/low and validate range as % of midpoint."""
    rows = _bars(candles)
    day = _date_value(session_date, rows)
    asian = [
        row for row in rows
        if day is not None
        and row["time"].date() == day
        and ASIAN_START <= row["time"].time().replace(tzinfo=None) < ASIAN_END
    ]
    if not asian:
        return {
            "date_utc": day.isoformat() if day else None,
            "status": "NO_SESSION_EDGE",
            "valid": False,
            "reason": "missing_asian_range_data",
            "high": None,
            "low": None,
            "range_percent": None,
            "bar_count": 0,
        }
    range_high = max(row["high"] for row in asian)
    range_low = min(row["low"] for row in asian)
    midpoint = (range_high + range_low) / 2.0
    range_percent = ((range_high - range_low) / midpoint) * 100.0 if midpoint > 0 else 0.0
    valid = MIN_RANGE_PERCENT <= range_percent <= MAX_RANGE_PERCENT
    return {
        "date_utc": day.isoformat(),
        "status": "VALID" if valid else "NO_SESSION_EDGE",
        "valid": valid,
        "reason": "" if valid else "asian_range_size_out_of_bounds",
        "high": range_high,
        "low": range_low,
        "range_percent": range_percent,
        "bar_count": len(asian),
    }


def _find_asian_signal(
    rows: list[dict[str, Any]], range_info: dict[str, Any]
) -> dict[str, Any]:
    day_text = range_info.get("date_utc")
    if not range_info.get("valid") or not day_text:
        return {"signal": None, "signal_type": None, "signal_time_utc": None}
    day = date.fromisoformat(day_text)
    high = float(range_info["high"])
    low = float(range_info["low"])
    session_bars = [
        row for row in rows
        if row["time"].date() == day
        and EDGE_START <= row["time"].time().replace(tzinfo=None) < EDGE_END
    ]
    pending_breakout: tuple[str, datetime] | None = None
    for row in session_bars:
        close = row["close"]
        # A closed bar after a confirmed breakout must touch the boundary and
        # close back beyond it before continuation is signalled.
        if pending_breakout is not None and row["time"] > pending_breakout[1]:
            if pending_breakout[0] == "BUY" and row["low"] <= high and close > high:
                return {
                    "signal": "BUY",
                    "signal_type": "BREAKOUT_RETEST",
                    "signal_time_utc": row["time"].isoformat().replace("+00:00", "Z"),
                }
            if pending_breakout[0] == "SELL" and row["high"] >= low and close < low:
                return {
                    "signal": "SELL",
                    "signal_type": "BREAKOUT_RETEST",
                    "signal_time_utc": row["time"].isoformat().replace("+00:00", "Z"),
                }
        # A wick beyond the boundary that closes back inside is a reversal.
        inside = low < close < high
        if inside and row["high"] > high:
            return {
                "signal": "SELL",
                "signal_type": "SWEEP_REVERSAL",
                "signal_time_utc": row["time"].isoformat().replace("+00:00", "Z"),
            }
        if inside and row["low"] < low:
            return {
                "signal": "BUY",
                "signal_type": "SWEEP_REVERSAL",
                "signal_time_utc": row["time"].isoformat().replace("+00:00", "Z"),
            }
        if close > high:
            pending_breakout = ("BUY", row["time"])
        elif close < low:
            pending_breakout = ("SELL", row["time"])
    return {"signal": None, "signal_type": None, "signal_time_utc": None}


def daily_anchored_vwap(
    candles: Iterable[Any], anchor_date: date | str | None = None
) -> float | None:
    """Calculate M5 typical-price VWAP anchored at 00:00 UTC for one UTC day."""
    rows = _bars(candles)
    day = _date_value(anchor_date, rows)
    if day is None:
        return None
    anchored = [row for row in rows if row["time"].date() == day]
    total_volume = sum(row["volume"] for row in anchored)
    if not anchored or total_volume <= 0:
        return None
    weighted = sum(
        ((row["high"] + row["low"] + row["close"]) / 3.0) * row["volume"]
        for row in anchored
    )
    return weighted / total_volume


def evaluate_entry_context(
    candles: Iterable[Any],
    direction: str,
    *,
    mode: str | None = None,
    applicable: bool = True,
    session_edge_trade_dates: Iterable[str] = (),
) -> dict[str, dict[str, Any]]:
    """Return Asian-edge and VWAP telemetry plus the non-mutating gate result."""
    resolved_mode = get_session_edge_mode(mode)
    rows = _bars(candles)
    range_info = compute_asian_range(rows)
    signal_info = _find_asian_signal(rows, range_info)
    day_text = range_info.get("date_utc")
    used_dates = {str(value) for value in session_edge_trade_dates}
    trade_used = bool(day_text and day_text in used_dates)
    latest = rows[-1] if rows else None
    latest_time = latest["time"] if latest else None
    in_edge_window = bool(
        latest_time
        and latest_time.date().isoformat() == day_text
        and EDGE_START <= latest_time.time().replace(tzinfo=None) < EDGE_END
    )
    signal = signal_info["signal"]
    active = bool(signal and in_edge_window)
    session_edge = {
        **range_info,
        **signal_info,
        "mode": resolved_mode,
        "active": active,
        "trade_used_today": trade_used,
        "available_to_trade": active and not trade_used,
        "status": (
            "SIGNAL" if signal else
            range_info["status"] if not range_info["valid"] else "NO_SIGNAL"
        ),
    }

    vwap = daily_anchored_vwap(rows, day_text)
    price = latest["close"] if latest else None
    side = (
        "ABOVE" if vwap is not None and price is not None and price > vwap else
        "BELOW" if vwap is not None and price is not None and price < vwap else
        "AT" if vwap is not None and price is not None else "UNKNOWN"
    )
    normalized_direction = str(direction or "").upper().strip()
    aligned = (
        price > vwap if normalized_direction == "BUY" and price is not None and vwap is not None else
        price < vwap if normalized_direction == "SELL" and price is not None and vwap is not None else
        False if normalized_direction in {"BUY", "SELL"} else None
    )
    sweep_override = bool(
        active and not trade_used and signal == normalized_direction
    )
    would_allow = bool(aligned or sweep_override)
    if not applicable or normalized_direction not in {"BUY", "SELL"}:
        action = "NOT_APPLICABLE"
    elif resolved_mode == "off":
        action = "OFF"
    elif resolved_mode == "shadow":
        action = "WOULD_ALLOW" if would_allow else "WOULD_BLOCK"
    else:
        action = "ALLOW" if would_allow else "BLOCK"
    if resolved_mode == "shadow" and applicable and normalized_direction in {"BUY", "SELL"}:
        _LOG.info(
            "SESSION_EDGE %s direction=%s vwap=%s edge=%s reason=%s",
            action,
            normalized_direction,
            f"{vwap:.5f}" if vwap is not None else "unavailable",
            signal or "none",
            "vwap_aligned" if aligned else "asian_edge_override" if sweep_override else "location_mismatch_or_data_unavailable",
        )
    elif resolved_mode == "enforce" and applicable and normalized_direction in {"BUY", "SELL"}:
        _LOG.info(
            "SESSION_EDGE %s direction=%s vwap=%s edge=%s",
            action,
            normalized_direction,
            f"{vwap:.5f}" if vwap is not None else "unavailable",
            signal or "none",
        )

    reason = (
        "vwap_aligned" if aligned else
        "asian_edge_override" if sweep_override else
        "vwap_unavailable" if vwap is None else
        "price_not_on_directional_vwap_side"
    )
    vwap_filter = {
        "mode": resolved_mode,
        "status": "READY" if vwap is not None else "UNAVAILABLE",
        "anchor_date_utc": day_text,
        "vwap": round(vwap, 8) if vwap is not None else None,
        "price": round(price, 8) if price is not None else None,
        "side": side,
        "direction": normalized_direction or None,
        "vwap_aligned": aligned,
        "asian_edge_override": sweep_override,
        "would_allow": would_allow,
        "action": action,
        "reason": reason,
    }
    return {"session_edge": session_edge, "vwap_filter": vwap_filter}

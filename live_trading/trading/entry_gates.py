"""Pure checks for spread, UTC trading window, and Friday entry cutoff."""

from datetime import datetime, timezone
import math


def evaluate_spread_atr_gate(
    spread_price: float | None,
    m5_atr: float,
    *,
    max_spread_atr_fraction: float = 0.15,
) -> tuple[bool, str]:
    """Block entries when spread exceeds a fraction of M5 ATR(14)."""
    try:
        spread = float(spread_price)
        atr_value = float(m5_atr)
        fraction = float(max_spread_atr_fraction)
    except (TypeError, ValueError, OverflowError):
        return False, "Spread or M5 ATR(14) is unavailable"

    if not math.isfinite(spread) or spread < 0:
        return False, "Spread unavailable or invalid"
    if not math.isfinite(atr_value) or atr_value <= 0:
        return False, "M5 ATR(14) unavailable or invalid"
    if not math.isfinite(fraction) or fraction < 0:
        return False, "Invalid maximum spread ATR fraction"

    limit = fraction * atr_value
    if spread > limit:
        return (
            False,
            f"Spread {spread:.4f} exceeds {fraction:.2f} × "
            f"M5 ATR(14) limit {limit:.4f}",
        )
    return True, ""


def evaluate_entry_gates(
    now_utc: datetime,
    spread_price: float | None,
    *,
    max_spread_price: float,
    trade_window_utc_start: int,
    trade_window_utc_end: int,
    friday_last_entry_utc_hour: int,
) -> tuple[bool, list[str]]:
    """Return whether a new entry passes the configured market-time gates."""
    if now_utc.tzinfo is None:
        current_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        current_utc = now_utc.astimezone(timezone.utc)

    reasons: list[str] = []
    if not 0 <= trade_window_utc_start < trade_window_utc_end <= 24:
        reasons.append("Invalid UTC trading window")
    elif not trade_window_utc_start <= current_utc.hour < trade_window_utc_end:
        reasons.append(
            "Outside UTC trading window "
            f"{trade_window_utc_start:02d}:00–{trade_window_utc_end:02d}:00"
        )

    if not 0 <= friday_last_entry_utc_hour <= 23:
        reasons.append("Invalid Friday entry cutoff hour")
    elif current_utc.weekday() == 4 and current_utc.hour >= friday_last_entry_utc_hour:
        reasons.append(
            f"Friday entry cutoff reached at "
            f"{friday_last_entry_utc_hour:02d}:00 UTC"
        )

    if not math.isfinite(max_spread_price) or max_spread_price < 0:
        reasons.append("Invalid maximum spread")
    elif spread_price is None:
        reasons.append("Spread unavailable")
    else:
        try:
            spread = float(spread_price)
        except (TypeError, ValueError):
            spread = math.nan
        if not math.isfinite(spread) or spread < 0:
            reasons.append("Spread unavailable or invalid")
        elif spread > max_spread_price:
            reasons.append(
                f"Spread {spread:.4f} exceeds limit {max_spread_price:.4f}"
            )

    return not reasons, reasons

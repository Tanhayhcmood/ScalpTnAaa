"""Last-moment entry validation for fast markets.

Signal generation works from a closed candle, while the broker quote can move
materially before the order request reaches MTAPI. This module keeps that
timing-sensitive safety rule pure and independently testable.
"""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class EntryPriceGuardResult:
    allowed: bool
    reason: str
    distance: float
    max_distance: float
    distance_atr: float


def validate_entry_price_distance(
    direction: str,
    signal_price: float,
    execution_price: float,
    atr: float,
    max_atr_distance: float,
) -> EntryPriceGuardResult:
    """Reject entries whose live executable price drifted from the signal.

    The distance is intentionally absolute for both BUY and SELL. A stale
    signal is unsafe even when the move happened in the nominally favorable
    direction: it may mean the setup has already completed and a late entry is
    now exposed to the inevitable retracement.
    """
    normalized_direction = str(direction).upper().strip()
    values = (signal_price, execution_price, atr, max_atr_distance)
    if normalized_direction not in {"BUY", "SELL"}:
        return EntryPriceGuardResult(
            False,
            f"Invalid entry direction: {direction!r}",
            0.0,
            0.0,
            0.0,
        )
    if not all(math.isfinite(float(value)) for value in values):
        return EntryPriceGuardResult(
            False,
            "Signal-price guard received non-finite market data",
            0.0,
            0.0,
            0.0,
        )
    if signal_price <= 0 or execution_price <= 0 or atr <= 0 or max_atr_distance <= 0:
        return EntryPriceGuardResult(
            False,
            "Signal-price guard received invalid price or ATR data",
            0.0,
            max(0.0, atr * max_atr_distance),
            0.0,
        )

    distance = round(abs(execution_price - signal_price), 6)
    max_distance = round(atr * max_atr_distance, 6)
    distance_atr = round(distance / atr, 6)
    if distance > max_distance:
        return EntryPriceGuardResult(
            False,
            (
                f"{normalized_direction} signal stale: execution moved "
                f"{distance:.2f} ({distance_atr:.2f} ATR) from signal; "
                f"limit is {max_distance:.2f} ({max_atr_distance:.2f} ATR)"
            ),
            distance,
            max_distance,
            distance_atr,
        )

    return EntryPriceGuardResult(
        True,
        (
            f"{normalized_direction} signal price valid: drift "
            f"{distance:.2f} ({distance_atr:.2f} ATR) within "
            f"{max_atr_distance:.2f} ATR limit"
        ),
        distance,
        max_distance,
        distance_atr,
    )
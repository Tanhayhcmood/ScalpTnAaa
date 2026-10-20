"""MTAPI SymbolParams volume-unit conversion helpers."""

import math
import os


DEFAULT_MTAPI_VOLUME_SCALE = 100_000_000.0


def configured_mtapi_volume_scale() -> float:
    """Return the extended-volume units per standard lot."""
    raw_scale = os.environ.get("MTAPI_VOLUME_SCALE")
    if raw_scale is None or not raw_scale.strip():
        return DEFAULT_MTAPI_VOLUME_SCALE

    try:
        scale = float(raw_scale)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "MTAPI_VOLUME_SCALE must be a finite positive number"
        ) from exc

    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("MTAPI_VOLUME_SCALE must be a finite positive number")
    return scale


def extended_volume_to_lots(raw_volume: object, scale: float) -> float:
    """Convert one MTAPI extended-volume value to standard lots."""
    if isinstance(raw_volume, bool):
        raise ValueError("MTAPI volume value is invalid")
    try:
        raw_value = float(raw_volume)
    except (TypeError, ValueError) as exc:
        raise ValueError("MTAPI volume value is invalid") from exc

    if not math.isfinite(raw_value):
        raise ValueError("MTAPI volume value must be finite")
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("MTAPI volume scale must be finite and positive")

    lots = raw_value / scale
    if not math.isfinite(lots):
        raise ValueError("converted MTAPI volume must be finite")
    return lots


def volume_constraint_sanity_error(
    volume_min: float | None,
    volume_step: float | None,
    volume_max: float | None,
) -> str | None:
    """Return a fail-closed reason for implausible converted lot constraints."""
    if volume_min is not None and volume_min > 1.0:
        return (
            f"converted minimum lot {volume_min:g} exceeds the 1 lot sanity limit"
        )
    if volume_max is not None and volume_max > 10_000.0:
        return (
            f"converted maximum lot {volume_max:g} exceeds the 10000 lot sanity limit"
        )
    if volume_step is not None and volume_step <= 0:
        return f"converted volume step must be greater than zero (got {volume_step:g})"
    return None

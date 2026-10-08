"""Small, dependency-free trade metrics for post-trade forensics.

MFE/MAE are quote-price distances, not account-currency P&L. R values use
the initial entry-to-stop distance; this module does not fetch market data.
"""
from __future__ import annotations

import json
import math
import sys
from typing import Iterable


def _finite(value: object, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def _side(value: str) -> str:
    side = str(value).strip().upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    return side


def trade_metrics(
    side: str, entry: float, stop: float, exit_price: float,
    highs: Iterable[float] = (), lows: Iterable[float] = (),
) -> dict[str, float]:
    """Return realized R and price/R MFE/MAE from supplied in-trade extremes."""
    direction = _side(side)
    entry_price = _finite(entry, "entry")
    stop_price = _finite(stop, "stop")
    exit_value = _finite(exit_price, "exit_price")
    risk = abs(entry_price - stop_price)
    if risk == 0:
        raise ValueError("entry and stop must differ")
    high_values = [_finite(value, "high") for value in highs]
    low_values = [_finite(value, "low") for value in lows]
    if len(high_values) != len(low_values):
        raise ValueError("highs and lows must have equal lengths")
    if any(high < low for high, low in zip(high_values, low_values)):
        raise ValueError("each high must be greater than or equal to its low")

    if direction == "BUY":
        realized = exit_value - entry_price
        favorable = max((high - entry_price for high in high_values), default=0.0)
        adverse = max((entry_price - low for low in low_values), default=0.0)
    else:
        realized = entry_price - exit_value
        favorable = max((entry_price - low for low in low_values), default=0.0)
        adverse = max((high - entry_price for high in high_values), default=0.0)
    mfe = max(0.0, favorable)
    mae = max(0.0, adverse)
    return {
        "r_multiple": realized / risk,
        "mfe_price": mfe,
        "mae_price": mae,
        "mfe_r": mfe / risk,
        "mae_r": mae / risk,
    }


def daily_loss_metrics(day_open: float, current: float, limit_pct: float) -> dict[str, float]:
    """Measure loss and percent of configured limit used on one chosen basis.

    Pass balance values for realized-only accounting or equity values to include
    floating P&L; this helper intentionally does not choose the basis.
    """
    baseline = _finite(day_open, "day_open")
    now = _finite(current, "current")
    limit = _finite(limit_pct, "limit_pct")
    if baseline <= 0 or now < 0 or limit <= 0:
        raise ValueError("day_open and limit_pct must be positive; current cannot be negative")
    loss_pct = max(0.0, (baseline - now) / baseline * 100.0)
    return {
        "daily_loss_pct": loss_pct,
        "daily_loss_limit_pct": limit,
        "daily_risk_used_pct": loss_pct / limit * 100.0,
    }


def main() -> int:
    """Read one JSON object from stdin and emit one JSON result to stdout."""
    try:
        payload = json.load(sys.stdin)
        if payload.get("kind") == "trade":
            result = trade_metrics(
                payload["side"], payload["entry"], payload["stop"],
                payload["exit_price"], payload.get("highs", ()), payload.get("lows", ()),
            )
        elif payload.get("kind") == "daily_loss":
            result = daily_loss_metrics(
                payload["day_open"], payload["current"], payload["limit_pct"]
            )
        else:
            raise ValueError("kind must be trade or daily_loss")
        print(json.dumps(result, sort_keys=True))
        return 0
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

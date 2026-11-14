"""Shared offline backtest helpers; never imported by the live trading loop."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Sequence

from live_trading.risk.adaptive_trailing_stop import (
    AdaptiveTrailingConfig,
    compute_adaptive_trail,
    should_apply,
)
from live_trading.signals.gold_engine import OHLCV
from live_trading.trading.active_entry_policy import ACTIVE_ENTRY_TIMEFRAME

M5_TIMEFRAME = ACTIVE_ENTRY_TIMEFRAME
ALLOWED_DIRECTIONS = frozenset({"BUY", "SELL"})


def _value(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def simulate_trade(
    bars: Sequence[OHLCV],
    spreads: Sequence[float],
    signal_index: int,
    decision: Any,
    *,
    trailing_config: AdaptiveTrailingConfig | None = None,
    max_exit_index: int | None = None,
) -> dict[str, float | int | str] | None:
    """Simulate one trade using the same adaptive stop state machine as live.

    Bar handling is deliberately conservative: the existing stop is checked
    before the target, and a newly calculated trail becomes active on the next
    bar because M1/M5 OHLC data cannot reveal intrabar price order.

    ``CapitalOutput.break_even_at``, ``break_even_sl``,
    ``trailing_activation_at`` and ``trailing_stop_distance`` are display/
    planning fields in the current live code. ``live_loop`` manages positions
    with ``compute_adaptive_trail`` from the original entry and initial risk;
    this simulator follows that actual path rather than treating the unused
    fields as live instructions.
    """
    if signal_index + 1 >= len(bars) or _value(decision, "trade_params") is None:
        return None

    params = _value(decision, "trade_params")
    direction = str(_value(decision, "direction", "")).upper()
    entry_index = signal_index + 1
    last_index = len(bars) - 1 if max_exit_index is None else min(max_exit_index, len(bars) - 1)
    if direction not in {"BUY", "SELL"} or entry_index > last_index:
        return None

    try:
        entry = float(bars[entry_index].open)
        params_entry = float(_value(params, "entry_price", bars[signal_index].close))
        price_shift = entry - params_entry
        initial_sl = float(_value(params, "stop_loss")) + price_shift
        target = float(_value(params, "take_profit")) + price_shift
        risk = abs(entry - initial_sl)
        spread = float(spreads[entry_index])
    except (IndexError, TypeError, ValueError, OverflowError):
        return None
    if (
        not all(math.isfinite(value) for value in (entry, initial_sl, target, risk, spread))
        or risk <= 0
        or spread < 0
    ):
        return None
    if (
        direction == "BUY" and (initial_sl >= entry or target <= entry)
    ) or (
        direction == "SELL" and (initial_sl <= entry or target >= entry)
    ):
        return None

    cfg = trailing_config or AdaptiveTrailingConfig()
    current_sl = initial_sl
    high_since_entry = entry
    low_since_entry = entry
    breakeven_armed = False
    exit_price = float(bars[last_index].close)
    exit_index = last_index
    exit_reason = "window_end"

    for index in range(entry_index, last_index + 1):
        candle = bars[index]
        open_price = float(candle.open)
        high = float(candle.high)
        low = float(candle.low)

        if direction == "BUY":
            if low <= current_sl:
                exit_price = min(open_price, current_sl)
                exit_index = index
                exit_reason = "stop"
                break
            if high >= target:
                exit_price = target
                exit_index = index
                exit_reason = "target"
                break
        else:
            if high >= current_sl:
                exit_price = max(open_price, current_sl)
                exit_index = index
                exit_reason = "stop"
                break
            if low <= target:
                exit_price = target
                exit_index = index
                exit_reason = "target"
                break

        high_since_entry = max(high_since_entry, high)
        low_since_entry = min(low_since_entry, low)
        if not cfg.enabled:
            continue
        live_trail = compute_adaptive_trail(
            direction=direction,
            current_price=float(candle.close),
            candles=bars[: index + 1],
            cfg=cfg,
            entry_price=entry,
            initial_sl=initial_sl,
            risk_distance=risk,
            current_sl=current_sl,
            highest_price_since_entry=high_since_entry,
            lowest_price_since_entry=low_since_entry,
            breakeven_armed=breakeven_armed,
        )
        breakeven_armed = live_trail.breakeven_armed
        apply_step = 0.0 if live_trail.stage == "BREAKEVEN" else cfg.min_step_price
        if should_apply(
            direction,
            current_sl,
            live_trail.candidate_sl,
            apply_step,
            reject_unchanged=cfg.adaptive_trail_v2,
        ):
            current_sl = float(live_trail.candidate_sl)

    gross = exit_price - entry if direction == "BUY" else entry - exit_price
    net = gross - spread
    return {
        "net": net,
        "r": net / risk,
        "exit_price": exit_price,
        "exit_index": exit_index,
        "exit_reason": exit_reason,
    }

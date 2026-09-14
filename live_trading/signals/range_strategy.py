"""Dedicated RANGE entry gate.

The trend engines remain unchanged.  This module is deliberately additive and
fail-closed: a range trade is only eligible at a validated range edge after a
liquidity sweep and a closed-candle reversal pattern.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.price_action_engine import PriceActionResult
from live_trading.signals.smc_engine import SmcResult


RangeLocation = Literal["SUPPORT", "RESISTANCE", "MIDDLE", "OUTSIDE", "UNKNOWN"]


@dataclass
class RangeContext:
    valid: bool
    direction: Literal["BUY", "SELL", "NEUTRAL"]
    support: float
    resistance: float
    atr: float
    edge_distance: float
    location: RangeLocation
    liquidity_sweep: bool
    reversal_candle: bool
    confirmation_count: int
    reason: str


def _atr(candles: list[OHLCV], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for current, previous in zip(candles[1:], candles[:-1]):
        trs.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    window = trs[-period:]
    return sum(window) / len(window) if window else 0.0


def _latest_sweep(smc: SmcResult, direction: str, last_bar: int) -> bool:
    """Require a sweep on the signal candle or one immediately before it."""
    wanted = "BULLISH" if direction == "BUY" else "BEARISH"
    return any(
        sweep.type == wanted and sweep.bar_index >= last_bar - 1
        for sweep in smc.liquidity_sweeps
    )


def _reversal_candle(pa: PriceActionResult, direction: str) -> bool:
    if direction == "BUY":
        return pa.bullish_engulf or pa.bullish_pin_bar or pa.strong_bullish
    return pa.bearish_engulf or pa.bearish_pin_bar or pa.strong_bearish


def evaluate_range_entry(
    candles: list[OHLCV],
    direction: str,
    smc: SmcResult,
    pa: PriceActionResult,
    confirmation_count: int,
    min_confirmations: int,
    lookback: int = 20,
    edge_atr_distance: float = 0.25,
    strict_filters: bool = True,
) -> RangeContext:
    """Evaluate the explicit range playbook using only closed candles.

    The range is measured from the candles before the signal candle so a
    breakout candle cannot move the boundary and accidentally qualify itself.
    When ``strict_filters`` is false, the range context is still calculated
    for telemetry and capital sizing, but Option 2's edge/sweep/reversal and
    confirmation gates are not entry blockers.
    """
    neutral = RangeContext(
        valid=False,
        direction="NEUTRAL",
        support=0.0,
        resistance=0.0,
        atr=0.0,
        edge_distance=0.0,
        location="UNKNOWN",
        liquidity_sweep=False,
        reversal_candle=False,
        confirmation_count=confirmation_count,
        reason="Insufficient candles for RANGE evaluation",
    )
    if direction not in {"BUY", "SELL"} or len(candles) < lookback + 1:
        return neutral

    history = candles[-(lookback + 1):-1]
    signal = candles[-1]
    support = min(c.low for c in history)
    resistance = max(c.high for c in history)
    atr = _atr(candles)
    if atr <= 0.0 or resistance <= support:
        return neutral

    edge_distance = atr * edge_atr_distance
    near_support = signal.low <= support + edge_distance
    near_resistance = signal.high >= resistance - edge_distance
    if near_support and not near_resistance:
        location: RangeLocation = "SUPPORT"
    elif near_resistance and not near_support:
        location = "RESISTANCE"
    elif near_support and near_resistance:
        location = "SUPPORT" if direction == "BUY" else "RESISTANCE"
    else:
        location = "MIDDLE"

    sweep = _latest_sweep(smc, direction, len(candles) - 1)
    reversal = _reversal_candle(pa, direction)
    correct_edge = location == ("SUPPORT" if direction == "BUY" else "RESISTANCE")
    if not strict_filters:
        valid = True
        reason = (
            f"RANGE {direction} accepted: strict Option 2 filters disabled "
            "(edge/sweep/reversal/confirmation telemetry retained)"
        )
    else:
        valid = (
            correct_edge
            and sweep
            and reversal
            and confirmation_count >= min_confirmations
        )

        if not correct_edge:
            reason = (
                f"RANGE entry blocked: price is in the {location.lower()}, "
                f"not at the {('support' if direction == 'BUY' else 'resistance')} edge"
            )
        elif not sweep:
            reason = "RANGE entry blocked: no fresh same-direction Liquidity Sweep"
        elif not reversal:
            reason = "RANGE entry blocked: no closed-candle reversal pattern"
        elif confirmation_count < min_confirmations:
            reason = (
                f"RANGE entry blocked: {confirmation_count}/{min_confirmations} "
                "confirmations"
            )
        else:
            reason = (
                f"RANGE {direction} accepted: edge + Liquidity Sweep + reversal "
                f"+ {confirmation_count} confirmations"
            )

    return RangeContext(
        valid=valid,
        direction=direction if valid else "NEUTRAL",  # type: ignore[arg-type]
        support=round(support, 2),
        resistance=round(resistance, 2),
        atr=round(atr, 5),
        edge_distance=round(edge_distance, 5),
        location=location,
        liquidity_sweep=sweep,
        reversal_candle=reversal,
        confirmation_count=confirmation_count,
        reason=reason,
    )
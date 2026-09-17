"""Adaptive ATR trailing-stop calculations.

The module is intentionally independent from broker I/O and strategy engines.
It accepts any candle objects (or dictionaries) exposing open/high/low/close
and, optionally, volume.  The live loop owns persistence, logging, and broker
modification requests.

The trail uses ATR for its normal distance and switches to a smaller distance
only after the trade has earned enough favourable movement and at least two of
these independent signals agree:

* consecutive contraction of recent candle bodies,
* momentum weakening through MACD histogram or directional RSI slope,
* declining volume when volume data is available.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence


@dataclass
class AdaptiveTrailingConfig:
    enabled: bool = True
    atr_period: int = 14
    normal_multiplier: float = 2.0
    tight_multiplier: float = 0.9
    exhaustion_confirm_count: int = 2
    momentum_lookback: int = 3
    body_lookback: int = 3
    body_baseline_period: int = 10
    body_shrink_ratio: float = 0.8
    volume_baseline_period: int = 10
    volume_shrink_ratio: float = 0.8
    min_profit_atr_multiple: float = 1.5
    min_tight_distance_atr: float = 1.0
    min_step_price: float = 0.05


@dataclass(frozen=True)
class ExhaustionSignals:
    candle_body_contraction: bool
    momentum_weakening: bool
    volume_declining: bool
    active: tuple[str, ...]

    @property
    def active_count(self) -> int:
        return len(self.active)


@dataclass(frozen=True)
class AdaptiveTrailDecision:
    mode: str
    multiplier: float
    atr: float
    distance: float
    candidate_sl: Optional[float]
    exhaustion: ExhaustionSignals
    floating_profit_price: float
    floating_profit_atr_multiple: float
    tightening_eligible: bool


def _read(candle: Any, name: str, default: float = 0.0) -> float:
    value = candle.get(name, default) if isinstance(candle, Mapping) else getattr(candle, name, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ema(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    period = max(1, int(period))
    if len(values) < period:
        return list(values)
    alpha = 2.0 / (period + 1.0)
    result = [0.0] * len(values)
    current = sum(values[:period]) / period
    for index in range(period):
        result[index] = current
    for index in range(period, len(values)):
        current = values[index] * alpha + current * (1.0 - alpha)
        result[index] = current
    return result


def atr(candles: Sequence[Any], period: int = 14) -> float:
    """Return Wilder ATR in the instrument's price units."""
    if len(candles) < 2:
        return 0.0
    true_ranges: list[float] = []
    for index in range(1, len(candles)):
        current = candles[index]
        previous = candles[index - 1]
        high = _read(current, "high")
        low = _read(current, "low")
        previous_close = _read(previous, "close")
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    if not true_ranges:
        return 0.0
    period = max(1, int(period))
    seed_size = min(period, len(true_ranges))
    value = sum(true_ranges[:seed_size]) / seed_size
    for true_range in true_ranges[seed_size:]:
        value = ((value * (period - 1)) + true_range) / period
    return round(max(0.0, value), 8)


def _rsi_series(closes: Sequence[float], period: int = 14) -> list[float]:
    result = [50.0] * len(closes)
    period = max(1, int(period))
    if len(closes) <= period:
        return result
    gains = [max(closes[index] - closes[index - 1], 0.0) for index in range(1, len(closes))]
    losses = [max(closes[index - 1] - closes[index], 0.0) for index in range(1, len(closes))]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period

    def value() -> float:
        if average_loss == 0:
            return 100.0
        return 100.0 - (100.0 / (1.0 + average_gain / average_loss))

    result[period] = value()
    for index in range(period, len(gains)):
        average_gain = ((average_gain * (period - 1)) + gains[index]) / period
        average_loss = ((average_loss * (period - 1)) + losses[index]) / period
        result[index + 1] = value()
    return result


def _directional_momentum_weakening(
    closes: Sequence[float],
    direction: str,
    lookback: int,
) -> bool:
    """Return True when MACD or RSI is weakening against the trade direction."""
    if len(closes) < 35:
        return False
    lookback = max(2, int(lookback))
    fast = _ema(closes, 12)
    slow = _ema(closes, 26)
    macd = [fast[index] - slow[index] for index in range(len(closes))]
    signal = _ema(macd[25:], 9)
    histogram = [0.0] * 25 + [
        macd[index + 25] - signal[index] for index in range(len(signal))
    ]
    recent_histogram = histogram[-lookback:]
    rsi = _rsi_series(closes, 14)
    recent_rsi = rsi[-lookback:]
    if len(recent_histogram) < 2 or len(recent_rsi) < 2:
        return False

    hist_slope = recent_histogram[-1] - recent_histogram[0]
    rsi_slope = recent_rsi[-1] - recent_rsi[0]
    if str(direction).upper() == "BUY":
        return hist_slope < 0.0 or rsi_slope < 0.0
    return hist_slope > 0.0 or rsi_slope > 0.0


def detect_exhaustion(
    candles: Sequence[Any],
    direction: str,
    cfg: AdaptiveTrailingConfig,
) -> ExhaustionSignals:
    """Evaluate the three independent exhaustion signal families."""
    body_lookback = max(2, int(cfg.body_lookback))
    body_baseline_period = max(1, int(cfg.body_baseline_period))
    bodies = [abs(_read(candle, "close") - _read(candle, "open")) for candle in candles]
    body_signal = False
    minimum_body_bars = body_lookback + body_baseline_period
    if len(bodies) >= minimum_body_bars:
        recent = bodies[-body_lookback:]
        baseline = bodies[-minimum_body_bars:-body_lookback]
        baseline_average = sum(baseline) / len(baseline)
        body_signal = (
            baseline_average > 0.0
            and all(recent[index] <= recent[index - 1] for index in range(1, len(recent)))
            and sum(recent) / len(recent) <= baseline_average * cfg.body_shrink_ratio
        )

    closes = [_read(candle, "close") for candle in candles]
    momentum_signal = _directional_momentum_weakening(
        closes, direction, cfg.momentum_lookback
    )

    volume_values = [_read(candle, "volume") for candle in candles]
    volume_lookback = body_lookback
    volume_baseline_period = max(1, int(cfg.volume_baseline_period))
    volume_signal = False
    if len(volume_values) >= volume_lookback + volume_baseline_period:
        recent_volume = volume_values[-volume_lookback:]
        baseline_volume = volume_values[-(volume_lookback + volume_baseline_period):-volume_lookback]
        baseline_average = sum(baseline_volume) / len(baseline_volume)
        recent_average = sum(recent_volume) / len(recent_volume)
        # All-zero or missing broker volume is not evidence of exhaustion.
        volume_signal = (
            baseline_average > 0.0
            and recent_average <= baseline_average * cfg.volume_shrink_ratio
        )

    active: list[str] = []
    if body_signal:
        active.append("candle_body_contraction")
    if momentum_signal:
        active.append("momentum_weakening")
    if volume_signal:
        active.append("volume_declining")
    return ExhaustionSignals(
        candle_body_contraction=body_signal,
        momentum_weakening=momentum_signal,
        volume_declining=volume_signal,
        active=tuple(active),
    )


def compute_adaptive_trail(
    direction: str,
    current_price: float,
    candles: Sequence[Any],
    cfg: AdaptiveTrailingConfig,
    entry_price: Optional[float] = None,
) -> AdaptiveTrailDecision:
    """Compute a ratchetable stop candidate from the latest closed candles."""
    current_atr = atr(candles, cfg.atr_period)
    exhaustion = detect_exhaustion(candles, direction, cfg)
    is_buy = str(direction).upper() == "BUY"
    floating_profit_price = 0.0
    if entry_price is not None:
        favorable_move = (
            float(current_price) - float(entry_price)
            if is_buy
            else float(entry_price) - float(current_price)
        )
        floating_profit_price = round(max(0.0, favorable_move), 8)
    floating_profit_atr_multiple = (
        floating_profit_price / current_atr if current_atr > 0.0 else 0.0
    )

    # Tightening is intentionally never configurable below 2-of-3.  A single
    # weakening momentum signal is not enough to pull the stop closer.
    required_confirmations = max(2, min(3, int(cfg.exhaustion_confirm_count)))
    enough_confirmation = exhaustion.active_count >= required_confirmations
    enough_profit = (
        current_atr > 0.0
        and floating_profit_atr_multiple
        >= max(0.0, float(cfg.min_profit_atr_multiple))
    )
    tightening_eligible = enough_confirmation and enough_profit
    mode = "TIGHTENING" if tightening_eligible else "NORMAL"
    raw_multiplier = cfg.tight_multiplier if tightening_eligible else cfg.normal_multiplier
    # Keep an absolute ATR floor in tightening mode, even if a smaller
    # tight_multiplier is configured.
    multiplier = (
        max(raw_multiplier, float(cfg.min_tight_distance_atr))
        if tightening_eligible
        else raw_multiplier
    )
    distance = current_atr * multiplier
    candidate = None
    if current_atr > 0.0 and distance > 0.0:
        candidate = round(current_price - distance if is_buy else current_price + distance, 8)
    return AdaptiveTrailDecision(
        mode=mode,
        multiplier=multiplier,
        atr=current_atr,
        distance=round(distance, 8),
        candidate_sl=candidate,
        exhaustion=exhaustion,
        floating_profit_price=floating_profit_price,
        floating_profit_atr_multiple=round(floating_profit_atr_multiple, 8),
        tightening_eligible=tightening_eligible,
    )


def should_apply(
    direction: str,
    current_sl: float,
    candidate_sl: Optional[float],
    min_step_price: float,
) -> bool:
    """Only permit stop movement in the trade's favourable direction."""
    if candidate_sl is None:
        return False
    if str(direction).upper() == "BUY":
        return candidate_sl - current_sl >= min_step_price
    return current_sl - candidate_sl >= min_step_price
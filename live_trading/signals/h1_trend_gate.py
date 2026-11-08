"""Pure H1 EMA bias detection and counter-trend entry checks."""

import math
from typing import Sequence

from live_trading.signals.gold_engine import OHLCV, calc_ema

H1_MIN_CANDLES = 210
H1_BIAS_UNAVAILABLE = "UNAVAILABLE"


def detect_h1_bias(candles: Sequence[OHLCV]) -> str:
    """Classify the latest closed H1 candle against EMA50 and EMA200."""
    if len(candles) < H1_MIN_CANDLES:
        return H1_BIAS_UNAVAILABLE
    try:
        closes = [float(candle.close) for candle in candles]
    except (AttributeError, TypeError, ValueError, OverflowError):
        return H1_BIAS_UNAVAILABLE
    if not all(math.isfinite(close) and close > 0 for close in closes):
        return H1_BIAS_UNAVAILABLE

    close = closes[-1]
    ema50 = calc_ema(closes, 50)
    ema200 = calc_ema(closes, 200)
    if not all(math.isfinite(value) for value in (close, ema50, ema200)):
        return H1_BIAS_UNAVAILABLE
    if close > ema50 > ema200:
        return "BULL"
    if close < ema50 < ema200:
        return "BEAR"
    return "NEUTRAL"


def evaluate_h1_trend_gate(
    direction: str,
    bias: str,
    strictness: str = "counter",
    *,
    unavailable_reason: str = "",
) -> tuple[bool, list[str]]:
    """Return whether a BUY/SELL direction agrees with the requested strictness."""
    normalized_direction = str(direction or "").strip().upper()
    normalized_bias = str(bias or "").strip().upper()
    normalized_strictness = str(strictness or "").strip().lower()

    if normalized_bias == H1_BIAS_UNAVAILABLE:
        return False, [unavailable_reason or "H1 trend data unavailable"]
    if normalized_bias not in {"BULL", "BEAR", "NEUTRAL"}:
        return False, ["H1 trend bias is invalid"]
    if normalized_direction not in {"BUY", "SELL"}:
        return False, ["Entry direction is invalid for H1 trend gate"]
    if normalized_strictness not in {"counter", "aligned"}:
        return False, ["H1 gate strictness is invalid"]
    if normalized_bias == "NEUTRAL":
        if normalized_strictness == "aligned":
            return False, ["H1 trend is NEUTRAL; aligned mode requires BULL or BEAR"]
        return True, []
    if normalized_direction == "BUY" and normalized_bias == "BEAR":
        return False, ["BUY is counter-trend against BEAR H1 bias"]
    if normalized_direction == "SELL" and normalized_bias == "BULL":
        return False, ["SELL is counter-trend against BULL H1 bias"]
    return True, []

"""
Divergence Detection Engine — GoldScalperPro v4.

Detects classic RSI and MACD divergences between price and momentum
oscillators.  Divergences are one of the strongest reversal / continuation
signals in gold scalping because they expose hidden exhaustion that raw
price action misses.

Classic Bullish Divergence : price → lower low,  oscillator → higher low  → BUY
Classic Bearish Divergence : price → higher high, oscillator → lower high → SELL
Hidden  Bullish Divergence : price → higher low,  RSI → lower low          → BUY  (trend continuation)
Hidden  Bearish Divergence : price → lower high,   RSI → higher high        → SELL (trend continuation)

Scoring (returned as DivergenceResult.score, 0–10):
  10 : RSI AND MACD divergences agree
   7 : RSI divergence only (higher weight — RSI is more reliable on gold M5)
   5 : MACD divergence only
   0 : NEUTRAL (no divergence detected)

Requires at least 40 OHLCV bars (uses last 30 for pivot detection).
Returns NEUTRAL on insufficient data — never raises.
"""

from dataclasses import dataclass
from typing import List, Literal
from live_trading.signals.gold_engine import OHLCV, calc_rsi_array, calc_macd_array

# ── Config ────────────────────────────────────────────────────────────────────
_LOOKBACK  = 30   # bars to scan for divergence pivots
_PIVOT_GAP = 5    # minimum bars between two pivot points


# ── Internal pivot detectors ──────────────────────────────────────────────────

def _pivot_lows(values: List[float], gap: int) -> List[int]:
    """Return indices of local minima (each separated by at least `gap` bars)."""
    pivots: List[int] = []
    n = len(values)
    for i in range(gap, n - gap):
        if all(values[i] <= values[i - j] and values[i] <= values[i + j]
               for j in range(1, gap + 1)):
            if not pivots or i - pivots[-1] >= gap:
                pivots.append(i)
    return pivots


def _pivot_highs(values: List[float], gap: int) -> List[int]:
    """Return indices of local maxima (each separated by at least `gap` bars)."""
    pivots: List[int] = []
    n = len(values)
    for i in range(gap, n - gap):
        if all(values[i] >= values[i - j] and values[i] >= values[i + j]
               for j in range(1, gap + 1)):
            if not pivots or i - pivots[-1] >= gap:
                pivots.append(i)
    return pivots


# ── Single-oscillator divergence ──────────────────────────────────────────────

def _divergence(
    price_slice: List[float],
    osc_slice:   List[float],
    gap:         int,
) -> Literal["BULLISH", "BEARISH", "NEUTRAL"]:
    """
    Detect the most recent classic divergence between a price series and an
    oscillator of equal length.  Both slices must be the same length.
    """
    if len(price_slice) != len(osc_slice) or len(price_slice) < gap * 2 + 3:
        return "NEUTRAL"

    # ── Bullish: price lower low + oscillator higher low ──────────────────
    p_lows = _pivot_lows(price_slice, gap)
    o_lows = _pivot_lows(osc_slice,   gap)
    if len(p_lows) >= 2 and len(o_lows) >= 2:
        p1, p2 = p_lows[-2], p_lows[-1]
        o1, o2 = o_lows[-2], o_lows[-1]
        if (price_slice[p2] < price_slice[p1]   # price: lower low
                and osc_slice[o2] > osc_slice[o1]):  # osc:   higher low
            return "BULLISH"

    # ── Bearish: price higher high + oscillator lower high ────────────────
    p_highs = _pivot_highs(price_slice, gap)
    o_highs = _pivot_highs(osc_slice,   gap)
    if len(p_highs) >= 2 and len(o_highs) >= 2:
        p1, p2 = p_highs[-2], p_highs[-1]
        o1, o2 = o_highs[-2], o_highs[-1]
        if (price_slice[p2] > price_slice[p1]   # price: higher high
                and osc_slice[o2] < osc_slice[o1]):  # osc:   lower high
            return "BEARISH"

    return "NEUTRAL"


# ── Public API ────────────────────────────────────────────────────────────────

@dataclass
class DivergenceResult:
    rsi_signal:  Literal["BULLISH", "BEARISH", "NEUTRAL"]
    macd_signal: Literal["BULLISH", "BEARISH", "NEUTRAL"]
    signal:      Literal["BULLISH", "BEARISH", "NEUTRAL"]   # combined verdict
    score:       float   # 0–10 (added to confidence as a bonus)


def analyze_divergence(candles: List[OHLCV]) -> DivergenceResult:
    """
    Compute RSI (14) and MACD (12/26/9) divergences from the last LOOKBACK
    bars of `candles`.  Requires at least 40 bars; returns NEUTRAL if fewer.
    """
    _NEUTRAL = DivergenceResult("NEUTRAL", "NEUTRAL", "NEUTRAL", 0.0)

    if len(candles) < 40:
        return _NEUTRAL

    closes = [c.close for c in candles]

    # Compute full indicator arrays then slice the last LOOKBACK bars
    try:
        rsi_arr           = calc_rsi_array(closes, 14)
        macd_arr, _, _    = calc_macd_array(closes, 12, 26, 9)
    except Exception:
        return _NEUTRAL

    if len(rsi_arr) < _LOOKBACK or len(macd_arr) < _LOOKBACK:
        return _NEUTRAL

    price_slice = closes[-_LOOKBACK:]
    rsi_slice   = rsi_arr[-_LOOKBACK:]
    macd_slice  = macd_arr[-_LOOKBACK:]

    rsi_sig  = _divergence(price_slice, rsi_slice,  _PIVOT_GAP)
    macd_sig = _divergence(price_slice, macd_slice, _PIVOT_GAP)

    # Combine signals
    if rsi_sig != "NEUTRAL" and macd_sig != "NEUTRAL" and rsi_sig == macd_sig:
        signal = rsi_sig
        score  = 10.0   # both agree — high conviction
    elif rsi_sig != "NEUTRAL":
        signal = rsi_sig
        score  = 7.0    # RSI only — strong on gold M5
    elif macd_sig != "NEUTRAL":
        signal = macd_sig
        score  = 5.0    # MACD only — moderate
    else:
        signal = "NEUTRAL"
        score  = 0.0

    return DivergenceResult(
        rsi_signal=rsi_sig,
        macd_signal=macd_sig,
        signal=signal,
        score=score,
    )

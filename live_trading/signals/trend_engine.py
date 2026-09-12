
"""
Adaptive Trend Engine — multi-factor EMA, momentum, slope and regime analysis.

The old engine only compared price with EMA50 and EMA100. This version keeps
that public contract but adds ATR-normalized structure, EMA slopes, RSI, ADX,
and pullback awareness so a healthy trend is not lost during a retracement.
"""
from dataclasses import dataclass
from typing import List, Literal
from live_trading.signals.gold_engine import OHLCV, calc_atr, calc_ema, calc_rsi


TrendState = Literal["TRENDING", "PULLBACK", "DEVELOPING", "TRANSITION", "RANGE"]


@dataclass
class TrendResult:
    ema50: float
    ema100: float
    ema200: float
    trend: Literal["BULLISH", "BEARISH", "NEUTRAL"]
    strength: Literal["STRONG", "MODERATE", "WEAK"]
    ema20: float = 0.0
    adx: float = 0.0
    slope50: float = 0.0
    slope100: float = 0.0
    rsi: float = 50.0
    score: float = 0.0
    quality: float = 0.0
    state: TrendState = "RANGE"
    pullback: bool = False


def _ema_series(closes: List[float], period: int) -> List[float]:
    if not closes:
        return []
    if len(closes) < period:
        return [closes[-1]] * len(closes)
    values = [0.0] * len(closes)
    ema = sum(closes[:period]) / period
    values[period - 1] = ema
    k = 2.0 / (period + 1)
    for index in range(period, len(closes)):
        ema = closes[index] * k + ema * (1.0 - k)
        values[index] = ema
    for index in range(period - 1):
        values[index] = values[period - 1]
    return values


def _signed_component(value: float, scale: float, weight: float) -> float:
    if scale <= 0:
        return 0.0
    normalized = max(-1.0, min(1.0, value / scale))
    return normalized * weight


def _calc_adx(candles: List[OHLCV], period: int = 14) -> float:
    if len(candles) < period * 2:
        return 20.0
    trs: List[float] = []
    dm_plus: List[float] = []
    dm_minus: List[float] = []
    for index in range(1, len(candles)):
        current, previous = candles[index], candles[index - 1]
        trs.append(max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ))
        up = current.high - previous.high
        down = previous.low - current.low
        dm_plus.append(up if up > down and up > 0 else 0.0)
        dm_minus.append(down if down > up and down > 0 else 0.0)

    tr_sum = sum(trs[:period])
    plus_sum = sum(dm_plus[:period])
    minus_sum = sum(dm_minus[:period])
    dx_values: List[float] = []
    for index in range(period, len(trs)):
        tr_sum = tr_sum - tr_sum / period + trs[index]
        plus_sum = plus_sum - plus_sum / period + dm_plus[index]
        minus_sum = minus_sum - minus_sum / period + dm_minus[index]
        plus_di = 100.0 * plus_sum / tr_sum if tr_sum > 0 else 0.0
        minus_di = 100.0 * minus_sum / tr_sum if tr_sum > 0 else 0.0
        total = plus_di + minus_di
        dx_values.append(
            100.0 * abs(plus_di - minus_di) / total if total > 0 else 0.0
        )
    if len(dx_values) < period:
        return 20.0
    return round(sum(dx_values[-period:]) / period, 2)


def _neutral_result(candles: List[OHLCV]) -> TrendResult:
    last = candles[-1].close if candles else 0.0
    return TrendResult(
        ema50=last,
        ema100=last,
        ema200=last,
        trend="NEUTRAL",
        strength="WEAK",
        ema20=last,
        state="RANGE",
    )


def analyze_trend(candles: List[OHLCV]) -> TrendResult:
    closes = [c.close for c in candles]
    if len(closes) < 210:
        return _neutral_result(candles)

    price = closes[-1]
    atr = max(calc_atr(candles, 14), 1e-6)
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, 50)
    ema100 = calc_ema(closes, 100)
    ema200 = calc_ema(closes, 200)

    series50 = _ema_series(closes, 50)
    series100 = _ema_series(closes, 100)
    slope50 = (series50[-1] - series50[-11]) / atr
    slope100 = (series100[-1] - series100[-11]) / atr
    rsi = calc_rsi(closes, 14)
    adx = _calc_adx(candles, 14)

    # The score is signed from -100 to +100. It combines structure, distance,
    # slope and momentum instead of trusting one closing-price crossover.
    score = (
        _signed_component(price - ema50, atr * 1.25, 15.0)
        + _signed_component(ema20 - ema50, atr * 1.00, 10.0)
        + _signed_component(ema50 - ema100, atr * 1.50, 25.0)
        + _signed_component(ema100 - ema200, atr * 2.00, 20.0)
        + _signed_component(slope50, 0.75, 15.0)
        + _signed_component(slope100, 0.50, 10.0)
        + _signed_component(rsi - 50.0, 20.0, 5.0)
    )
    score = round(max(-100.0, min(100.0, score)), 1)
    quality = round(min(100.0, abs(score) * 0.8 + max(0.0, adx - 15.0) * 1.5), 1)

    bullish_structure = ema50 > ema100 > ema200
    bearish_structure = ema50 < ema100 < ema200
    bullish_core = price > ema50 and bullish_structure
    bearish_core = price < ema50 and bearish_structure

    # A shallow retracement should not erase a valid higher-timeframe trend.
    # It is still bounded by EMA100 and ATR so a genuine reversal becomes a
    # transition instead of being mislabeled as a pullback.
    bullish_pullback = (
        bullish_structure
        and slope50 > 0.0
        and slope100 > 0.0
        and price >= ema100 - 1.25 * atr
        and price <= ema50 + 0.75 * atr
        and score >= 8.0
    )
    bearish_pullback = (
        bearish_structure
        and slope50 < 0.0
        and slope100 < 0.0
        and price <= ema100 + 1.25 * atr
        and price >= ema50 - 0.75 * atr
        and score <= -8.0
    )

    if (bullish_core and score >= 25.0) or bullish_pullback:
        trend = "BULLISH"
        is_pullback = not bullish_core
    elif (bearish_core and score <= -25.0) or bearish_pullback:
        trend = "BEARISH"
        is_pullback = not bearish_core
    else:
        trend = "NEUTRAL"
        is_pullback = False

    direction_score = abs(score)
    if trend != "NEUTRAL" and direction_score >= 55.0 and adx >= 25.0:
        strength = "STRONG"
    elif trend != "NEUTRAL" and direction_score >= 30.0 and adx >= 18.0:
        strength = "MODERATE"
    else:
        strength = "WEAK"

    if trend != "NEUTRAL":
        state = "PULLBACK" if is_pullback else (
            "TRENDING" if strength == "STRONG" else "DEVELOPING"
        )
    elif abs(score) >= 20.0:
        state = "TRANSITION"
    else:
        state = "RANGE"

    return TrendResult(
        ema50=ema50,
        ema100=ema100,
        ema200=ema200,
        trend=trend,
        strength=strength,
        ema20=ema20,
        adx=adx,
        slope50=round(slope50, 3),
        slope100=round(slope100, 3),
        rsi=rsi,
        score=score,
        quality=quality,
        state=state,
        pullback=is_pullback,
    )

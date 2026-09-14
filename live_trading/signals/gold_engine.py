"""
Gold Engine — EMA, ATR, RSI, Bollinger Bands
Ported from goldEngine.ts
"""
from typing import List
from dataclasses import dataclass


@dataclass
class OHLCV:
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float


def calc_ema(closes: List[float], period: int) -> float:
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 4)


def calc_ema_array(closes: List[float], period: int) -> List[float]:
    """Returns EMA value at every bar (same length as closes)."""
    if len(closes) < period:
        return closes[:]
    k = 2.0 / (period + 1)
    result = [0.0] * len(closes)
    ema = sum(closes[:period]) / period
    for i in range(period):
        result[i] = ema
    for i in range(period, len(closes)):
        ema = closes[i] * k + ema * (1 - k)
        result[i] = ema
    return result


def calc_atr(candles: List[OHLCV], period: int = 14) -> float:
    """Returns the most recent ATR value."""
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low,
                       abs(c.high - p.close),
                       abs(c.low - p.close)))
    if not trs:
        return 0.0
    # Wilder smoothing
    if len(trs) < period:
        return sum(trs) / len(trs)
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return round(atr, 4)


def calc_atr_array(candles: List[OHLCV], period: int = 14) -> List[float]:
    """Returns ATR at every bar."""
    result = [0.0] * len(candles)
    if len(candles) < 2:
        return result
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low,
                       abs(c.high - p.close),
                       abs(c.low - p.close)))
    if not trs:
        return result
    atr = sum(trs[:period]) / min(period, len(trs))
    for i, tr in enumerate(trs):
        if i >= period:
            atr = (atr * (period - 1) + tr) / period
        result[i + 1] = atr
    return result


# ── RSI ───────────────────────────────────────────────────────────────────────

def calc_rsi(closes: List[float], period: int = 14) -> float:
    """Returns the most recent RSI value (0-100). Uses Wilder smoothing."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def calc_rsi_array(closes: List[float], period: int = 14) -> List[float]:
    """Returns RSI value at every bar (same length as closes)."""
    n = len(closes)
    result = [50.0] * n
    if n < period + 1:
        return result
    gains  = [max(closes[i] - closes[i-1], 0.0) for i in range(1, n)]
    losses = [max(closes[i-1] - closes[i], 0.0) for i in range(1, n)]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    def _rsi(ag, al):
        if al == 0: return 100.0
        return round(100.0 - 100.0 / (1.0 + ag / al), 2)
    result[period] = _rsi(avg_g, avg_l)
    for i in range(period, n - 1):
        avg_g = (avg_g * (period - 1) + gains[i])  / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        result[i + 1] = _rsi(avg_g, avg_l)
    return result


# ── MACD ──────────────────────────────────────────────────────────────────────

def calc_macd_array(
    closes: List[float],
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
):
    """
    Returns (macd_line, signal_line, histogram) — three lists of equal length.
    All lists are the same length as `closes`. Leading entries are 0.0.
    """
    n = len(closes)
    ema_fast = calc_ema_array(closes, fast)
    ema_slow = calc_ema_array(closes, slow)
    macd_line = [ema_fast[i] - ema_slow[i] for i in range(n)]
    # Signal line: EMA of macd_line (skip leading zeros)
    signal_line = [0.0] * n
    first_valid = slow - 1  # first bar where both EMAs are defined
    if n > first_valid + signal_period:
        macd_valid = macd_line[first_valid:]
        sig_ema = calc_ema_array(macd_valid, signal_period)
        for i, v in enumerate(sig_ema):
            signal_line[first_valid + i] = v
    histogram = [macd_line[i] - signal_line[i] for i in range(n)]
    return macd_line, signal_line, histogram


def calc_macd(
    closes: List[float],
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
):
    """Returns (macd_value, signal_value, histogram_value) for the last bar."""
    ml, sl, hl = calc_macd_array(closes, fast, slow, signal_period)
    return ml[-1], sl[-1], hl[-1]

"""
Quality Filter — 10-category final gate.
Ported from qualityFilter.ts
"""
from dataclasses import dataclass, field
from typing import List, Literal, Optional
from datetime import datetime, timezone
from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.market_regime import calc_adx
from live_trading.config import CONF_HARD_MIN, QUALITY_ADX_MIN, STRUCTURE_MAX_AGE_BARS

# The robot may evaluate valid setups throughout the full UTC day.  Session
# quality remains useful for confidence scoring, but it is informational only:
# there is no "dead zone" that can block an otherwise valid entry.
ALLOWED_SESSIONS = [
    (0,  7,  "MODERATE"),
    (7, 17,  "PRIME"),
    (17, 24, "MODERATE"),
]

LATE_EXTENSION_MULT = 10.0
MOMENTUM_BARS       = 5
# Backwards-compatible alias; the max age is now bounded and configurable.
STALE_BAR_COUNT     = STRUCTURE_MAX_AGE_BARS


@dataclass
class QualityFilterResult:
    allowed: bool
    blocked_reasons: List[str]
    session_quality: Literal["PRIME", "MODERATE", "BLOCKED"]
    adx: float
    is_severe_range: bool
    is_late_entry: bool
    is_low_probability: bool
    is_fake_breakout: bool
    is_weak_volume: bool
    is_low_momentum: bool
    # Retained for state/panel compatibility. News is intentionally inactive
    # on the live entry path.
    is_news_blocked: bool = False


def get_session_quality(timestamp: object) -> Literal["PRIME", "MODERATE", "BLOCKED"]:
    """Classify a candle timestamp in UTC without rejecting valid datetime objects."""
    try:
        if isinstance(timestamp, datetime):
            dt = timestamp
        elif isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
            epoch = float(timestamp)
            if epoch > 100_000_000_000:
                epoch /= 1000.0
            dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        else:
            text = str(timestamp).strip()
            if not text:
                return "BLOCKED"
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return "BLOCKED"
    hour = dt.hour
    for start, end, quality in ALLOWED_SESSIONS:
        if start <= hour < end:
            return quality  # type: ignore
    return "BLOCKED"
def _calc_ema50(closes: List[float]) -> float:
    period = 50
    if len(closes) < period:
        return closes[-1]
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = p * k + ema * (1 - k)
    return ema


def _is_volatility_compressed(candles: List[OHLCV], lookback: int = 20, threshold: float = 0.65) -> bool:
    if len(candles) < lookback + 2:
        return False
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    curr_atr = trs[-1]
    mean_atr = sum(trs[-lookback - 1:-1]) / lookback
    return mean_atr > 0 and curr_atr < mean_atr * threshold


def _is_severe_range(candles: List[OHLCV], adx: float) -> bool:
    if adx >= 14:
        return False
    if not _is_volatility_compressed(candles, 20, 0.50):
        return False
    sl = candles[-15:]
    highest = max(c.high for c in sl)
    lowest  = min(c.low  for c in sl)
    recent_range = highest - lowest
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    mean_atr = sum(trs[-20:]) / 20 if len(trs) >= 20 else sum(trs) / len(trs)
    return recent_range < mean_atr * 2.5


def _is_late_entry(candles: List[OHLCV], last_bos_bar: Optional[int]) -> bool:
    n      = len(candles)
    closes = [c.close for c in candles]
    price  = closes[-1]
    curr   = candles[-1]
    prev   = candles[-2]
    atr = max(curr.high - curr.low,
              abs(curr.high - prev.close),
              abs(curr.low  - prev.close))
    if abs(price - _calc_ema50(closes)) > LATE_EXTENSION_MULT * atr:
        return True
    if n >= MOMENTUM_BARS + 1:
        recent   = candles[-MOMENTUM_BARS:]
        all_bull = all(c.close > c.open for c in recent)
        all_bear = all(c.close < c.open for c in recent)
        if all_bull or all_bear:
            bodies    = [abs(c.close - c.open) for c in recent]
            shrinking = all(bodies[i] <= bodies[i - 1] for i in range(1, len(bodies)))
            if shrinking:
                return True
    if last_bos_bar is not None and (n - 1) - last_bos_bar > STRUCTURE_MAX_AGE_BARS:
        return True
    return False


def _is_weak_volume(candles: List[OHLCV]) -> bool:
    if len(candles) < 22:
        return False
    avg_vol  = sum(c.volume for c in candles[-21:-1]) / 20
    curr_vol = candles[-1].volume
    return avg_vol > 0 and curr_vol < avg_vol * 0.35


def apply_quality_filter(
    candles: List[OHLCV],
    smc_signal: str,
    confidence: float,
    last_bos_bar: Optional[int],
    adx: Optional[float] = None,
    atr_ratio: Optional[float] = None,
    news_blocked: bool = False,
    news_reason: str = "",
    allow_without_smc: bool = False,
) -> QualityFilterResult:
    blocked = QualityFilterResult(
        allowed=False, blocked_reasons=[],
        session_quality="BLOCKED", adx=0.0,
        is_severe_range=False, is_late_entry=False,
        is_low_probability=False, is_fake_breakout=False,
        is_weak_volume=False, is_low_momentum=False,
    )

    if len(candles) < 30:
        blocked.blocked_reasons = ["Insufficient candle data (< 30)"]
        return blocked
    # The ordinary entry filter is N-of-4, but a legacy hard gate used to
    # discard PA+Trend/Wyckoff setups whenever SMC was neutral.  Only the
    # decision engine can explicitly open this path, and only when PA itself
    # is one of the aligned confirmations.
    if smc_signal == "NEUTRAL" and not allow_without_smc:
        blocked.blocked_reasons = ["No SMC direction signal"]
        return blocked
    reasons = []
    last_candle = candles[-1]

    # Option 3: News Filter is intentionally not evaluated on the entry path.
    # Keep these legacy parameters so older callers remain source-compatible;
    # the values are deliberately ignored. The news module remains available
    # for non-entry telemetry/UI use.

    # Session quality is informational and never blocks valid setups.  Valid
    # timestamps are covered by ALLOWED_SESSIONS for all 24 UTC hours.  Keep
    # malformed timestamps fail-closed so bad market data cannot authorize an
    # order.
    session = get_session_quality(last_candle.time)
    if session == "BLOCKED":
        reasons.append("Invalid candle timestamp — cannot determine session")

    adx_val   = adx if adx is not None else calc_adx(candles)
    sev_range = _is_severe_range(candles, adx_val)
    if sev_range:
        reasons.append("Severe range / compressed volatility — ADX too low")

    late = _is_late_entry(candles, last_bos_bar)
    if late:
        reasons.append("Late entry: price over-extended from EMA50 or BOS/CHoCH is stale")

    # ADX momentum check — configurable via QUALITY_ADX_MIN env var (default 15).
    # Set higher (e.g. 20) for stricter momentum confirmation,
    # set lower (e.g. 10) to allow weaker-trend entries.
    low_mom = adx_val < QUALITY_ADX_MIN
    if low_mom:
        reasons.append(
            f"Low momentum: ADX {adx_val:.1f} < {QUALITY_ADX_MIN:.0f} (QUALITY_ADX_MIN)"
        )

    weak_vol = _is_weak_volume(candles)
    # Note: volume filter is informational only — MT5 tick volume is a proxy,
    # not real market depth, so we log the flag but do not block on it.

    low_prob = confidence < CONF_HARD_MIN
    if low_prob:
        reasons.append(f"Confidence {confidence:.1f}% < {CONF_HARD_MIN}% hard minimum")

    return QualityFilterResult(
        allowed=(len(reasons) == 0),
        blocked_reasons=reasons,
        session_quality=session,
        adx=adx_val,
        is_severe_range=sev_range,
        is_late_entry=late,
        is_low_probability=low_prob,
        is_fake_breakout=False,
        is_weak_volume=weak_vol,
        is_low_momentum=low_mom,
    )

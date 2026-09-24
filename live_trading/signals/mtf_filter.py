"""
Multi-Timeframe (HTF) Filter — GoldScalperPro v4

Computes a Higher TimeFrame directional bias from the existing Trend,
Wyckoff, and Regime engines on HTF candles (default H1).

The live loop uses this module as a *negative* filter.  A normal entry keeps
its existing approval path; MTF only rejects an entry when the higher-timeframe
trend is clearly and strongly opposite to the candidate direction.  This is
intentional: a neutral, transitioning, unavailable, or weakly opposing HTF
must not turn MTF into a rare positive-confirmation requirement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal, Optional

from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.trend_engine import analyze_trend, TrendResult
from live_trading.signals.wyckoff_engine import analyze_wyckoff, WyckoffResult
from live_trading.signals.market_regime import detect_market_regime, RegimeResult


# H1 states that must never authorize a lower-timeframe entry.  Keep this
# explicit and centralized so a malformed or stale directional bias cannot
# bypass the final fail-closed gate.
HTF_BLOCKED_REGIMES = frozenset({"NEUTRAL", "RANGE"})


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class MtfBias:
    """
    HTF directional bias produced by compute_mtf_bias().

    direction : BUY | SELL | NEUTRAL
        The bias to apply. NEUTRAL means the HTF has not approved a
        directional entry and must block new trades.

    trend     : HTF EMA trend string (BULLISH / BEARISH / NEUTRAL).
    smc_signal: Legacy telemetry field, always NEUTRAL.
    regime    : HTF regime label     (e.g. STRONG_TREND_BULL, RANGE, …).
    strength  : STRONG | MODERATE | WEAK — derived from trend engine.
    reasoning : Ordered list of human-readable explanation strings.  Each
                element explains one decision step; the last element is
                always the summary ("Bias CONFIRMED: …" or "HTF CONFLICT: …").
    """
    direction:  Literal["BUY", "SELL", "NEUTRAL"]
    trend:      str
    smc_signal: str
    regime:     str
    strength:   Literal["STRONG", "MODERATE", "WEAK"]
    reasoning:  List[str] = field(default_factory=list)
    # Exposed for the H1 integrity gate to verify that the bias analyzer used
    # the exact same closed-candle EMA values that were validated upstream.
    ema50:      float = 0.0
    ema100:     float = 0.0
    ema200:     float = 0.0
    # Signed Trend Engine score (-100..100).  The absolute value is the
    # opposition strength used by the negative-only MTF gate.
    trend_score: float = 0.0


@dataclass(frozen=True)
class MtfOppositionCheck:
    """Decision telemetry for the negative-only MTF gate."""

    candidate: str
    htf_trend: str
    opposition_strength: float
    threshold: float
    would_block: bool
    reason: str


def _neutral(reason: str) -> MtfBias:
    """Fast-path helper: return a NEUTRAL bias with a single-element reasoning."""
    return MtfBias(
        direction="NEUTRAL", trend="NEUTRAL",
        smc_signal="NEUTRAL", regime="RANGE",
        strength="WEAK", reasoning=[reason],
    )


# ── Core computation ──────────────────────────────────────────────────────────

def compute_mtf_bias(htf_candles: List[OHLCV]) -> MtfBias:
    """
    Derive the HTF directional bias from Trend + Regime analysis.

    NEVER raises — any exception produces a NEUTRAL bias (fail-closed).
    Returns NEUTRAL when data is insufficient or engines conflict.

    Parameters
    ----------
    htf_candles : list of OHLCV
        Closed candles on the HTF (e.g. H1).  Callers should pass at least
        200 bars so the trend engine can compute EMA-200 reliably; 300 is
        the recommended default (MTF_CANDLE_WINDOW env var).

    Returns
    -------
    MtfBias
        Always non-None.  Check .direction for BUY / SELL / NEUTRAL.
    """
    if len(htf_candles) < 50:
        return _neutral(f"Insufficient HTF candles ({len(htf_candles)} < 50)")

    try:
        trend   : TrendResult  = analyze_trend(htf_candles)
        wyckoff : WyckoffResult = analyze_wyckoff(htf_candles)
        regime  : RegimeResult  = detect_market_regime(
            htf_candles, trend, wyckoff, use_atr_high_vol=False
        )
    except Exception as exc:   # noqa: BLE001
        return _neutral(f"HTF analysis error (fail-safe): {exc}")

    reasoning: List[str] = []

    # ── 1. Trend vote (primary anchor) ───────────────────────────────────────
    # EMA alignment is the most reliable HTF signal for XAUUSD intraday.
    if trend.trend == "BULLISH":
        trend_vote = "BUY"
        reasoning.append(
            f"HTF EMA aligned BULLISH "
            f"(EMA50={trend.ema50:.2f} > EMA100={trend.ema100:.2f}, "
            f"strength={trend.strength})"
        )
    elif trend.trend == "BEARISH":
        trend_vote = "SELL"
        reasoning.append(
            f"HTF EMA aligned BEARISH "
            f"(EMA50={trend.ema50:.2f} < EMA100={trend.ema100:.2f}, "
            f"strength={trend.strength})"
        )
    else:
        trend_vote = "NEUTRAL"
        reasoning.append(
            f"HTF EMA trend NEUTRAL "
            f"(EMA50={trend.ema50:.2f}, EMA100={trend.ema100:.2f}, "
            f"EMA200={trend.ema200:.2f}) — no alignment"
        )

    # ── 2. Regime context (informational) ────────────────────────────────────
    reasoning.append(
        f"HTF regime: {regime.regime} "
        f"(ADX={regime.adx:.1f}, ATR_ratio={regime.atr_ratio:.2f})"
    )

    # ── 3. Combine → final bias ───────────────────────────────────────────────
    # MTF is a Trend-only directional filter. SMC is intentionally absent from
    # this path; the legacy result field stays NEUTRAL for telemetry shape
    # compatibility.
    if trend_vote == "NEUTRAL":
        direction : Literal["BUY", "SELL", "NEUTRAL"] = "NEUTRAL"
        strength  : Literal["STRONG", "MODERATE", "WEAK"] = "WEAK"
        reasoning.append(
            "HTF bias: NEUTRAL (trend not aligned — no M5 filter applied)"
        )
    else:
        direction = trend_vote  # type: ignore[assignment]
        strength  = trend.strength
        reasoning.append(
            f"Bias CONFIRMED: {direction} (Trend-only, strength={strength})"
        )

    return MtfBias(
        direction=direction,
        trend=trend.trend,
        smc_signal="NEUTRAL",
        regime=regime.regime,
        strength=strength,
        reasoning=reasoning,
        ema50=trend.ema50,
        ema100=trend.ema100,
        ema200=trend.ema200,
        trend_score=trend.score,
    )


# ── Negative-only gate ─────────────────────────────────────────────────────────

def evaluate_mtf_opposition(
    bias: Optional[MtfBias],
    candidate: str,
    opposition_threshold: float = 55.0,
) -> MtfOppositionCheck:
    """Measure whether the HTF trend is strongly opposite to ``candidate``.

    ``opposition_threshold`` is deliberately the only blocking threshold.
    Missing/neutral/weak HTF context has zero opposition strength and therefore
    preserves the existing entry path.
    """
    candidate = str(candidate).upper()
    threshold = max(0.0, min(100.0, float(opposition_threshold)))
    htf_trend = "NEUTRAL"

    if bias is not None:
        if bias.trend == "BULLISH":
            htf_trend = "BUY"
        elif bias.trend == "BEARISH":
            htf_trend = "SELL"

    opposition_strength = 0.0
    if (
        candidate in {"BUY", "SELL"}
        and htf_trend in {"BUY", "SELL"}
        and candidate != htf_trend
        and bias is not None
    ):
        opposition_strength = round(
            max(0.0, min(100.0, abs(float(bias.trend_score)))), 1
        )

    would_block = (
        candidate in {"BUY", "SELL"}
        and htf_trend in {"BUY", "SELL"}
        and candidate != htf_trend
        and opposition_strength >= threshold
    )
    if would_block:
        reason = (
            f"strong HTF opposition: candidate={candidate} "
            f"htf_trend={htf_trend} strength={opposition_strength:.1f}"
        )
    elif htf_trend == "NEUTRAL":
        reason = "no directional HTF trend to oppose the candidate"
    elif candidate == htf_trend:
        reason = "candidate is aligned with the HTF trend"
    else:
        reason = (
            f"HTF opposition is below threshold: "
            f"{opposition_strength:.1f} < {threshold:.1f}"
        )

    return MtfOppositionCheck(
        candidate=candidate,
        htf_trend=htf_trend,
        opposition_strength=opposition_strength,
        threshold=threshold,
        would_block=would_block,
        reason=reason,
    )

def mtf_allows_trade(
    bias: Optional[MtfBias],
    m5_direction: str,
    confidence: Optional[float] = None,
    confirmed_timeframes: int = 0,
    min_confidence: float = 49.0,
    min_timeframes: int = 2,
    allow_range_regime: bool = False,
    opposition_threshold: float = 55.0,
) -> tuple[bool, str]:
    """Compatibility wrapper for callers that expect an ``(allowed, reason)``.

    The old confidence, timeframe, and RANGE arguments are retained so older
    integrations do not break, but MTF no longer uses them as approval gates.
    """
    check = evaluate_mtf_opposition(
        bias,
        m5_direction,
        opposition_threshold=opposition_threshold,
    )
    if check.would_block:
        return False, f"MTF BLOCK: {check.reason}"
    return True, ""

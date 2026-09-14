"""
Multi-Timeframe (HTF) Filter — GoldScalperPro v4

Computes a Higher TimeFrame directional bias by reusing the existing
Trend, SMC, Wyckoff, and Regime engines on HTF candles (default H1).
The live loop uses this bias as an additional trade gate:

  • Only BUY entries allowed when HTF bias is BUY.
  • Only SELL entries allowed when HTF bias is SELL.
  • NEUTRAL or RANGE HTF → entry blocked.
  • An unavailable HTF bias → entry blocked (fail-closed).
  • The entry must have at least 49% confidence and two confirmed
    timeframes (the HTF plus the active entry timeframe).

Design principles:
  • Safety first: any fetch/analysis error → NEUTRAL → trade is blocked.
    A missing HTF confirmation must never be treated as approval.
  • Additive: zero changes to any existing signal engine or decision logic.
    This module is a pure add-on — it imports from the existing engines but
    never modifies them.
  • Fail-safe precedence: Trend must be non-NEUTRAL for a bias to be issued.
    If Trend is neutral but SMC has a strong signal, we stay NEUTRAL rather
    than issuing a bias from SMC alone (SMC is noisier on higher timeframes
    without the EMA anchor).
  • Conflict suppression: if Trend and SMC actively disagree, bias = NEUTRAL.
    A conflicted HTF means the market is transitioning — entry is blocked.
  • Configurable: MTF_ENABLED / MTF_TIMEFRAME / MTF_CANDLE_WINDOW env vars.
  • Transparent: all reasoning is recorded in MtfBias.reasoning for logging
    and panel display.

Typical usage (inside live_loop._on_new_bar):

    htf_candles = await fetch_candles(SYMBOL, MTF_TIMEFRAME, MTF_CANDLE_WINDOW)
    htf_bias    = compute_mtf_bias(htf_candles)          # never raises
    allowed, reason = mtf_allows_trade(htf_bias, decision.direction)
    if not allowed:
        log.info(f"MTF BLOCK: {reason}")
        return
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal, Optional

from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.trend_engine import analyze_trend, TrendResult
from live_trading.signals.smc_engine import analyze_smc_structure, SmcResult
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
    smc_signal: HTF SMC signal       (BUY / SELL / NEUTRAL).
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
    Derive the HTF directional bias from Trend + SMC + Regime analysis.

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
        smc     : SmcResult    = analyze_smc_structure(htf_candles, timeframe="H1")
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

    # ── 2. SMC vote (structural confirmation) ────────────────────────────────
    # HTF SMC gives structural confirmation (BOS, CHoCH, OB), but is
    # NOT allowed to override a neutral trend — it can only reinforce.
    smc_vote = smc.smc_signal   # BUY | SELL | NEUTRAL
    if smc_vote != "NEUTRAL":
        reasoning.append(
            f"HTF SMC signal: {smc_vote} "
            f"(BOS={len(smc.bos_signals)}, "
            f"CHoCH={len(smc.choch_signals)}, "
            f"OBs={len(smc.order_blocks)}, "
            f"FVGs={len(smc.fair_value_gaps)})"
        )
    else:
        reasoning.append("HTF SMC: no structural direction signal")

    # ── 3. Regime context (informational) ────────────────────────────────────
    reasoning.append(
        f"HTF regime: {regime.regime} "
        f"(ADX={regime.adx:.1f}, ATR_ratio={regime.atr_ratio:.2f})"
    )

    # ── 4. Combine → final bias ───────────────────────────────────────────────
    # Rule matrix:
    #   Trend NEUTRAL            → NEUTRAL (no filter regardless of SMC)
    #   Trend directional + SMC agrees or NEUTRAL → bias = trend direction
    #   Trend directional + SMC actively opposes  → NEUTRAL (conflicted HTF)
    if trend_vote == "NEUTRAL":
        direction : Literal["BUY", "SELL", "NEUTRAL"] = "NEUTRAL"
        strength  : Literal["STRONG", "MODERATE", "WEAK"] = "WEAK"
        reasoning.append(
            "HTF bias: NEUTRAL (trend not aligned — no M5 filter applied)"
        )
    elif smc_vote == "NEUTRAL" or smc_vote == trend_vote:
        direction = trend_vote  # type: ignore[assignment]
        strength  = trend.strength
        smc_note  = "SMC confirms" if smc_vote == trend_vote else "SMC neutral"
        reasoning.append(
            f"Bias CONFIRMED: {direction} ({smc_note}, strength={strength})"
        )
    else:
        # Trend and SMC conflict — HTF is transitioning; pass-through
        direction = "NEUTRAL"
        strength  = "WEAK"
        reasoning.append(
            f"HTF CONFLICT: Trend={trend_vote} vs SMC={smc_vote} "
            f"— bias NEUTRAL, no M5 filter applied"
        )

    return MtfBias(
        direction=direction,
        trend=trend.trend,
        smc_signal=smc_vote,
        regime=regime.regime,
        strength=strength,
        reasoning=reasoning,
        ema50=trend.ema50,
        ema100=trend.ema100,
        ema200=trend.ema200,
    )


# ── Gate helper ───────────────────────────────────────────────────────────────

def mtf_allows_trade(
    bias: Optional[MtfBias],
    m5_direction: str,
    confidence: Optional[float] = None,
    confirmed_timeframes: int = 0,
    min_confidence: float = 49.0,
    min_timeframes: int = 2,
    allow_range_regime: bool = False,
) -> tuple[bool, str]:
    """
    Check whether a proposed M5 trade is aligned with the HTF bias.

    Parameters
    ----------
    bias          : MtfBias | None
        The result of compute_mtf_bias().  None is treated as NEUTRAL.
    m5_direction  : str
        The active entry timeframe decision engine's proposed direction:
        "BUY", "SELL",
        or "NEUTRAL".
    confidence    : float | None
        Entry confidence percentage. Required for a strict approval.
    confirmed_timeframes : int
        Number of distinct timeframes agreeing on the direction. The HTF and
        active entry timeframe count as two when both agree.
    min_confidence : float
        Minimum confidence percentage required for entry.
    min_timeframes : int
        Minimum number of confirming timeframes required.

    Returns
    -------
    (allowed, reason)
        allowed=True  → all Option 2 conditions are satisfied.
        allowed=False → caller should block the trade; reason explains why.
        When allowed=True, reason is an empty string.

    This function NEVER raises.
    """
    if bias is None:
        return False, "MTF BLOCK: HTF bias unavailable — confirmation required"

    if m5_direction == "NEUTRAL":
        return False, "MTF BLOCK: entry direction is NEUTRAL"

    # A RANGE entry has its own edge/sweep/reversal gate on the active
    # timeframe.  When H1 is also ranging, it is valid context rather than a
    # reason to block every trade.  Directional H1 regimes still go through
    # the normal alignment check below.
    if allow_range_regime and bias.regime == "RANGE":
        if confidence is None:
            return False, "MTF BLOCK: confidence is unavailable"
        if confidence < min_confidence:
            return (
                False,
                f"MTF BLOCK: range confidence {confidence:.1f}% < "
                f"{min_confidence:.1f}% minimum",
            )
        if confirmed_timeframes < min_timeframes:
            return (
                False,
                f"MTF BLOCK: only {confirmed_timeframes} timeframe "
                f"confirmation(s); {min_timeframes} required",
            )
        return True, "HTF RANGE accepted for dedicated edge/sweep/reversal play"

    if bias.direction == "NEUTRAL":
        return (
            False,
            f"MTF BLOCK: HTF is NEUTRAL [trend={bias.trend}, "
            f"regime={bias.regime}]",
        )

    # Fail closed on the H1 trend/regime itself as well as on the public
    # direction.  compute_mtf_bias() normally keeps these fields consistent,
    # but this guard prevents a stale, partially populated, or future bias
    # object from authorizing a trade while H1 is neutral or ranging.
    if bias.trend == "NEUTRAL":
        return (
            False,
            f"MTF BLOCK: HTF trend is NEUTRAL [regime={bias.regime}]",
        )

    if bias.regime in HTF_BLOCKED_REGIMES:
        return False, f"MTF BLOCK: HTF regime is {bias.regime} — entry prohibited"

    if m5_direction != bias.direction:
        return (
            False,
            f"MTF BLOCK: entry wants {m5_direction} but HTF confirms "
            f"{bias.direction} [trend={bias.trend}, SMC={bias.smc_signal}, "
            f"regime={bias.regime}, strength={bias.strength}]",
        )

    if confidence is None:
        return False, "MTF BLOCK: confidence is unavailable"

    if confidence < min_confidence:
        return (
            False,
            f"MTF BLOCK: confidence {confidence:.1f}% < "
            f"{min_confidence:.1f}% minimum",
        )

    if confirmed_timeframes < min_timeframes:
        return (
            False,
            f"MTF BLOCK: only {confirmed_timeframes} timeframe "
            f"confirmation(s); {min_timeframes} required",
        )

    return True, ""

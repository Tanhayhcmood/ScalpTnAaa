"""
Confidence Score Engine — weighted 0–100 score across 6 components.
Ported from confidenceEngine.ts
"""
from dataclasses import dataclass
from typing import List, Literal
from live_trading.signals.smc_engine import SmcResult
from live_trading.signals.wyckoff_engine import WyckoffResult
from live_trading.signals.price_action_engine import PriceActionResult
from live_trading.signals.trend_engine import TrendResult
from live_trading.signals.market_regime import RegimeResult
from live_trading.config import CONF_HARD_MIN


@dataclass
class ConfidenceComponents:
    smc_score:        float   # 0–35
    trend_score:      float   # 0–20
    pa_score:         float   # 0–20 normally; up to 35 in standalone PA mode
    wyckoff_score:    float   # 0–15
    liquidity_score:  float   # 0–5
    volatility_score: float   # 0–5
    total:            float   # 0–100
    divergence_score: float = 0.0  # 0–10 bonus (RSI/MACD divergence)
    # Retained for panel/backward-compatible payloads. DXY is intentionally
    # inactive in entry confidence scoring.
    dxy_score:        float = 0.0


@dataclass
class ConfidenceResult:
    confidence:  float
    components:  ConfidenceComponents
    grade:       Literal["PRIME", "HIGH", "MARGINAL", "REJECTED"]
    reasoning:   List[str]


def _cap(val: float, max_val: float) -> float:
    return max(0.0, min(max_val, val))


def _calc_smc_score(smc: SmcResult, candidate: str):
    reasons = []
    pts = 0.0
    d = candidate

    if smc.trend == ("BULLISH" if d == "BUY" else "BEARISH"):
        pts += 4; reasons.append("Structural trend aligned")

    aligned_bos = [b for b in smc.bos_signals if b.type == d]
    if len(aligned_bos) >= 2:
        pts += 7; reasons.append("Multiple BOS confirmed")
    elif len(aligned_bos) == 1:
        pts += 5; reasons.append("BOS confirmed")

    last_choch = smc.choch_signals[-1] if smc.choch_signals else None
    if last_choch and last_choch.type == d:
        pts += 8; reasons.append("CHoCH (structural reversal) confirmed")

    ob_pts = 0; ob_count = 0
    for ob in smc.order_blocks:
        if ob.type != ("BULLISH" if d == "BUY" else "BEARISH"): continue
        if ob_count >= 2: break
        body = abs(ob.close - ob.open)
        rng  = max(ob.high - ob.low, 0.01)
        ob_pts += 4 if body / rng >= 0.5 else 3
        ob_count += 1
    if ob_pts > 0:
        pts += _cap(ob_pts, 8)
        reasons.append(f"Order Block{'s ×' + str(ob_count) if ob_count > 1 else ''} in zone")

    fvg_count = sum(1 for f in smc.fair_value_gaps
                    if f.type == ("BULLISH" if d == "BUY" else "BEARISH"))
    if fvg_count >= 2:   pts += 4; reasons.append("Multiple FVGs in direction")
    elif fvg_count == 1: pts += 2; reasons.append("FVG in direction")

    last_sweep = smc.liquidity_sweeps[-1] if smc.liquidity_sweeps else None
    if last_sweep and last_sweep.type == ("BULLISH" if d == "BUY" else "BEARISH"):
        pts += 4; reasons.append("Liquidity sweep confirmed")

    return _cap(pts, 35), reasons


def _calc_trend_score(trend: TrendResult, candidate: str):
    reasons = []
    target = "BULLISH" if candidate == "BUY" else "BEARISH"
    if trend.trend == target:
        if trend.strength == "STRONG":
            reasons.append("Strong EMA alignment (50/100/200)")
            return 20.0, reasons
        reasons.append("Moderate EMA alignment (50/100)")
        return 14.0, reasons
    if trend.trend == "NEUTRAL":
        reasons.append("Neutral EMA trend (choppy)")
        return 7.0, reasons
    return 0.0, ["Counter-trend: EMA opposes direction"]


def _calc_pa_score(pa: PriceActionResult, candidate: str):
    # M-2 FIX: removed pa.pa_score * 13 base multiplier — it double-counted the
    # same patterns that are explicitly rewarded in the blocks below, inflating
    # PA contribution by up to 13 pts per strong setup.
    reasons = []
    is_buy  = candidate == "BUY"
    pts = 0.0

    if is_buy:
        if pa.bullish_engulf:              pts += 4; reasons.append("Bullish Engulf")
        elif pa.bullish_pin_bar:           pts += 3; reasons.append("Bullish Pin Bar")
        elif pa.strong_bullish:            pts += 2; reasons.append("Strong Bull candle")
        if pa.bullish_inside_breakout:
            pts += 3; reasons.append(
                f"Bullish Inside-Bar Breakout"
                f"{' ×' + str(pa.inside_bar_depth) if pa.inside_bar_depth > 1 else ''}"
            )
        if pa.valid_bull_breakout:         pts += 2; reasons.append("Valid Breakout")
        if pa.bullish_pullback:            pts += 1; reasons.append("Pullback to demand")
        if pa.near_demand_zone or pa.near_support: pts += 1; reasons.append("Near demand/support")
        if pa.fake_bull_breakout:          pts -= 6; reasons.append("⚠ Fake Breakout detected")
    else:
        if pa.bearish_engulf:              pts += 4; reasons.append("Bearish Engulf")
        elif pa.bearish_pin_bar:           pts += 3; reasons.append("Bearish Pin Bar")
        elif pa.strong_bearish:            pts += 2; reasons.append("Strong Bear candle")
        if pa.bearish_inside_breakout:
            pts += 3; reasons.append(
                f"Bearish Inside-Bar Breakout"
                f"{' ×' + str(pa.inside_bar_depth) if pa.inside_bar_depth > 1 else ''}"
            )
        if pa.valid_bear_breakout:         pts += 2; reasons.append("Valid Breakout")
        if pa.bearish_pullback:            pts += 1; reasons.append("Pullback to supply")
        if pa.near_supply_zone or pa.near_resistance: pts += 1; reasons.append("Near supply/resistance")
        if pa.fake_bear_breakout:          pts -= 6; reasons.append("⚠ Fake Breakout detected")

    if pa.pa_signal == candidate and not reasons:
        reasons.append("PA signal aligned")

    return _cap(pts, 20), reasons


def _calc_wyckoff_score(wyckoff: WyckoffResult, candidate: str):
    if wyckoff.wyckoff_signal != candidate:
        return 0.0, []
    reasons = []
    pts = wyckoff.wyckoff_score * 8
    if candidate == "BUY"  and wyckoff.spring:
        pts += 4; reasons.append("Spring confirmed")
    if candidate == "SELL" and wyckoff.upthrust:
        pts += 4; reasons.append("Upthrust confirmed")
    if wyckoff.volume_confirmed:
        pts += 3; reasons.append("Volume confirms phase")
    if wyckoff.phase != "NEUTRAL":
        reasons.insert(0, f"Wyckoff {wyckoff.phase} phase")
    return _cap(pts, 15), reasons


def _calc_liquidity_score(smc: SmcResult, candidate: str):
    reasons = []
    pts = 0.0
    sweep_dir = "BULLISH" if candidate == "BUY" else "BEARISH"
    aligned_sweeps = [s for s in smc.liquidity_sweeps if s.type == sweep_dir]
    if aligned_sweeps:
        pts += 2.5; reasons.append("Liquidity sweep in direction")

    eq_count = (len(smc.equal_lows) if candidate == "BUY" else len(smc.equal_highs))
    if eq_count >= 2:   pts += 1.5; reasons.append("Multiple equal-level pools")
    elif eq_count == 1: pts += 0.75; reasons.append("Equal-level pool")

    if aligned_sweeps and any(b.type == candidate for b in smc.bos_signals):
        pts += 1.0; reasons.append("Sweep + BOS confluence")

    return _cap(pts, 5), reasons


def _calc_volatility_score(regime: RegimeResult, session: str):
    reasons = []
    pts = 0.0
    if session == "PRIME":     pts += 2.5; reasons.append("Prime session (London/NY)")
    elif session == "MODERATE": pts += 1.0; reasons.append("Moderate session")
    if regime.adx >= 30:   pts += 1.5; reasons.append(f"ADX {regime.adx} (strong momentum)")
    elif regime.adx >= 20: pts += 0.75
    if 0.8 <= regime.atr_ratio <= 1.5:
        pts += 1.0; reasons.append("Normal ATR range")
    elif regime.atr_ratio > 1.5:
        pts += 0.5
    return _cap(pts, 5), reasons


def _assign_grade(confidence: float, min_conf: float) -> Literal["PRIME","HIGH","MARGINAL","REJECTED"]:
    # PRIME    — ≥ 90 %: exceptional confidence, well above any regime threshold.
    # HIGH     — ≥ regime min_conf but < 90: trade is allowed, solid setup.
    # MARGINAL — ≥ hard floor but < regime threshold: trade may still be
    #            allowed by the marginal R:R check in decision_engine.
    # REJECTED — below the hard floor; trade will never be allowed.
    # CONF_HARD_MIN is imported from live_trading.config so the CONF_HARD_MIN
    # env var is honoured here as well as in decision_engine.py.
    if confidence >= 90:           return "PRIME"
    if confidence >= min_conf:     return "HIGH"
    if confidence >= CONF_HARD_MIN: return "MARGINAL"
    return "REJECTED"




def _calc_divergence_score(divergence_signal: str, candidate: str) -> tuple:
    """Score RSI/MACD divergence confirmation (0–10 bonus, –5 penalty)."""
    reasons = []
    if divergence_signal == "NEUTRAL":
        return 0.0, reasons
    # Map trade direction to expected divergence signal
    expected = "BULLISH" if candidate == "BUY" else "BEARISH"
    if divergence_signal == expected:
        reasons.append(f"{divergence_signal.title()} RSI/MACD divergence confirmed")
        return 10.0, reasons
    # Opposing divergence — penalise
    reasons.append(f"Counter-divergence ({divergence_signal}) against {candidate}")
    return -5.0, reasons


def _calc_dxy_score(dxy_signal: str, candidate: str) -> tuple:
    """
    DXY correlation score (–5 to +5).
    BEARISH_DXY  + BUY  gold → +5  (dollar falling, gold rising)
    BULLISH_DXY  + SELL gold → +5  (dollar rising,  gold falling)
    BEARISH_DXY  + SELL gold → –5  (contradiction)
    BULLISH_DXY  + BUY  gold → –5  (contradiction)
    NEUTRAL                   →  0
    """
    reasons = []
    if dxy_signal == "NEUTRAL":
        return 0.0, reasons
    if dxy_signal == "BEARISH_DXY" and candidate == "BUY":
        reasons.append("DXY falling — tailwind for gold BUY")
        return 5.0, reasons
    if dxy_signal == "BULLISH_DXY" and candidate == "SELL":
        reasons.append("DXY rising — tailwind for gold SELL")
        return 5.0, reasons
    if dxy_signal == "BULLISH_DXY" and candidate == "BUY":
        reasons.append("DXY rising — headwind against gold BUY")
        return -5.0, reasons
    if dxy_signal == "BEARISH_DXY" and candidate == "SELL":
        reasons.append("DXY falling — headwind against gold SELL")
        return -5.0, reasons
    return 0.0, reasons

def calc_confidence(
    smc:              SmcResult,
    wyckoff:          WyckoffResult,
    pa:               PriceActionResult,
    trend:            TrendResult,
    regime:           RegimeResult,
    session:          str,
    candidate:        str,
    divergence_signal: str = "NEUTRAL",
    dxy_signal:        str = "NEUTRAL",
    price_action_standalone: bool = False,
) -> ConfidenceResult:
    smc_s,  smc_r  = _calc_smc_score(smc, candidate)
    tr_s,   tr_r   = _calc_trend_score(trend, candidate)
    pa_s,   pa_r   = _calc_pa_score(pa, candidate)
    if price_action_standalone and pa.pa_signal == candidate:
        # In independent PA mode, the PA engine is the primary evidence
        # source. Promote its bounded local score to the same 0–35 ceiling as
        # structural confirmation, while retaining volatility and confidence
        # gates below. This makes "standalone" genuinely usable without
        # reducing the hard confidence floor or borrowing votes from another
        # strategy.
        standalone_pa = min(35.0, 28.0 + pa.pa_score * 20.0)
        pa_s = max(pa_s, standalone_pa)
        pa_r = pa_r + [
            f"Standalone PA evidence {pa.pa_score:.2f} "
            f"({standalone_pa:.1f} confidence points)"
        ]
    wy_s,   wy_r   = _calc_wyckoff_score(wyckoff, candidate)
    liq_s,  liq_r  = _calc_liquidity_score(smc, candidate)
    vol_s,  vol_r  = _calc_volatility_score(regime, session)

    div_s, div_r = _calc_divergence_score(divergence_signal, candidate)
    # Option 3: DXY must not influence entry confidence in either direction.
    # Keep the argument for compatibility with existing callers and telemetry,
    # but deliberately do not evaluate it here.
    dxy_s, dxy_r = 0.0, []

    raw_total  = smc_s + tr_s + pa_s + wy_s + liq_s + vol_s + div_s + dxy_s
    total_capped = round(min(100.0, max(0.0, raw_total)), 1)

    comp = ConfidenceComponents(
        smc_score=smc_s, trend_score=tr_s, pa_score=pa_s,
        wyckoff_score=wy_s, liquidity_score=liq_s, volatility_score=vol_s,
        total=total_capped,
        divergence_score=max(0.0, div_s),
        dxy_score=dxy_s,
    )
    confidence = comp.total
    reasoning  = smc_r + tr_r + pa_r + wy_r + liq_r + vol_r + div_r + dxy_r

    return ConfidenceResult(
        confidence=confidence,
        components=comp,
        grade=_assign_grade(confidence, regime.rules.min_confidence),
        reasoning=reasoning,
    )

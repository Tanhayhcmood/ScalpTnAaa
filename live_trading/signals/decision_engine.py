"""
Decision Engine — Central orchestrator of all 7 signal engines.
Ported from decisionEngine.ts
"""
from dataclasses import dataclass, field
from typing import List, Literal, Optional
from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.smc_engine import (
    SmcResult,
    analyze_smc_structure,
)
from live_trading.signals.wyckoff_engine import WyckoffResult, analyze_wyckoff
from live_trading.signals.price_action_engine import PriceActionResult, analyze_price_action
from live_trading.signals.trend_engine import TrendResult, analyze_trend
from live_trading.signals.market_regime import RegimeResult, RegimeEntryRules, detect_market_regime
from live_trading.signals.confidence_engine import ConfidenceResult, ConfidenceComponents, calc_confidence
from live_trading.signals.quality_filter import QualityFilterResult, apply_quality_filter, get_session_quality
from live_trading.signals.entry_filter import apply_entry_filter, EntryFilterResult
from live_trading.signals.range_strategy import RangeContext, evaluate_range_entry
from live_trading.signals.divergence_engine import analyze_divergence, DivergenceResult
from live_trading.risk.capital_manager import (
    FIXED_TP_RR, MAX_FIXED_LOT_RISK_USD, CapitalInput, CapitalOutput,
    calc_trade_parameters,
)
from live_trading.config import (
    CONF_HARD_MIN,
    MIN_CONFIRMATIONS,
    PRICE_ACTION_STANDALONE,
    RANGE_MIN_CONFIRMATIONS,
    REQUIRE_SMC_PRICE_ACTION_WYCKOFF,
    RANGE_ENTRY_FILTERS_ENABLED,
    RANGE_REQUIRE_EDGE_POSITION,
    TREND_MIN_CONFIRMATIONS,
)

# Marginal confidence R:R floor: trades with confidence between CONF_HARD_MIN
# and the regime minimum must still achieve this R:R to be allowed.
# 1.3 = profitable in expectancy even at 45% win rate (1.3 × 0.45 > 0.55).
CONF_MARGINAL_RR = 1.3

# SMC/Wyckoff/divergence remain available for telemetry, but the live entry
# policy is deliberately limited to Price Action and EMA Trend. RANGE keeps
# its non-signal structural safeguards.
_CHOPPY_REGIMES = {"ACCUMULATION", "DISTRIBUTION", "HIGH_VOLATILITY"}


def _effective_min_confirmations(
    base_min_confirmations: int,
    regime: str,
    counter_trend: bool,
    range_min_confirmations: int = RANGE_MIN_CONFIRMATIONS,
) -> int:
    """Return the operator-selected N-of-4 floor.

    Counter-trend and choppy-market handling remain covered by the existing
    confidence, quality, regime, MTF, and risk gates; they do not silently
    turn the configured one-strategy floor into a stricter vote requirement.
    """
    if regime == "RANGE":
        # RANGE owns its floor; never inherit the ordinary/TREND floor.
        return range_min_confirmations
    return base_min_confirmations


def _allow_without_smc_for_quality(
    entry_filter: EntryFilterResult,
    effective_min_confirmations: int,
    price_action_standalone: bool,
) -> bool:
    """Keep the quality gate aligned with the PA + Trend policy.

    SMC is display-only, so an entry that passed the policy must never be
    rejected merely because SMC is neutral.  The legacy arguments remain for
    source compatibility with callers and tests.
    """
    del effective_min_confirmations, price_action_standalone
    return bool(entry_filter.allowed)


def _range_confirmation_gate(
    entry_filter: EntryFilterResult,
    min_confirmations: int,
    price_action_standalone: bool = False,
    entry_policy_only: bool = False,
) -> tuple[bool, str]:
    """Apply the RANGE confirmation floor without changing vote semantics.

    RANGE keeps its separate edge, fresh sweep, reversal, R:R, and session
    limits. Its signal vote still follows the same equal-weight N-of-4
    consensus as ordinary entries. An explicit standalone Price Action policy
    may reduce this floor to one aligned PA vote; all other RANGE safeguards
    remain mandatory.
    """
    if entry_policy_only:
        if entry_filter.allowed:
            return True, ""
        return False, f"Entry policy blocked: {entry_filter.entry_reason}"

    effective_min_confirmations = (
        1
        if price_action_standalone and entry_filter.price_action
        else min_confirmations
    )
    if entry_filter.confirmation_count < effective_min_confirmations:
        return (
            False,
            f"RANGE entry blocked: {entry_filter.confirmation_count}/"
            f"{effective_min_confirmations} confirmations",
        )
    return True, ""


@dataclass
class DecisionResult:
    allowed:         bool
    direction:       Literal["BUY", "SELL", "NEUTRAL"]
    confidence:      float
    components:      ConfidenceComponents
    grade:           str
    regime:          str
    regime_label:    str
    regime_rules:    RegimeEntryRules
    quality_filter:  QualityFilterResult
    blocked_reasons: List[str]
    reasoning:       List[str]
    trade_params:    Optional[CapitalOutput]
    smc:    SmcResult
    wyckoff: WyckoffResult
    pa:     PriceActionResult
    trend:  TrendResult
    # Additive, optional — which of the 4 independent engines (SMC/Trend/
    # PriceAction/Wyckoff) voted for this trade's direction. None on the
    # early "no SMC signal" path, where the vote was never computed.
    # Existing callers that construct/consume DecisionResult are unaffected
    # since this has a default and nothing reads it unless it asks for it.
    entry_filter:    Optional[EntryFilterResult] = None
    divergence:      Optional[DivergenceResult]  = None
    dxy_signal:      str                         = "NEUTRAL"
    range_context:   Optional[RangeContext]      = None


def _candidate_direction(
    smc: SmcResult,
    wyckoff: WyckoffResult,
    pa: PriceActionResult,
    trend: TrendResult,
    price_action_standalone: bool = False,
) -> str:
    """Return the candidate from the two allowed entry engines only."""
    del smc, wyckoff, price_action_standalone
    pa_vote = pa.pa_signal if pa.pa_signal in {"BUY", "SELL"} else "NEUTRAL"
    if pa_vote in {"BUY", "SELL"}:
        return pa_vote
    return (
        "BUY" if trend.trend == "BULLISH"
        else "SELL" if trend.trend == "BEARISH"
        else "NEUTRAL"
    )


def _make_neutral(
    smc, wyckoff, pa, trend, blocked_reasons, reasoning=None,
    range_context: Optional[RangeContext] = None,
    regime_result: Optional[RegimeResult] = None,
    entry_filter: Optional[EntryFilterResult] = None,
    direction: Optional[str] = None,
    candles: Optional[List[OHLCV]] = None,
) -> DecisionResult:
    from live_trading.signals.market_regime import REGIME_RULES
    rules = regime_result.rules if regime_result is not None else REGIME_RULES["RANGE"]
    regime = regime_result.regime if regime_result is not None else "RANGE"
    regime_label = rules.label if regime_result is not None else "No Signal"
    adx = regime_result.adx if regime_result is not None else 0.0
    candidate = direction or _candidate_direction(smc, wyckoff, pa, trend)
    if candidate not in {"BUY", "SELL"}:
        candidate = "NEUTRAL"
    try:
        session = get_session_quality(candles[-1].time) if candles else "BLOCKED"
    except (IndexError, AttributeError):
        session = "BLOCKED"
    return DecisionResult(
        allowed=False, direction=candidate, confidence=0.0,
        components=ConfidenceComponents(0,0,0,0,0,0,0),
        grade="REJECTED", regime=regime, regime_label=regime_label,
        regime_rules=rules,
        quality_filter=QualityFilterResult(
            allowed=False, blocked_reasons=blocked_reasons,
            session_quality=session, adx=adx,
            is_severe_range=False, is_late_entry=False,
            is_low_probability=False, is_fake_breakout=False,
            is_weak_volume=False, is_low_momentum=False,
        ),
        blocked_reasons=blocked_reasons,
        reasoning=reasoning or [],
        trade_params=None,
        smc=smc, wyckoff=wyckoff, pa=pa, trend=trend,
        entry_filter=entry_filter,
        range_context=range_context,
    )


def run_decision_engine(
    candles:           List[OHLCV],
    account_balance:   float,
    risk_percent:      float = 1.0,
    min_confirmations: int   = MIN_CONFIRMATIONS,
    trend_min_confirmations: int = TREND_MIN_CONFIRMATIONS,
    use_atr_high_vol:  bool  = False,
    dxy_signal:        str   = "NEUTRAL",
    require_price_action: bool = False,
    require_smc_price_action_wyckoff: bool = REQUIRE_SMC_PRICE_ACTION_WYCKOFF,
    range_trading_enabled: bool = True,
    range_min_confirmations: int = RANGE_MIN_CONFIRMATIONS,
    range_min_rr: float = 1.5,
    range_edge_atr_distance: float = 0.25,
    range_risk_percent: Optional[float] = None,
    range_entry_filters_enabled: bool = RANGE_ENTRY_FILTERS_ENABLED,
    range_require_edge_position: bool = RANGE_REQUIRE_EDGE_POSITION,
    timeframe: str = "M5",
    price_action_standalone: bool = PRICE_ACTION_STANDALONE,
) -> DecisionResult:

    # This is part of the requested policy, not an operator-tunable legacy
    # switch.  Keep the parameter for compatibility with existing callers,
    # but always enable PA standalone when evaluating the live robot.
    price_action_standalone = True

    smc     = analyze_smc_structure(candles, timeframe=timeframe)
    wyckoff = analyze_wyckoff(candles)
    pa      = analyze_price_action(candles, timeframe=timeframe)
    trend   = analyze_trend(candles)

    # SMC and Wyckoff are intentionally passed through for telemetry only.
    # Their signals, votes, and legacy operator overrides cannot authorize or
    # block an entry in this policy.
    ef = apply_entry_filter(
        smc_signal      = smc.smc_signal,
        ema_trend       = trend.trend,
        pa_signal       = pa.pa_signal,
        wyckoff_signal  = wyckoff.wyckoff_signal,
        min_confirmations = min_confirmations,
        require_price_action = require_price_action,
        require_smc_price_action_wyckoff = require_smc_price_action_wyckoff,
        price_action_standalone=price_action_standalone,
    )
    candidate = _candidate_direction(
        smc,
        wyckoff,
        pa,
        trend,
        price_action_standalone=price_action_standalone,
    )
    if candidate == "NEUTRAL":
        return _make_neutral(
            smc, wyckoff, pa, trend,
            [f"Entry policy blocked: {ef.entry_reason}"],
            entry_filter=ef,
            direction="NEUTRAL",
            candles=candles,
        )

    # Trend is used only as the second allowed engine.  A counter-trend
    # candidate is not special-cased here; PA/Trend disagreement is a hard
    # policy block regardless of market regime.
    trend_dir = ("BUY" if trend.trend == "BULLISH" else
                 "SELL" if trend.trend == "BEARISH" else "NEUTRAL")
    _counter_trend = (candidate == "BUY" and trend_dir == "SELL") or \
                     (candidate == "SELL" and trend_dir == "BUY")

    # Detect regime early — needed to set the adaptive confirmation threshold.
    regime = detect_market_regime(
        candles,
        trend,
        wyckoff,
        use_atr_high_vol,
        use_wyckoff_phase=False,
    )
    effective_min_confirmations = _effective_min_confirmations(
        min_confirmations,
        regime.regime,
        _counter_trend,
    )

    # Entry filter — only the two allowed engines participate.  The legacy
    # confirmation settings are retained in telemetry for compatibility, but
    # they no longer turn this into an N-of-4 gate.
    is_range_regime = regime.regime == "RANGE"
    # The RANGE playbook retains its edge/reversal/risk safeguards, but its
    # confirmation gate must not reintroduce SMC/Wyckoff.
    if is_range_regime:
        range_votes_ok, range_votes_reason = _range_confirmation_gate(
            ef,
            range_min_confirmations,
            price_action_standalone=price_action_standalone,
            entry_policy_only=True,
        )
        if not range_votes_ok:
            return _make_neutral(
                smc, wyckoff, pa, trend,
            [range_votes_reason],
            [range_votes_reason],
            regime_result=regime,
            entry_filter=ef,
            direction=candidate,
            candles=candles,
            )
    elif not ef.allowed:
        reason = (
            f"Entry policy blocked: {ef.entry_reason} "
            f"[PA={pa.pa_signal}, Trend={trend_dir}, regime={regime.regime}]"
        )
        return _make_neutral(
            smc, wyckoff, pa, trend, [reason], [reason],
            regime_result=regime,
            entry_filter=ef,
            direction=candidate,
            candles=candles,
        )

    # RANGE is a separate playbook. It must never inherit a trend entry just
    # because the generic four-engine vote happened to pass.
    range_context: Optional[RangeContext] = None
    if is_range_regime:
        if not range_trading_enabled:
            return _make_neutral(
                smc, wyckoff, pa, trend,
                ["RANGE trading is disabled"],
                ["RANGE trading is disabled"],
                regime_result=regime,
                entry_filter=ef,
                direction=candidate,
                candles=candles,
            )
        range_context = evaluate_range_entry(
            candles=candles,
            direction=candidate,
            smc=smc,
            pa=pa,
            confirmation_count=ef.confirmation_count,
            min_confirmations=(
                1
                if price_action_standalone and ef.price_action
                else range_min_confirmations
            ),
            edge_atr_distance=range_edge_atr_distance,
            strict_filters=range_entry_filters_enabled,
            require_edge_position=range_require_edge_position,
            require_liquidity_sweep=False,
        )
        if not range_context.valid:
            return _make_neutral(
                smc, wyckoff, pa, trend,
                [range_context.reason],
                [range_context.reason],
                range_context=range_context,
                regime_result=regime,
                entry_filter=ef,
                direction=candidate,
                candles=candles,
            )

    if candidate == "BUY"  and not regime.rules.allow_long:
        return _make_neutral(smc, wyckoff, pa, trend,
                             [f'Regime "{regime.rules.label}" does not allow LONG'],
                             regime_result=regime,
                             entry_filter=ef,
                             direction=candidate,
                             candles=candles)
    if candidate == "SELL" and not regime.rules.allow_short:
        return _make_neutral(smc, wyckoff, pa, trend,
                             [f'Regime "{regime.rules.label}" does not allow SHORT'],
                             regime_result=regime,
                             entry_filter=ef,
                             direction=candidate,
                             candles=candles)

    last_candle  = candles[-1]
    session      = get_session_quality(last_candle.time)
    divergence   = analyze_divergence(candles)
    # Option 3: DXY is retained as telemetry only and cannot affect entry
    # confidence or the decision. The confidence engine explicitly ignores
    # this legacy compatibility argument.
    conf_result  = calc_confidence(
        smc, wyckoff, pa, trend, regime, session, candidate,
        divergence_signal=divergence.signal,
        entry_policy_only=True,
    )

    if conf_result.confidence < CONF_HARD_MIN:
        n = DecisionResult(
            allowed=False, direction=candidate,  # type: ignore
            confidence=conf_result.confidence, components=conf_result.components,
            grade="REJECTED", regime=regime.regime, regime_label=regime.rules.label,
            regime_rules=regime.rules,
            quality_filter=QualityFilterResult(
                False, [f"Confidence {conf_result.confidence:.1f}% < {CONF_HARD_MIN}% minimum"],
                session, regime.adx, False, False, True, False, False, False),
            blocked_reasons=[f"Confidence {conf_result.confidence:.1f}% < {CONF_HARD_MIN}%"],
            reasoning=conf_result.reasoning, trade_params=None,
            smc=smc, wyckoff=wyckoff, pa=pa, trend=trend,
            entry_filter=ef,
        )
        return n

    # SMC structure is telemetry-only for this policy.  In particular, do not
    # let a stale/missing BOS or CHoCH turn into an entry block.
    last_structure_bar = None
    # Feed the newest BOS/CHoCH bar through the existing quality-filter slot
    # so both event types share the same freshness gate.
    quality  = apply_quality_filter(candles, candidate, conf_result.confidence,
                                    last_structure_bar, regime.adx, regime.atr_ratio,
                                    allow_without_smc=_allow_without_smc_for_quality(
                                        ef,
                                        effective_min_confirmations,
                                        price_action_standalone,
                                    ))
    if not quality.allowed:
        return DecisionResult(
            allowed=False, direction=candidate,  # type: ignore
            confidence=conf_result.confidence, components=conf_result.components,
            grade=conf_result.grade, regime=regime.regime, regime_label=regime.rules.label,
            regime_rules=regime.rules, quality_filter=quality,
            blocked_reasons=quality.blocked_reasons, reasoning=conf_result.reasoning,
            trade_params=None, smc=smc, wyckoff=wyckoff, pa=pa, trend=trend,
            entry_filter=ef,
        )

    # Capital manager inputs
    entry = last_candle.close

    # SMC order blocks, BOS/CHoCH, and equal levels remain visible in
    # telemetry but cannot influence stop sizing or a downstream risk block.
    range_support = (
        range_context.support
        if range_context is not None and range_context.support < entry
        else None
    )
    range_resistance = (
        range_context.resistance
        if range_context is not None and range_context.resistance > entry
        else None
    )

    cap_input = CapitalInput(
        direction=candidate,
        entry_price=entry,
        atr=regime.atr,
        account_balance=account_balance,
        risk_percent=(
            range_risk_percent
            if regime.regime == "RANGE" and range_risk_percent is not None
            else risk_percent
        ),
        take_profit_rr=range_min_rr if regime.regime == "RANGE" else FIXED_TP_RR,
        order_block_top=None,
        order_block_bottom=None,
        swing_high=None,
        swing_low=None,
        support_level=range_support,
        resistance_level=range_resistance,
        take_profit_level=(
            range_resistance if candidate == "BUY" else range_support
        ),
    )
    trade_params = calc_trade_parameters(cap_input)

    # Fixed 0.01-lot policy: allow the configured volume up to the
    # explicit dollar-risk ceiling, independent of the percentage budget.
    if trade_params.min_lot_risk_exceeded:
        risk_reason = (
            f"Fixed lot {trade_params.lot_size:.4f} would risk "
            f"${trade_params.risk_amount:.2f}, above the "
            f"${MAX_FIXED_LOT_RISK_USD:.2f} per-trade risk cap"
        )
        return DecisionResult(
            allowed=False, direction=candidate,  # type: ignore
            confidence=conf_result.confidence, components=conf_result.components,
            grade=conf_result.grade, regime=regime.regime,
            regime_label=regime.rules.label, regime_rules=regime.rules,
            quality_filter=quality,
            blocked_reasons=[risk_reason],
            reasoning=conf_result.reasoning + [risk_reason],
            trade_params=trade_params,
            smc=smc, wyckoff=wyckoff, pa=pa, trend=trend,
            entry_filter=ef,
            divergence=divergence,
            dxy_signal=dxy_signal,
            range_context=range_context,
        )

    # Marginal confidence check
    min_conf = regime.rules.min_confidence
    if conf_result.confidence < min_conf:
        if trade_params.risk_reward_ratio < CONF_MARGINAL_RR:
            return DecisionResult(
                allowed=False, direction=candidate,  # type: ignore
                confidence=conf_result.confidence, components=conf_result.components,
                grade="MARGINAL", regime=regime.regime, regime_label=regime.rules.label,
                regime_rules=regime.rules, quality_filter=quality,
                blocked_reasons=[
                    f"Marginal conf {conf_result.confidence:.1f}% requires R:R ≥ {CONF_MARGINAL_RR} "
                    f"(got {trade_params.risk_reward_ratio:.2f})"
                ],
                reasoning=conf_result.reasoning, trade_params=None,
                smc=smc, wyckoff=wyckoff, pa=pa, trend=trend,
                entry_filter=ef,
            )

    # R:R gate
    required_rr = range_min_rr if regime.regime == "RANGE" else regime.rules.min_rr
    if trade_params.risk_reward_ratio < required_rr:
        return DecisionResult(
            allowed=False, direction=candidate,  # type: ignore
            confidence=conf_result.confidence, components=conf_result.components,
            grade=conf_result.grade, regime=regime.regime, regime_label=regime.rules.label,
            regime_rules=regime.rules, quality_filter=quality,
            blocked_reasons=[
                f"R:R {trade_params.risk_reward_ratio:.2f} < {required_rr} "
                f"minimum for {regime.rules.label}"
            ],
            reasoning=conf_result.reasoning, trade_params=None,
            smc=smc, wyckoff=wyckoff, pa=pa, trend=trend,
            entry_filter=ef,
        )

    # ── TRADE ALLOWED ─────────────────────────────────────────────────────────
    return DecisionResult(
        allowed=True, direction=candidate,  # type: ignore
        confidence=conf_result.confidence, components=conf_result.components,
        grade=conf_result.grade, regime=regime.regime, regime_label=regime.rules.label,
        regime_rules=regime.rules, quality_filter=quality,
        blocked_reasons=[], reasoning=conf_result.reasoning,
        trade_params=trade_params,
        smc=smc, wyckoff=wyckoff, pa=pa, trend=trend,
        entry_filter=ef,
        divergence=divergence,
        dxy_signal=dxy_signal,
        range_context=range_context,
    )


def describe_strategy(decision: "DecisionResult") -> dict:
    """Build a human-readable summary of *why* this trade was taken.

    Purely derived from data the decision engine already computed — it adds
    no new signal logic and cannot change whether a trade is taken. Intended
    to travel alongside a just-opened trade (e.g. published to Redis by the
    live loop) so the Telegram panel can explain the trade in its
    "TRADE OPENED" notification instead of showing only price/volume/SL/TP.
    """
    ef = decision.entry_filter
    _ENGINE_NAMES = {
        "trend":        "Trend (EMA alignment)",
        "price_action": "Price Action",
    }
    if ef is not None:
        confirmations = [
            label for key, label in _ENGINE_NAMES.items() if getattr(ef, key)
        ]
        confirmation_count = ef.confirmation_count
    else:
        confirmations = []
        confirmation_count = 0

    trend_vote = (
        "BUY" if decision.trend.trend == "BULLISH"
        else "SELL" if decision.trend.trend == "BEARISH"
        else "NEUTRAL"
    )
    pa = decision.pa
    components = decision.components
    current_price = float(decision.smc.current_price)

    def _zone_location(low: float, high: float) -> tuple[str, float]:
        if low <= current_price <= high:
            return "INSIDE", 0.0
        if current_price < low:
            return "BELOW", round(low - current_price, 3)
        return "ABOVE", round(current_price - high, 3)

    order_blocks = []
    for ob in decision.smc.order_blocks:
        location, distance = _zone_location(ob.low, ob.high)
        order_blocks.append({
            "type": ob.type,
            "high": ob.high,
            "low": ob.low,
            "bar_index": ob.bar_index,
            "time": ob.time,
            "mitigated": bool(ob.mitigated),
            "mitigation_state": ob.mitigation_state,
            "location": location,
            "distance": distance,
        })

    fair_value_gaps = []
    for fvg in decision.smc.fair_value_gaps:
        location, distance = _zone_location(fvg.bottom, fvg.top)
        fair_value_gaps.append({
            "type": fvg.type,
            "top": fvg.top,
            "bottom": fvg.bottom,
            "bar_index": fvg.bar_index,
            "time": fvg.time,
            "filled": bool(fvg.filled),
            "fill_state": fvg.fill_state,
            "location": location,
            "distance": distance,
        })

    return {
        "direction":           decision.direction,
        "allowed":             bool(decision.allowed),
        "entry_reason":        (
            ef.entry_reason if ef is not None else "BLOCKED_NO_SIGNAL"
        ),
        "grade":               decision.grade,
        "confidence":          round(decision.confidence, 1),
        "regime":              decision.regime,
        "regime_label":        decision.regime_label,
        "confirmations":       confirmations,
        "confirmation_count":  confirmation_count,
        "confirmation_total":  2,
        # Top signal-level reasons behind the confidence score (e.g. "BOS
        # confirmed", "Strong EMA alignment (50/100/200)", "Spring confirmed").
        "signals":             list(decision.reasoning[:6]),
        # Structured per-candle telemetry.  Keep the four decision stages
        # separate so the panel/log consumer can identify where a setup was
        # weakened or blocked without re-running strategy code.
        "consensus": {
            "candidate": decision.direction,
            "engines": {
                "smc": decision.smc.smc_signal,
                "trend": trend_vote,
                "price_action": pa.pa_signal,
                "wyckoff": decision.wyckoff.wyckoff_signal,
            },
            "confirmed": confirmation_count,
            "total": 2,
            "allowed": bool(decision.allowed),
            "entry_reason": (
                ef.entry_reason if ef is not None else "BLOCKED_NO_SIGNAL"
            ),
        },
        "confidence_stage": {
            "total": round(components.total, 1),
            "grade": decision.grade,
            "components": {
                "smc": round(components.smc_score, 2),
                "trend": round(components.trend_score, 2),
                "price_action": round(components.pa_score, 2),
                "wyckoff": round(components.wyckoff_score, 2),
                "liquidity": round(components.liquidity_score, 2),
                "volatility": round(components.volatility_score, 2),
                "divergence": round(components.divergence_score, 2),
                "dxy": round(components.dxy_score, 2),
            },
        },
        "quality_stage": {
            "allowed": bool(decision.quality_filter.allowed),
            "session": decision.quality_filter.session_quality,
            "adx": round(decision.quality_filter.adx, 2),
            "low_momentum": bool(decision.quality_filter.is_low_momentum),
            "low_probability": bool(decision.quality_filter.is_low_probability),
            "fake_breakout": bool(decision.quality_filter.is_fake_breakout),
            "blocked_reasons": list(decision.quality_filter.blocked_reasons),
        },
        "price_action": {
            "signal": pa.pa_signal,
            "score": round(pa.pa_score, 3),
            "engulfing": {
                "bullish": bool(pa.bullish_engulf),
                "bearish": bool(pa.bearish_engulf),
            },
            "breakout": {
                "bullish": bool(pa.valid_bull_breakout),
                "bearish": bool(pa.valid_bear_breakout),
                "fake_bullish": bool(pa.fake_bull_breakout),
                "fake_bearish": bool(pa.fake_bear_breakout),
            },
            "inside_bar": {
                "detected": bool(pa.inside_bar_detected),
                "depth": int(pa.inside_bar_depth),
                "bullish_breakout": bool(pa.bullish_inside_breakout),
                "bearish_breakout": bool(pa.bearish_inside_breakout),
                "breakout_level": pa.inside_bar_breakout_level,
                "strength_atr": round(pa.inside_bar_breakout_strength, 3),
            },
        },
        "smc_zones": {
            "current_price": round(current_price, 3),
            "order_blocks": order_blocks,
            "fair_value_gaps": fair_value_gaps,
        },
    }

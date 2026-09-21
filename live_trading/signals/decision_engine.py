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
    detect_order_block_fake_breakout,
    get_latest_structure_event,
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
    PA_STANDALONE_MIN_SCORE,
    PA_MAX_BREAKOUT_EXTENSION_ATR,
    RANGE_MIN_CONFIRMATIONS,
    RANGE_WEAK_MIN_CONFIRMATIONS,
    REQUIRE_SMC_PRICE_ACTION_WYCKOFF,
    RANGE_ENTRY_FILTERS_ENABLED,
    RANGE_REQUIRE_EDGE_POSITION,
    TREND_MIN_CONFIRMATIONS,
    ENABLED_STRATEGIES,
    SL_ATR_BASE_MULTIPLIER,
    LOW_VOLATILITY_SL_ATR_ADD,
    TARGET_ROOM_BUFFER_ATR,
)

# Marginal confidence R:R floor: trades with confidence between CONF_HARD_MIN
# and the regime minimum must still achieve this R:R to be allowed.
# 1.3 = profitable in expectancy even at 45% win rate (1.3 × 0.45 > 0.55).
CONF_MARGINAL_RR = 1.3

# Trend and Price Action are the only engines with live-entry authority.
# SMC and Wyckoff still run for diagnostics/telemetry, but never participate
# in direction selection, confirmation counting, or mandatory entry gates.
# RANGE keeps its separate structural safeguards.
_CHOPPY_REGIMES = {"ACCUMULATION", "DISTRIBUTION", "HIGH_VOLATILITY"}


def _entry_location_block_reason(candidate: str, pa) -> Optional[str]:
    """Reject entries that arrive at the wrong side of a nearby level.

    A bullish candle at resistance (or a bearish candle at support) is not
    enough evidence for a market entry. Confirmed breakouts and pullback
    reclaims remain eligible, subject to the existing extension and quality
    gates.
    """
    candidate = str(candidate).upper()
    if (
        candidate == "BUY"
        and pa.near_resistance
        and not (pa.valid_bull_breakout or pa.bullish_pullback)
    ):
        return (
            "BUY blocked: price is near resistance without a confirmed "
            "breakout/retest"
        )
    if (
        candidate == "SELL"
        and pa.near_support
        and not (pa.valid_bear_breakout or pa.bearish_pullback)
    ):
        return (
            "SELL blocked: price is near support without a confirmed "
            "breakdown/retest"
        )
    return None


def _target_room_block_reason(
    candidate: str,
    entry: float,
    stop_loss: float,
    structural_level: Optional[float],
    minimum_rr: float,
    atr: float,
) -> Optional[str]:
    """Require usable room beyond the next structural barrier.

    The fixed target is only meaningful when price can reach it before the
    nearest equal high/low. A small ATR buffer accounts for spread and wick
    touches that would otherwise make the advertised R:R misleading.
    """
    if structural_level is None or entry <= 0 or stop_loss <= 0:
        return None
    risk_distance = abs(entry - stop_loss)
    if risk_distance <= 0 or minimum_rr <= 0:
        return None
    candidate = str(candidate).upper()
    room = (
        structural_level - entry
        if candidate == "BUY"
        else entry - structural_level
        if candidate == "SELL"
        else 0.0
    )
    if room <= 0:
        return None
    buffer = max(0.0, float(atr)) * TARGET_ROOM_BUFFER_ATR
    required_room = risk_distance * minimum_rr + buffer
    if room + 1e-9 < required_room:
        return (
            f"{candidate} blocked: structural room {room:.2f} is below "
            f"required {required_room:.2f} for R:R {minimum_rr:.2f}"
        )
    return None


def _effective_min_confirmations(
    base_min_confirmations: int,
    regime: str,
    counter_trend: bool,
    range_min_confirmations: int = RANGE_MIN_CONFIRMATIONS,
    range_weak_min_confirmations: int = RANGE_WEAK_MIN_CONFIRMATIONS,
    strength: str = "",
) -> int:
    """Return the operator-selected two-engine floor.

    Counter-trend and choppy-market handling remain covered by the existing
    confidence, quality, regime, MTF, and risk gates; they do not silently
    turn the configured one-strategy floor into a stricter vote requirement.
    """
    max_confirmations = 2
    base_min_confirmations = min(max(1, int(base_min_confirmations)), max_confirmations)
    range_min_confirmations = min(
        max(1, int(range_min_confirmations)), max_confirmations
    )
    range_weak_min_confirmations = min(
        max(1, int(range_weak_min_confirmations)), max_confirmations
    )
    # The live policy requires both authorized engines. The legacy env floors
    # remain parsed for compatibility/telemetry, but can never downgrade a
    # Trend + Price Action entry to a one-vote entry.
    if regime == "RANGE":
        # RANGE owns its floor; never inherit the ordinary/TREND floor.
        if str(strength).upper() == "WEAK":
            return max(
                max_confirmations,
                range_min_confirmations,
                range_weak_min_confirmations,
            )
        return max(max_confirmations, range_min_confirmations)
    return max(max_confirmations, base_min_confirmations)


def _allow_without_smc_for_quality(
    entry_filter: EntryFilterResult,
    effective_min_confirmations: int,
    price_action_standalone: bool,
) -> bool:
    """Keep the quality gate aligned with the explicit PA standalone policy.

    The entry filter can authorize a directional PA vote by itself. This later
    quality gate must honor that same policy; other strategies still require
    the configured confirmation floor before proceeding without SMC direction.
    """
    if price_action_standalone and entry_filter.price_action:
        return True
    return (
        entry_filter.confirmation_count >= effective_min_confirmations
        and (
            entry_filter.smc
            or entry_filter.trend
            or entry_filter.price_action
            or entry_filter.wyckoff
        )
    )


def _range_confirmation_gate(
    entry_filter: EntryFilterResult,
    min_confirmations: int,
    price_action_standalone: bool = False,
) -> tuple[bool, str]:
    """Apply the RANGE confirmation floor without changing vote semantics.

    RANGE keeps its separate edge, fresh sweep, reversal, R:R, and session
    limits. Its signal vote still follows the same equal-weight two-engine
    consensus as ordinary entries. An explicit standalone Price Action policy
    may reduce this floor to one aligned PA vote; all other RANGE safeguards
    remain mandatory.
    """
    # Strict live policy: exactly two authorized engines are required. Clamp
    # both legacy one-vote and stale three/four-vote callers to the same
    # Trend + Price Action contract.
    effective_min_confirmations = 2
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
    effective_min_confirmations: Optional[int]   = None
    regime_strength: str                         = ""
    policy_regime: str                           = ""


def _candidate_direction(
    smc: SmcResult,
    wyckoff: WyckoffResult,
    pa: PriceActionResult,
    trend: TrendResult,
    price_action_standalone: bool = False,
    enabled_strategies = None,
) -> str:
    """Return the unique direction with the most enabled strategy votes."""
    enabled = set(enabled_strategies or ("trend", "price_action"))
    if (
        price_action_standalone
        and "price_action" in enabled
        and pa.pa_signal in {"BUY", "SELL"}
    ):
        return pa.pa_signal
    votes = {
        "smc": smc.smc_signal,
        "trend": (
            "BUY" if trend.trend == "BULLISH" else
            "SELL" if trend.trend == "BEARISH" else "NEUTRAL"
        ),
        "price_action": pa.pa_signal,
        "wyckoff": wyckoff.wyckoff_signal,
    }
    votes = tuple(
        vote for name, vote in votes.items()
        if name in enabled
    )
    buy_count = sum(vote == "BUY" for vote in votes)
    sell_count = sum(vote == "SELL" for vote in votes)
    if buy_count > sell_count:
        return "BUY"
    if sell_count > buy_count:
        return "SELL"
    return "NEUTRAL"


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
    pa_standalone_min_score: float = PA_STANDALONE_MIN_SCORE,
    range_weak_min_confirmations: int = RANGE_WEAK_MIN_CONFIRMATIONS,
    regime_strength: Optional[str] = None,
    regime_context: Optional[str] = None,
    sl_atr: Optional[float] = None,
    sl_atr_multiplier: float = SL_ATR_BASE_MULTIPLIER,
    low_volatility_sl_atr_add: float = LOW_VOLATILITY_SL_ATR_ADD,
) -> DecisionResult:
    # Strict live policy: PA standalone is retained only as a compatibility
    # argument for old callers. It must never authorize a production order.
    price_action_standalone = False

    smc     = analyze_smc_structure(candles, timeframe=timeframe)
    wyckoff = analyze_wyckoff(candles)
    pa      = analyze_price_action(candles, timeframe=timeframe)
    trend   = analyze_trend(candles)

    candidate = _candidate_direction(
        smc,
        wyckoff,
        pa,
        trend,
        price_action_standalone=price_action_standalone,
        enabled_strategies=ENABLED_STRATEGIES,
    )
    if candidate == "NEUTRAL":
        return _make_neutral(
            smc, wyckoff, pa, trend, ["No unique strategy direction"]
        )

    # Soft EMA gate — counter-trend trades are allowed but need 3 confirmations
    trend_dir = ("BUY" if trend.trend == "BULLISH" else
                 "SELL" if trend.trend == "BEARISH" else "NEUTRAL")
    _counter_trend = (candidate == "BUY" and trend_dir == "SELL") or \
                     (candidate == "SELL" and trend_dir == "BUY")

    # Detect regime early — needed to set the adaptive confirmation threshold.
    regime = detect_market_regime(candles, trend, wyckoff, use_atr_high_vol)
    effective_regime_strength = str(regime_strength or trend.strength).upper()
    policy_regime = str(regime_context or regime.regime).upper()
    local_min_confirmations = _effective_min_confirmations(
        min_confirmations,
        regime.regime,
        _counter_trend,
        range_min_confirmations=range_min_confirmations,
        range_weak_min_confirmations=range_weak_min_confirmations,
        strength=effective_regime_strength,
    )
    policy_min_confirmations = _effective_min_confirmations(
        min_confirmations,
        policy_regime,
        _counter_trend,
        range_min_confirmations=range_min_confirmations,
        range_weak_min_confirmations=range_weak_min_confirmations,
        strength=effective_regime_strength,
    )
    # Preserve the stricter local RANGE floor when the HTF context is a
    # different regime, while also allowing a weak HTF RANGE to raise the
    # ordinary local floor to the configured weak-range value.
    effective_min_confirmations = max(
        local_min_confirmations,
        policy_min_confirmations,
    )

    # Standalone mode is intentionally independent of SMC/Trend/Wyckoff, but
    # it is not a blanket pass for every diagnostic PA pulse.  Require a
    # meaningful local PA score before allowing that one strategy to select
    # the direction on its own.  The later confidence, quality, regime, R:R,
    # position, and risk gates remain unchanged.
    if (
        pa.breakout_overextended
    ):
        extension_reason = (
            f"Price Action breakout extended "
            f"{pa.breakout_extension_atr:.2f} ATR beyond "
            f"{pa.breakout_level:.2f}; wait for retest"
        )
        return _make_neutral(
            smc,
            wyckoff,
            pa,
            trend,
            [extension_reason],
            [extension_reason],
            regime_result=regime,
            direction=pa.pa_signal,
            candles=candles,
        )

    # Entry filter — equal-weight Trend + Price Action consensus.
    # RANGE has a narrower rule than ordinary regimes: SMC plus either
    # Price Action or Wyckoff is sufficient; the global option-1 gate must
    # not turn that dedicated two-confirmation playbook into a three-vote gate.
    is_range_regime = regime.regime == "RANGE"
    is_weak_range = (
        effective_regime_strength == "WEAK"
        and (is_range_regime or policy_regime == "RANGE")
    )
    # The explicit PA standalone override remains available for ordinary
    # RANGE conditions, but weak RANGE must honor its stricter 3-vote floor.
    range_price_action_standalone = False
    # A Trend-aligned entry is more exposed to a single transient EMA signal
    # than a structure/price-action setup. Keep the ordinary operator floor,
    # but require a second independent confirmation whenever Trend votes for
    # the candidate direction. RANGE keeps its dedicated playbook unchanged.
    if (
        not is_range_regime
        and trend_dir == candidate
        and trend_min_confirmations > effective_min_confirmations
    ):
        effective_min_confirmations = trend_min_confirmations
    ef = apply_entry_filter(
        smc_signal      = smc.smc_signal,
        ema_trend       = trend.trend,
        pa_signal       = pa.pa_signal,
        wyckoff_signal  = wyckoff.wyckoff_signal,
        min_confirmations = effective_min_confirmations,
        require_price_action = require_price_action and not is_range_regime,
        # The old three-engine option is intentionally ignored for live
        # entries. SMC and Wyckoff are diagnostic-only under the current
        # two-engine policy.
        require_smc_price_action_wyckoff = False,
        price_action_standalone=price_action_standalone,
        enabled_strategies=ENABLED_STRATEGIES,
    )
    # The RANGE confirmation floor is mandatory even when the optional
    # structural filters (edge, sweep, reversal) are disabled.  Those filters
    # may be informational, but a single strategy vote must never authorize an
    # entry.
    if is_range_regime:
        range_votes_ok, range_votes_reason = _range_confirmation_gate(
            ef,
            effective_min_confirmations,
            price_action_standalone=range_price_action_standalone,
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
    elif not is_range_regime and not ef.allowed:
        votes = (f"SMC={'✓' if ef.smc else '✗'}  "
                 f"Trend={'✓' if ef.trend else '✗'}  "
                 f"PA={'✓' if ef.price_action else '✗'}  "
                 f"Wyckoff={'✓' if ef.wyckoff else '✗'}")
        if (
            require_smc_price_action_wyckoff
            and not (ef.smc and ef.price_action and ef.wyckoff)
        ):
            reason = (
                "Entry filter: Option 1 requires SMC + Price Action + Wyckoff — "
                f"{votes}  [regime={regime.regime}]"
            )
        elif require_price_action and not ef.price_action:
            reason = (f"Entry filter: Price Action confirmation required — "
                      f"{votes}  [regime={regime.regime}]")
        else:
            reason = (f"Entry filter: only {ef.confirmation_count}/{effective_min_confirmations} "
                      f"confirmations — {votes}  [regime={regime.regime}]")
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
            min_confirmations=effective_min_confirmations,
            edge_atr_distance=range_edge_atr_distance,
            strict_filters=range_entry_filters_enabled,
            require_edge_position=range_require_edge_position,
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

    # Protective-stop volatility is intentionally independent from the signal
    # timeframe. The live loop supplies ATR from SL_ATR_TIMEFRAME (normally
    # M5), so a compressed M1 candle cannot create an unrealistically tight
    # stop. LOW_VOLATILITY gets an additional configurable buffer because it
    # can precede a volatility expansion.
    protective_atr = float(sl_atr or regime.atr)
    protective_multiplier = float(sl_atr_multiplier)
    if regime.regime == "LOW_VOLATILITY":
        protective_multiplier += float(low_volatility_sl_atr_add)

    last_candle  = candles[-1]
    session      = get_session_quality(last_candle.time)
    divergence   = analyze_divergence(candles)
    # Option 3: DXY is retained as telemetry only and cannot affect entry
    # confidence or the decision. The confidence engine explicitly ignores
    # this legacy compatibility argument.
    conf_result  = calc_confidence(
        smc, wyckoff, pa, trend, regime, session, candidate,
        divergence_signal=divergence.signal,
        price_action_standalone=price_action_standalone,
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

    latest_structure = get_latest_structure_event(smc)
    last_structure_bar = (latest_structure.bar_index
                          if latest_structure is not None else None)
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

    # Option 2 hard gate: a directional breakout that closes back inside its
    # aligned Order Block is treated as a fake breakout.  Do not let this
    # setup reach capital sizing or the order executor.  The existing quality
    # result is reused so the panel receives the normal filter telemetry plus
    # the explicit rejection flag.
    fake_ob = detect_order_block_fake_breakout(candles, smc, candidate)
    if fake_ob is not None:
        fake_reason = (
            f"Fake Breakout in {fake_ob.type.title()} Order Block "
            f"[{fake_ob.low:.2f}, {fake_ob.high:.2f}] — entry blocked"
        )
        quality.allowed = False
        quality.is_fake_breakout = True
        quality.blocked_reasons.append(fake_reason)
        return DecisionResult(
            allowed=False, direction=candidate,  # type: ignore
            confidence=conf_result.confidence, components=conf_result.components,
            grade=conf_result.grade, regime=regime.regime,
            regime_label=regime.rules.label, regime_rules=regime.rules,
            quality_filter=quality,
            blocked_reasons=[fake_reason],
            reasoning=conf_result.reasoning + [fake_reason],
            trade_params=None,
            smc=smc, wyckoff=wyckoff, pa=pa, trend=trend,
            entry_filter=ef,
            divergence=divergence,
            dxy_signal=dxy_signal,
        )

    location_reason = _entry_location_block_reason(candidate, pa)
    if location_reason is not None:
        return _make_neutral(
            smc,
            wyckoff,
            pa,
            trend,
            [location_reason],
            [location_reason],
            regime_result=regime,
            entry_filter=ef,
            direction=candidate,
            candles=candles,
        )

    # Capital manager inputs
    aligned_obs = [ob for ob in smc.order_blocks
                   if ob.type == ("BULLISH" if candidate == "BUY" else "BEARISH")]
    latest_ob = aligned_obs[-1] if aligned_obs else None

    entry = last_candle.close

    # H-1 FIX: use most-recent directionally-valid BOS price as the SL anchor,
    # not the global max/min across all time.
    # BUY SL anchor: most recent SELL-BOS price below entry (= broken swing low)
    # SELL SL anchor: most recent BUY-BOS price above entry (= broken swing high)
    sell_bos_below = [b.price for b in smc.bos_signals if b.type == "SELL" and b.price < entry]
    buy_bos_above  = [b.price for b in smc.bos_signals if b.type == "BUY"  and b.price > entry]

    # H-2 FIX: populate support/resistance from SMC equal levels (previously always None).
    # Equal lows = institutional demand / support; equal highs = supply / resistance.
    eq_support    = (smc.equal_lows[-1].price
                     if smc.equal_lows  and smc.equal_lows[-1].price  < entry else None)
    eq_resistance = (smc.equal_highs[-1].price
                     if smc.equal_highs and smc.equal_highs[-1].price > entry else None)
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
        atr=protective_atr,
        account_balance=account_balance,
        risk_percent=(
            range_risk_percent
            if regime.regime == "RANGE" and range_risk_percent is not None
            else risk_percent
        ),
        take_profit_rr=range_min_rr if regime.regime == "RANGE" else FIXED_TP_RR,
        order_block_top=latest_ob.high if latest_ob else None,
        order_block_bottom=latest_ob.low if latest_ob else None,
        swing_high=buy_bos_above[-1]  if buy_bos_above  else None,
        swing_low=sell_bos_below[-1]  if sell_bos_below else None,
        support_level=range_support or eq_support,
        resistance_level=range_resistance or eq_resistance,
        take_profit_level=(
            range_resistance if candidate == "BUY" else range_support
        ),
        sl_atr_multiplier=protective_multiplier,
    )
    trade_params = calc_trade_parameters(cap_input)

    # For ordinary trend entries the nearest equal high/low is a hard
    # structural barrier even though the target remains the fixed 2R target.
    # RANGE already supplies its own explicit target and R:R check, so it does
    # not pass through this additional room gate.
    structural_target = (
        eq_resistance if candidate == "BUY" else eq_support
    ) if regime.regime != "RANGE" else None
    required_rr_for_room = regime.rules.min_rr
    room_reason = _target_room_block_reason(
        candidate,
        trade_params.entry_price,
        trade_params.stop_loss,
        structural_target,
        required_rr_for_room,
        protective_atr,
    )
    if room_reason is not None:
        return DecisionResult(
            allowed=False,
            direction=candidate,  # type: ignore
            confidence=conf_result.confidence,
            components=conf_result.components,
            grade=conf_result.grade,
            regime=regime.regime,
            regime_label=regime.rules.label,
            regime_rules=regime.rules,
            quality_filter=quality,
            blocked_reasons=[room_reason],
            reasoning=conf_result.reasoning + [room_reason],
            trade_params=None,
            smc=smc,
            wyckoff=wyckoff,
            pa=pa,
            trend=trend,
            entry_filter=ef,
            divergence=divergence,
            dxy_signal=dxy_signal,
            range_context=range_context,
        )

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
        effective_min_confirmations=effective_min_confirmations,
        regime_strength=effective_regime_strength,
        policy_regime=policy_regime,
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
        "smc":          "Smart Money Concepts (structure)",
        "trend":        "Trend (EMA alignment)",
        "price_action": "Price Action",
        "wyckoff":      "Wyckoff",
    }
    if ef is not None:
        confirmations = [
            _ENGINE_NAMES[key]
            for key in ("trend", "price_action")
            if getattr(ef, key)
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
        # Structured per-candle telemetry. Keep all four signal stages
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
            "allowed": bool(ef.allowed) if ef is not None else False,
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
                "entry_quality": {
                    "breakout_level": getattr(pa, "breakout_level", None),
                    "extension_atr": round(
                        getattr(pa, "breakout_extension_atr", 0.0), 3
                    ),
                    "overextended": bool(
                        getattr(pa, "breakout_overextended", False)
                    ),
                    "max_extension_atr": PA_MAX_BREAKOUT_EXTENSION_ATR,
                },
        },
        "smc_zones": {
            "current_price": round(current_price, 3),
            "order_blocks": order_blocks,
            "fair_value_gaps": fair_value_gaps,
        },
    }

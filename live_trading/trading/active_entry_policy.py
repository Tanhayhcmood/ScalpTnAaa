"""Authoritative Phase 1 live-entry policy.

Signal engines may continue to run for diagnostics and telemetry, but this
module is the only policy that can authorize the live order path.
"""

from dataclasses import dataclass

from live_trading.signals.market_regime import (
    REGIME_RULES,
    STRONG_ADX_TREND_THRESHOLD,
)


ACTIVE_ENTRY_STRATEGY = "XAUUSD_VOLATILITY_TREND_BREAKOUT"
ACTIVE_ENTRY_SYMBOL = "XAUUSD"
ACTIVE_ENTRY_TIMEFRAME = "5m"

@dataclass(frozen=True)
class ActiveEntryPolicyResult:
    allowed: bool
    strategy: str
    reason: str = ""


def _trend_direction(trend) -> str:
    value = str(getattr(trend, "trend", "")).upper().strip()
    if value == "BULLISH":
        return "BUY"
    if value == "BEARISH":
        return "SELL"
    return "NEUTRAL"


def _strong_trend_direction(regime: str) -> str:
    if regime == "STRONG_TREND_BULL":
        return "BUY"
    if regime == "STRONG_TREND_BEAR":
        return "SELL"
    return "NEUTRAL"


def _pa_has_structural_confirmation(pa, direction: str) -> bool:
    if pa is None:
        return False
    if str(getattr(pa, "pa_signal", "")).upper().strip() != direction:
        return False

    if direction == "BUY":
        structural_flags = (
            "valid_bull_breakout",
            "bullish_inside_breakout",
            "bullish_pullback",
        )
    else:
        structural_flags = (
            "valid_bear_breakout",
            "bearish_inside_breakout",
            "bearish_pullback",
        )
    return any(bool(getattr(pa, flag, False)) for flag in structural_flags)


def strong_trend_validation_reason(decision) -> str | None:
    """Return a fail-closed reason for an unconfirmed ADX-driven strong regime.

    This is intentionally a final-order-only validation.  It does not classify
    regimes or change the Trend/Price Action engines.  Regimes below the
    existing ADX threshold are outside this guard and retain their current
    behavior.
    """
    regime = str(getattr(decision, "regime", "")).upper().strip()
    expected_direction = _strong_trend_direction(regime)
    if expected_direction == "NEUTRAL":
        return None

    quality = getattr(decision, "quality_filter", None)
    raw_adx = getattr(quality, "adx", None)
    try:
        adx = float(raw_adx)
    except (TypeError, ValueError):
        return (
            "Strong Trend final validation blocked: ADX is missing or invalid "
            f"for {regime}"
        )

    # ADX < 45 deliberately stays on the existing path.  The detector's
    # current 45 threshold is the boundary for this additional validation.
    if adx < STRONG_ADX_TREND_THRESHOLD:
        return None

    direction = str(getattr(decision, "direction", "")).upper().strip()
    if direction != expected_direction:
        return (
            "Strong Trend final validation blocked: decision direction "
            f"{direction or 'UNKNOWN'} does not match regime direction "
            f"{expected_direction}"
        )

    trend_direction = _trend_direction(getattr(decision, "trend", None))
    if trend_direction == expected_direction:
        return None

    if _pa_has_structural_confirmation(
        getattr(decision, "pa", None),
        expected_direction,
    ):
        return None

    return (
        "Strong Trend final validation blocked: ADX "
        f"{adx:.1f} >= {STRONG_ADX_TREND_THRESHOLD:.0f} for {regime}, "
        f"but Trend Engine is {trend_direction} and no structural Price "
        f"Action confirmation exists for {expected_direction}"
    )


def evaluate_active_entry(
    decision,
    *,
    symbol: str,
    timeframe: str,
) -> ActiveEntryPolicyResult:
    """Return whether ``decision`` belongs to the one live strategy.

    Either Trend or Price Action may authorize the direction through its own
    standalone gate. SMC and Wyckoff remain diagnostic-only and cannot
    authorize or veto this policy.
    """
    normalized_symbol = str(symbol or "").upper().strip()
    if normalized_symbol != ACTIVE_ENTRY_SYMBOL:
        return ActiveEntryPolicyResult(
            False,
            ACTIVE_ENTRY_STRATEGY,
            f"{ACTIVE_ENTRY_STRATEGY} blocked: symbol {normalized_symbol or 'UNKNOWN'} "
            f"is not {ACTIVE_ENTRY_SYMBOL}",
        )

    normalized_timeframe = str(timeframe or "").strip().lower()
    if normalized_timeframe != ACTIVE_ENTRY_TIMEFRAME:
        return ActiveEntryPolicyResult(
            False,
            ACTIVE_ENTRY_STRATEGY,
            f"{ACTIVE_ENTRY_STRATEGY} blocked: execution timeframe must remain "
            f"{ACTIVE_ENTRY_TIMEFRAME} (got {timeframe or 'UNKNOWN'})",
        )

    if not bool(getattr(decision, "allowed", False)):
        return ActiveEntryPolicyResult(
            False,
            ACTIVE_ENTRY_STRATEGY,
            f"{ACTIVE_ENTRY_STRATEGY} blocked: decision engine did not approve the entry",
        )

    regime = str(getattr(decision, "regime", "")).upper().strip()
    if regime not in REGIME_RULES:
        return ActiveEntryPolicyResult(
            False,
            ACTIVE_ENTRY_STRATEGY,
            f"{ACTIVE_ENTRY_STRATEGY} blocked: regime {regime or 'UNKNOWN'} "
            "is not a supported market regime",
        )

    direction = str(getattr(decision, "direction", "")).upper().strip()
    if direction not in {"BUY", "SELL"}:
        return ActiveEntryPolicyResult(
            False,
            ACTIVE_ENTRY_STRATEGY,
            f"{ACTIVE_ENTRY_STRATEGY} blocked: direction is not BUY or SELL",
        )

    trend_direction = _trend_direction(getattr(decision, "trend", None))
    entry_filter = getattr(decision, "entry_filter", None)
    trend_confirmed = bool(getattr(entry_filter, "trend", False))
    pa_direction = str(
        getattr(getattr(decision, "pa", None), "pa_signal", "")
    ).upper()
    pa_standalone_confirmed = (
        bool(getattr(entry_filter, "price_action", False))
        and direction == pa_direction
        and not trend_confirmed
    )
    if trend_direction != direction and not pa_standalone_confirmed:
        return ActiveEntryPolicyResult(
            False,
            ACTIVE_ENTRY_STRATEGY,
            f"{ACTIVE_ENTRY_STRATEGY} blocked: counter-trend direction "
            f"{direction} vs trend {trend_direction}",
        )

    if entry_filter is None or not (trend_confirmed or pa_standalone_confirmed):
        return ActiveEntryPolicyResult(
            False,
            ACTIVE_ENTRY_STRATEGY,
            f"{ACTIVE_ENTRY_STRATEGY} blocked: Trend or Price Action "
            "confirmation is required",
        )

    return ActiveEntryPolicyResult(True, ACTIVE_ENTRY_STRATEGY)

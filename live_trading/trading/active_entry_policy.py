"""
Authoritative live-entry policy for XAUUSD M1 directional scalp entries.

A live order requires XAUUSD and M1 execution, a directional trend regime,
aligned Trend and structural Price Action confirmation, and usable ADX data.
SMC/Wyckoff remain diagnostic-only.
"""

from dataclasses import dataclass

from live_trading.signals.market_regime import (
    REGIME_RULES,
    STRONG_ADX_TREND_THRESHOLD,
)
from live_trading.utils import canonical_symbol


ACTIVE_ENTRY_STRATEGY = "XAUUSD_M1_TREND_PA_SCALP"
ACTIVE_ENTRY_SYMBOL = "XAUUSD"
ACTIVE_ENTRY_TIMEFRAME = "1m"
MINIMUM_DIRECTIONAL_ADX = 18.0

_ALLOWED_SCALP_REGIMES = frozenset({
    "STRONG_TREND_BULL",
    "STRONG_TREND_BEAR",
    "WEAK_TREND_BULL",
    "WEAK_TREND_BEAR",
    "PULLBACK_BULL",
    "PULLBACK_BEAR",
})


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


def _pa_has_structural_confirmation(pa, direction: str) -> bool:
    if pa is None:
        return False
    if str(getattr(pa, "pa_signal", "")).upper().strip() != direction:
        return False

    flags = (
        ("valid_bull_breakout", "bullish_inside_breakout", "bullish_pullback")
        if direction == "BUY"
        else ("valid_bear_breakout", "bearish_inside_breakout", "bearish_pullback")
    )
    return any(bool(getattr(pa, flag, False)) for flag in flags)


def _decision_adx(decision) -> float | None:
    quality = getattr(decision, "quality_filter", None)
    raw_adx = getattr(quality, "adx", None)
    try:
        adx = float(raw_adx)
    except (TypeError, ValueError):
        return None
    if adx < 0:
        return None
    return adx


def strong_trend_validation_reason(decision) -> str | None:
    """Fail closed for an unconfirmed ADX-driven strong trend."""
    regime = str(getattr(decision, "regime", "")).upper().strip()
    expected_direction = (
        "BUY" if regime == "STRONG_TREND_BULL"
        else "SELL" if regime == "STRONG_TREND_BEAR"
        else "NEUTRAL"
    )
    if expected_direction == "NEUTRAL":
        return None

    adx = _decision_adx(decision)
    if adx is None:
        return (
            "Strong Trend final validation blocked: ADX is missing or invalid "
            f"for {regime}"
        )
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
        getattr(decision, "pa", None), expected_direction
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
    raw_symbol = str(symbol or "").strip().upper()
    normalized_symbol = canonical_symbol(symbol)
    if normalized_symbol != ACTIVE_ENTRY_SYMBOL:
        return ActiveEntryPolicyResult(
            False,
            ACTIVE_ENTRY_STRATEGY,
            f"{ACTIVE_ENTRY_STRATEGY} blocked: symbol raw={raw_symbol!r} "
            f"canonical={normalized_symbol!r} allowed={ACTIVE_ENTRY_SYMBOL!r}",
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
            f"{ACTIVE_ENTRY_STRATEGY} blocked: regime {regime or 'UNKNOWN'} is unsupported",
        )
    if regime not in _ALLOWED_SCALP_REGIMES:
        return ActiveEntryPolicyResult(
            False,
            ACTIVE_ENTRY_STRATEGY,
            f"{ACTIVE_ENTRY_STRATEGY} blocked: regime {regime or 'UNKNOWN'} "
            "is not a directional XAUUSD scalp setup",
        )

    direction = str(getattr(decision, "direction", "")).upper().strip()
    if direction not in {"BUY", "SELL"}:
        return ActiveEntryPolicyResult(
            False,
            ACTIVE_ENTRY_STRATEGY,
            f"{ACTIVE_ENTRY_STRATEGY} blocked: direction is not BUY or SELL",
        )

    adx = _decision_adx(decision)
    if adx is None:
        return ActiveEntryPolicyResult(
            False,
            ACTIVE_ENTRY_STRATEGY,
            f"{ACTIVE_ENTRY_STRATEGY} blocked: ADX is missing or invalid",
        )
    if adx < MINIMUM_DIRECTIONAL_ADX:
        return ActiveEntryPolicyResult(
            False,
            ACTIVE_ENTRY_STRATEGY,
            f"{ACTIVE_ENTRY_STRATEGY} blocked: ADX {adx:.1f} is below "
            f"minimum {MINIMUM_DIRECTIONAL_ADX:.1f}",
        )

    entry_filter = getattr(decision, "entry_filter", None)
    if entry_filter is None:
        return ActiveEntryPolicyResult(
            False,
            ACTIVE_ENTRY_STRATEGY,
            f"{ACTIVE_ENTRY_STRATEGY} blocked: entry confirmation data is missing",
        )

    trend_confirmed = bool(getattr(entry_filter, "trend", False))
    pa_confirmed = bool(getattr(entry_filter, "price_action", False))
    if not trend_confirmed or not pa_confirmed:
        return ActiveEntryPolicyResult(
            False,
            ACTIVE_ENTRY_STRATEGY,
            f"{ACTIVE_ENTRY_STRATEGY} blocked: Trend and Price Action "
            "confirmation are both required",
        )

    pa = getattr(decision, "pa", None)
    pa_direction = str(getattr(pa, "pa_signal", "")).upper().strip()
    if pa_direction != direction:
        return ActiveEntryPolicyResult(
            False,
            ACTIVE_ENTRY_STRATEGY,
            f"{ACTIVE_ENTRY_STRATEGY} blocked: Price Action direction "
            f"{pa_direction or 'NEUTRAL'} does not match {direction}",
        )
    if not _pa_has_structural_confirmation(pa, direction):
        return ActiveEntryPolicyResult(
            False,
            ACTIVE_ENTRY_STRATEGY,
            f"{ACTIVE_ENTRY_STRATEGY} blocked: Price Action requires a "
            "closed breakout, inside-bar breakout, or pullback/reclaim",
        )

    trend_direction = _trend_direction(getattr(decision, "trend", None))
    if trend_direction != direction:
        return ActiveEntryPolicyResult(
            False,
            ACTIVE_ENTRY_STRATEGY,
            f"{ACTIVE_ENTRY_STRATEGY} blocked: Trend and entry direction "
            f"disagree ({trend_direction} vs {direction})",
        )

    strong_reason = strong_trend_validation_reason(decision)
    if strong_reason is not None:
        return ActiveEntryPolicyResult(False, ACTIVE_ENTRY_STRATEGY, strong_reason)

    return ActiveEntryPolicyResult(True, ACTIVE_ENTRY_STRATEGY)

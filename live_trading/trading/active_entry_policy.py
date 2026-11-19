"""
Authoritative Phase 1 live-entry policy for XAUUSD M5 volatility/scalp entries.

This policy is deliberately fail-closed: a live order requires Trend and
Price Action agreement, a usable directional market regime, and structural
Price Action confirmation.  SMC/Wyckoff remain diagnostic only.
"""

from dataclasses import dataclass

from live_trading.signals.market_regime import REGIME_RULES
from live_trading.utils import canonical_symbol


ACTIVE_ENTRY_STRATEGY = "XAUUSD_M5_TREND_PA_SCALP"
ACTIVE_ENTRY_SYMBOL = "XAUUSD"
ACTIVE_ENTRY_TIMEFRAME = "5m"

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

    if direction == "BUY":
        flags = ("valid_bull_breakout", "bullish_inside_breakout", "bullish_pullback")
    else:
        flags = ("valid_bear_breakout", "bearish_inside_breakout", "bearish_pullback")
    return any(bool(getattr(pa, flag, False)) for flag in flags)


def strong_trend_validation_reason(decision) -> str | None:
    """Keep the existing STRONG-ADX guard as an additional final-order check."""
    return None


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
            f"{ACTIVE_ENTRY_STRATEGY} blocked: regime {regime or 'UNKNOWN'} is not a directional XAUUSD scalp setup",
        )

    direction = str(getattr(decision, "direction", "")).upper().strip()
    if direction not in {"BUY", "SELL"}:
        return ActiveEntryPolicyResult(
            False,
            ACTIVE_ENTRY_STRATEGY,
            f"{ACTIVE_ENTRY_STRATEGY} blocked: direction is not BUY or SELL",
        )

    entry_filter = getattr(decision, "entry_filter", None)
    trend_confirmed = bool(getattr(entry_filter, "trend", False))
    pa_confirmed = bool(getattr(entry_filter, "price_action", False))

    pa = getattr(decision, "pa", None)
    pa_direction = str(getattr(pa, "pa_signal", "")).upper().strip()

    if not trend_confirmed or not pa_confirmed:
        return ActiveEntryPolicyResult(
            False,
            ACTIVE_ENTRY_STRATEGY,
            f"{ACTIVE_ENTRY_STRATEGY} blocked: Trend and Price Action confirmation are both required",
        )
    if pa_direction != direction:
        return ActiveEntryPolicyResult(
            False,
            ACTIVE_ENTRY_STRATEGY,
            f"{ACTIVE_ENTRY_STRATEGY} blocked: Price Action direction {pa_direction or 'NEUTRAL'} does not match {direction}",
        )
    if not _pa_has_structural_confirmation(pa, direction):
        return ActiveEntryPolicyResult(
            False,
            ACTIVE_ENTRY_STRATEGY,
            f"{ACTIVE_ENTRY_STRATEGY} blocked: Price Action requires a closed breakout, inside-bar breakout, or pullback/reclaim",
        )

    trend_direction = _trend_direction(getattr(decision, "trend", None))
    if trend_direction != direction:
        return ActiveEntryPolicyResult(
            False,
            ACTIVE_ENTRY_STRATEGY,
            f"{ACTIVE_ENTRY_STRATEGY} blocked: Trend and entry direction disagree ({trend_direction} vs {direction})",
        )

    return ActiveEntryPolicyResult(True, ACTIVE_ENTRY_STRATEGY)

"""Authoritative Phase 1 live-entry policy.

Signal engines may continue to run for diagnostics and telemetry, but this
module is the only policy that can authorize the live order path.
"""

from dataclasses import dataclass


ACTIVE_ENTRY_STRATEGY = "XAUUSD_VOLATILITY_TREND_BREAKOUT"
ACTIVE_ENTRY_SYMBOL = "XAUUSD"
ACTIVE_ENTRY_TIMEFRAME = "5m"

# RANGE is allowed by the active strategy alongside the directional volatility
# and trend regimes; pullback, Wyckoff, accumulation/distribution, and
# low-volatility paths are not separate strategy identities.
_ACTIVE_REGIMES = frozenset(
    {
        "HIGH_VOLATILITY",
        "RANGE",
        "STRONG_TREND_BULL",
        "STRONG_TREND_BEAR",
        "WEAK_TREND_BULL",
        "WEAK_TREND_BEAR",
    }
)


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


def evaluate_active_entry(
    decision,
    *,
    symbol: str,
    timeframe: str,
) -> ActiveEntryPolicyResult:
    """Return whether ``decision`` belongs to the one live strategy.

    This is deliberately stricter than the legacy multi-engine decision
    result. Trend must agree with the direction unless the entry filter
    approved a score-qualified standalone Price Action entry. SMC and Wyckoff
    remain diagnostic-only and cannot authorize or veto this policy.
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
    if regime not in _ACTIVE_REGIMES:
        return ActiveEntryPolicyResult(
            False,
            ACTIVE_ENTRY_STRATEGY,
            f"{ACTIVE_ENTRY_STRATEGY} blocked: regime {regime or 'UNKNOWN'} "
            "is not an active volatility/trend regime",
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
    pa_standalone_confirmed = (
        trend_direction == "NEUTRAL"
        and bool(getattr(entry_filter, "price_action", False))
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
            f"{ACTIVE_ENTRY_STRATEGY} blocked: Trend confirmation is required",
        )

    return ActiveEntryPolicyResult(True, ACTIVE_ENTRY_STRATEGY)

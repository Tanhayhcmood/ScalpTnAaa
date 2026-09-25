"""Entry Filter — independent Trend and Price Action live-entry authorization.

Agreement is preferred, but either eligible engine can authorize a trade using
its own standalone threshold. SMC and Wyckoff remain diagnostic inputs only.
"""
from dataclasses import dataclass
from typing import Iterable, Literal

from live_trading.config import MIN_CONFIRMATIONS, PA_STANDALONE_MIN_SCORE

ENTRY_STRATEGIES = ("trend", "price_action")
ALL_STRATEGIES = ENTRY_STRATEGIES


@dataclass
class EntryFilterResult:
    allowed: bool
    direction: Literal["BUY", "SELL", "NEUTRAL"]
    confirmation_count: int
    smc: bool
    trend: bool
    price_action: bool
    wyckoff: bool


def _vote(value: str) -> str:
    if value in {"BUY", "SELL"}:
        return value
    return "NEUTRAL"


def select_independent_direction(
    trend_direction: str,
    pa_direction: str,
    *,
    trend_standalone: bool = False,
    trend_score: float = 0.0,
    trend_standalone_min_score: float = 55.0,
    price_action_standalone: bool = False,
    pa_score: float = 0.0,
    pa_standalone_min_score: float = PA_STANDALONE_MIN_SCORE,
) -> str:
    """Choose an eligible engine without requiring the other engine to agree.

    Agreement still produces the strongest signal. When the engines disagree,
    each one is evaluated against its own standalone gate; the stronger
    standalone margin wins so a weak signal cannot veto a qualified one.
    """
    trend_direction = _vote(trend_direction)
    pa_direction = _vote(pa_direction)

    try:
        numeric_trend_score = float(trend_score)
    except (TypeError, ValueError):
        numeric_trend_score = 0.0
    try:
        numeric_pa_score = float(pa_score)
    except (TypeError, ValueError):
        numeric_pa_score = 0.0

    trend_floor = max(0.0, float(trend_standalone_min_score))
    pa_floor = min(float(pa_standalone_min_score), PA_STANDALONE_MIN_SCORE)
    trend_standalone_ok = (
        trend_standalone
        and trend_direction in {"BUY", "SELL"}
        and (
            numeric_trend_score >= trend_floor
            if trend_direction == "BUY"
            else numeric_trend_score <= -trend_floor
        )
    )
    pa_standalone_ok = (
        price_action_standalone
        and pa_direction in {"BUY", "SELL"}
        and numeric_pa_score >= pa_floor
    )

    if trend_direction in {"BUY", "SELL"} and trend_direction == pa_direction:
        return trend_direction
    if trend_standalone_ok and not pa_standalone_ok:
        return trend_direction
    if pa_standalone_ok and not trend_standalone_ok:
        return pa_direction
    if trend_standalone_ok and pa_standalone_ok:
        trend_margin = abs(numeric_trend_score) - trend_floor
        pa_margin = numeric_pa_score - pa_floor
        return trend_direction if trend_margin >= pa_margin * 100.0 else pa_direction
    return "NEUTRAL"


def apply_entry_filter(
    smc_signal: str,
    ema_trend: str,        # BULLISH / BEARISH / NEUTRAL
    pa_signal: str,
    wyckoff_signal: str,
    min_confirmations: int = MIN_CONFIRMATIONS,
    require_price_action: bool = False,
    require_smc_price_action_wyckoff: bool = False,
    price_action_standalone: bool = False,
    enabled_strategies: Iterable[str] | None = None,
    trend_score: float = 0.0,
    trend_standalone: bool = False,
    trend_standalone_min_score: float = 55.0,
    pa_score: float = 0.0,
    pa_standalone_min_score: float = PA_STANDALONE_MIN_SCORE,
) -> EntryFilterResult:
    """Allow aligned Trend/Price Action votes or an explicitly strong standalone.

    The legacy policy arguments remain in the signature for callers that have
    not migrated yet, but they cannot re-enable diagnostic engines as live
    entry authorities.
    """
    # ``enabled_strategies`` is retained for API compatibility only. The live
    # authority is intentionally fixed here so stale caller/config values
    # cannot restore SMC or Wyckoff voting.
    enabled = set(ENTRY_STRATEGIES)
    required_confirmations = min(
        max(1, int(min_confirmations)),
        len(ENTRY_STRATEGIES),
    )
    raw_votes = {
        "smc": _vote(smc_signal),
        "trend": _vote(
            "BUY" if ema_trend == "BULLISH" else
            "SELL" if ema_trend == "BEARISH" else "NEUTRAL"
        ),
        "price_action": _vote(pa_signal),
        "wyckoff": _vote(wyckoff_signal),
    }
    # Keep diagnostic engines visible to the caller as telemetry inputs, but
    # remove their votes from direction selection and confirmation counting.
    votes = {
        name: value if name in enabled else "NEUTRAL"
        for name, value in raw_votes.items()
    }
    trend_direction = votes["trend"]
    pa_direction = votes["price_action"]

    try:
        numeric_trend_score = float(trend_score)
    except (TypeError, ValueError):
        numeric_trend_score = 0.0
    try:
        numeric_pa_score = float(pa_score)
    except (TypeError, ValueError):
        numeric_pa_score = 0.0

    direction = select_independent_direction(
        trend_direction,
        pa_direction,
        trend_standalone=trend_standalone,
        trend_score=numeric_trend_score,
        trend_standalone_min_score=trend_standalone_min_score,
        price_action_standalone=price_action_standalone,
        pa_score=numeric_pa_score,
        pa_standalone_min_score=pa_standalone_min_score,
    )
    if direction == "NEUTRAL":
        return EntryFilterResult(
            allowed=False,
            direction="NEUTRAL",
            confirmation_count=0,
            smc=False,
            trend=False,
            price_action=False,
            wyckoff=False,
        )

    count = sum(votes[name] == direction for name in ENTRY_STRATEGIES)
    smc_ok = votes["smc"] == direction
    trend_ok = votes["trend"] == direction
    pa_ok = votes["price_action"] == direction
    wyc_ok = votes["wyckoff"] == direction

    effective_pa_standalone_min_score = min(
        float(pa_standalone_min_score), PA_STANDALONE_MIN_SCORE
    )
    aligned_trend_score = (
        numeric_trend_score >= trend_standalone_min_score
        if trend_direction == "BUY"
        else numeric_trend_score <= -trend_standalone_min_score
    )
    trend_standalone_ok = (
        trend_standalone
        and trend_ok
        and aligned_trend_score
    )
    pa_standalone_ok = (
        price_action_standalone
        and pa_ok
        and numeric_pa_score >= effective_pa_standalone_min_score
    )

    # A one-vote entry can only use one of the explicit standalone paths.
    # SMC and Wyckoff remain excluded from both vote counts and authorization.
    allowed = (
        (
            count >= required_confirmations
            and (count > 1 or pa_standalone_ok or trend_standalone_ok)
        )
        or pa_standalone_ok
        or trend_standalone_ok
    )

    return EntryFilterResult(
        allowed=allowed,
        direction=direction if allowed else "NEUTRAL",  # type: ignore
        confirmation_count=count,
        smc=smc_ok,
        trend=trend_ok,
        price_action=pa_ok,
        wyckoff=wyc_ok,
    )

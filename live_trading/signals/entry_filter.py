"""Entry Filter — Trend and Price Action live-entry consensus.

Trend supplies the candidate direction. Price Action can confirm or oppose it.
SMC and Wyckoff remain diagnostic inputs only and never participate in the
entry vote.
"""
from dataclasses import dataclass
from typing import Iterable, Literal

MIN_CONFIRMATIONS = 1
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
) -> EntryFilterResult:
    """Allow an entry from Trend/Price Action votes only.

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
    direction = votes["trend"]
    pa_direction = votes["price_action"]
    if direction == "NEUTRAL" or (
        pa_direction in {"BUY", "SELL"} and pa_direction != direction
    ):
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

    # Only Trend and Price Action participate in live-entry authorization.
    # Legacy SMC/Wyckoff requirements remain intentionally ignored.
    allowed = count >= required_confirmations

    return EntryFilterResult(
        allowed=allowed,
        direction=direction if allowed else "NEUTRAL",  # type: ignore
        confirmation_count=count,
        smc=smc_ok,
        trend=trend_ok,
        price_action=pa_ok,
        wyckoff=wyc_ok,
    )

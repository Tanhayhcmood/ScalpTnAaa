"""Entry Filter — Trend-only live entry confirmation policy.

Trend is the only engine that can authorize a live entry. Price Action, SMC,
and Wyckoff remain available to callers for diagnostics and confidence scoring,
but are excluded from direction selection and confirmation counting.
"""
from dataclasses import dataclass
from typing import Iterable, Literal

MIN_CONFIRMATIONS = 1
ENTRY_STRATEGIES = ("trend",)
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
    """Allow an entry when Trend confirms the candidate direction.

    The legacy policy arguments remain in the signature for callers that have
    not migrated yet, but they cannot re-enable diagnostic engines as live
    entry authorities.
    """
    # ``enabled_strategies`` is retained for API compatibility only. The live
    # authority is intentionally fixed here so stale caller/config values
    # cannot restore Price Action, SMC, or Wyckoff voting.
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
    # Keep disabled engines visible to the caller as telemetry inputs, but
    # remove their votes from direction selection and confirmation counting.
    votes = {
        name: value if name in enabled else "NEUTRAL"
        for name, value in raw_votes.items()
    }
    buy_count = sum(value == "BUY" for value in votes.values())
    sell_count = sum(value == "SELL" for value in votes.values())

    if buy_count > sell_count:
        direction = "BUY"
        count = buy_count
    elif sell_count > buy_count:
        direction = "SELL"
        count = sell_count
    else:
        return EntryFilterResult(
            allowed=False,
            direction="NEUTRAL",
            confirmation_count=max(buy_count, sell_count),
            smc=False,
            trend=False,
            price_action=False,
            wyckoff=False,
        )

    smc_ok = votes["smc"] == direction
    trend_ok = votes["trend"] == direction
    pa_ok = votes["price_action"] == direction
    wyc_ok = votes["wyckoff"] == direction

    # Only the Trend vote participates in this live-entry decision. The
    # legacy Price Action/SMC/Wyckoff requirements are intentionally ignored.
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

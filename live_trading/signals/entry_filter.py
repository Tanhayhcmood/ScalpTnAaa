"""Entry Filter — four-engine confirmation policy.

Ordinary entries require the configured number of aligned engines. The live
configuration can restrict which engines are allowed to authorize entries;
disabled engines remain available for diagnostics but do not vote.
"""
from dataclasses import dataclass
from typing import Iterable, Literal

MIN_CONFIRMATIONS = 2
ALL_STRATEGIES = ("smc", "trend", "price_action", "wyckoff")


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
    """Allow an entry when the configured strategy policy is satisfied.

    With ``price_action_standalone`` enabled, a directional Price Action vote
    selects the candidate and may pass without SMC, Trend, or Wyckoff. The
    production configuration keeps that override disabled.
    """
    enabled = set(enabled_strategies or ALL_STRATEGIES)
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

    standalone_pa = (
        price_action_standalone
        and votes["price_action"] in {"BUY", "SELL"}
    )
    if standalone_pa:
        direction = votes["price_action"]
        count = sum(value == direction for value in votes.values())
    elif buy_count > sell_count:
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

    if require_smc_price_action_wyckoff:
        # Explicit legacy/operator override. Production defaults to false.
        allowed = smc_ok and pa_ok and wyc_ok
    elif standalone_pa:
        allowed = pa_ok
    else:
        allowed = count >= min_confirmations and (
            not require_price_action or pa_ok
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

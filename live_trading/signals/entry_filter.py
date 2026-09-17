"""Entry Filter — the Price Action + Trend entry policy.

SMC, Wyckoff, divergence, DXY, and other engines may still be calculated for
telemetry, but only Price Action and EMA Trend are allowed to authorize an
entry.  The policy is intentionally explicit instead of being an N-of-4 vote:

* Price Action alone may authorize an entry.
* Trend alone may never authorize an entry.
* When both are directional they must agree.
"""
from dataclasses import dataclass
from typing import Literal

MIN_CONFIRMATIONS = 2


@dataclass
class EntryFilterResult:
    allowed: bool
    direction: Literal["BUY", "SELL", "NEUTRAL"]
    confirmation_count: int
    smc: bool
    trend: bool
    price_action: bool
    wyckoff: bool
    entry_reason: Literal[
        "PA_STANDALONE",
        "PA+TREND_ALIGNED",
        "BLOCKED_TREND_ONLY",
        "BLOCKED_CONFLICT",
        "BLOCKED_NO_SIGNAL",
    ] = "BLOCKED_NO_SIGNAL"


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
) -> EntryFilterResult:
    """Apply the fixed Price Action + EMA Trend entry policy.

    The legacy parameters remain in the signature so older callers and
    operator payloads stay source-compatible.  They are deliberately ignored:
    no SMC/Wyckoff vote, confirmation floor, or legacy option can override the
    policy requested for the live robot.
    """
    del smc_signal, wyckoff_signal, min_confirmations
    del require_price_action, require_smc_price_action_wyckoff
    del price_action_standalone

    pa_vote = _vote(pa_signal)
    trend_vote = _vote(
        "BUY" if ema_trend == "BULLISH" else
        "SELL" if ema_trend == "BEARISH" else "NEUTRAL"
    )

    pa_directional = pa_vote in {"BUY", "SELL"}
    trend_directional = trend_vote in {"BUY", "SELL"}

    if pa_directional and not trend_directional:
        return EntryFilterResult(
            allowed=True,
            direction=pa_vote,  # type: ignore[arg-type]
            confirmation_count=1,
            smc=False,
            trend=False,
            price_action=True,
            wyckoff=False,
            entry_reason="PA_STANDALONE",
        )

    if not pa_directional and trend_directional:
        return EntryFilterResult(
            allowed=False,
            direction=trend_vote,  # type: ignore[arg-type]
            confirmation_count=1,
            smc=False,
            trend=True,
            price_action=False,
            wyckoff=False,
            entry_reason="BLOCKED_TREND_ONLY",
        )

    if pa_directional and trend_directional:
        if pa_vote == trend_vote:
            return EntryFilterResult(
                allowed=True,
                direction=pa_vote,  # type: ignore[arg-type]
                confirmation_count=2,
                smc=False,
                trend=True,
                price_action=True,
                wyckoff=False,
                entry_reason="PA+TREND_ALIGNED",
            )
        return EntryFilterResult(
            allowed=False,
            direction="NEUTRAL",
            confirmation_count=0,
            smc=False,
            trend=False,
            price_action=False,
            wyckoff=False,
            entry_reason="BLOCKED_CONFLICT",
        )

    return EntryFilterResult(
        allowed=False,
        direction="NEUTRAL",
        confirmation_count=0,
        smc=False,
        trend=False,
        price_action=False,
        wyckoff=False,
        entry_reason="BLOCKED_NO_SIGNAL",
    )

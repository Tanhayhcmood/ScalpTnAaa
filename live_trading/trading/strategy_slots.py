"""One-open-position capacity for the single live strategy.

Positions opened before strategy-slot tagging was introduced are treated as
occupying every slot.  That fail-closed behavior prevents a legacy position
from being duplicated while the robot cannot identify its owner.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping


ACTIVE_STRATEGY_SLOT = "xauusd_volatility_trend_breakout"

# The first slot is the only slot that can be claimed by a new live decision.
# Legacy slots remain understood so positions opened in earlier phases still
# fail closed instead of allowing the active strategy to duplicate them.
STRATEGY_SLOTS = (
    ACTIVE_STRATEGY_SLOT,
    "smc",
    "trend",
    "price_action",
    "wyckoff",
)
_SLOT_CODES = {
    ACTIVE_STRATEGY_SLOT: "XVTB",
    "smc": "SMC",
    "trend": "TRD",
    "price_action": "PA",
    "wyckoff": "WYC",
}
_CODE_TO_SLOT = {code: slot for slot, code in _SLOT_CODES.items()}
_SLOT_PATTERN = re.compile(r"(?:^|\|)S=([A-Z,]+)(?:\||$)")


def _normalized_direction(position: Mapping) -> str:
    """Return a broker position direction in the small set used by the bot."""
    raw = position.get("type", position.get("direction", ""))
    direction = str(raw or "").upper().strip()
    return direction if direction in {"BUY", "SELL"} else "UNKNOWN"


def opposite_direction(direction: str) -> str:
    """Return the only direction that is incompatible with a one-way entry."""
    normalized = str(direction or "").upper().strip()
    if normalized == "BUY":
        return "SELL"
    if normalized == "SELL":
        return "BUY"
    return "UNKNOWN"


def one_way_entry_allowed(
    positions: Iterable[Mapping],
    candidate_direction: str,
    *,
    allow_hedged_positions: bool = False,
) -> tuple[bool, str]:
    """Prevent opposite-direction entries unless hedging is explicitly enabled.

    The strategy-slot guard intentionally permits different strategy slots to
    coexist. That is useful for scaling, but it is unsafe for this robot's
    directional scalping model because it can leave BUY and SELL open on the
    same symbol at the same time. Unknown position directions fail closed.
    """
    if allow_hedged_positions:
        return True, ""

    candidate = str(candidate_direction or "").upper().strip()
    if candidate not in {"BUY", "SELL"}:
        return False, f"Invalid candidate direction: {candidate or 'UNKNOWN'}"

    opposite = opposite_direction(candidate)
    unknown = 0
    for position in positions:
        direction = _normalized_direction(position)
        if direction == opposite:
            ticket = position.get("id", position.get("ticket", "?"))
            return (
                False,
                f"Opposite-direction position already open: {opposite} "
                f"(ticket={ticket}); blocked candidate={candidate}",
            )
        if direction == "UNKNOWN":
            unknown += 1

    if unknown:
        return (
            False,
            f"Cannot verify direction for {unknown} open position(s); "
            f"blocked candidate={candidate}",
        )
    return True, ""


def strategy_slots_for_decision(decision) -> tuple[str, ...]:
    """Return the single live strategy slot claimed by a permitted decision."""
    entry_filter = getattr(decision, "entry_filter", None)
    # Trend is the only live entry authority. Price Action, SMC, and Wyckoff
    # remain diagnostic-only and must not be required to attach the active
    # strategy slot.
    if entry_filter is None or not bool(getattr(entry_filter, "trend", False)):
        return ()
    return (ACTIVE_STRATEGY_SLOT,)


def strategy_order_comment(base_comment: str, slots: Iterable[str]) -> str:
    """Encode the active strategy ownership in the broker-visible comment."""
    codes = [
        _SLOT_CODES[slot]
        for slot in slots
        if slot == ACTIVE_STRATEGY_SLOT
    ]
    if not codes:
        return base_comment[:32]
    return f"{base_comment}|S={','.join(codes)}"[:32]


def strategy_slots_from_position(position: Mapping) -> frozenset[str]:
    """Read strategy ownership from a normalized or raw position mapping."""
    comment = str(position.get("comment") or "")
    if not comment and isinstance(position.get("_raw"), Mapping):
        comment = str(position["_raw"].get("comment") or "")
    match = _SLOT_PATTERN.search(comment)
    if not match:
        return frozenset(STRATEGY_SLOTS)
    slots = frozenset(
        _CODE_TO_SLOT[code]
        for code in match.group(1).split(",")
        if code in _CODE_TO_SLOT
    )
    if not slots:
        return frozenset(STRATEGY_SLOTS)
    # Any position created by a previous multi-strategy phase occupies the
    # single active slot during the transition.  This preserves fail-closed
    # behavior without allowing an older SMC/Trend/PA/Wyckoff position to
    # coexist with the Phase 3 strategy.
    return frozenset({ACTIVE_STRATEGY_SLOT})


def occupied_strategy_slots(positions: Iterable[Mapping]) -> frozenset[str]:
    """Return every strategy slot claimed by currently open positions."""
    occupied: set[str] = set()
    for position in positions:
        occupied.update(strategy_slots_from_position(position))
    return frozenset(occupied)


def available_for_strategy_slots(
    positions: Iterable[Mapping],
    candidate_slots: Iterable[str],
    *,
    max_open_positions: int,
) -> tuple[bool, str]:
    """Check total capacity and prevent duplicate ownership per strategy."""
    position_list = list(positions)
    if len(position_list) >= max_open_positions:
        return False, f"Maximum open positions reached ({max_open_positions})"

    candidates = tuple(dict.fromkeys(
        slot for slot in candidate_slots if slot == ACTIVE_STRATEGY_SLOT
    ))
    if not candidates:
        return False, "No active strategy slot is attached to the permitted decision"

    occupied = occupied_strategy_slots(position_list)
    duplicate_slots = tuple(slot for slot in candidates if slot in occupied)
    if duplicate_slots:
        labels = ", ".join(_SLOT_CODES[slot] for slot in duplicate_slots)
        return False, f"Strategy slot already has an open position: {labels}"

    return True, ""
"""One-open-position capacity per signal strategy.

Positions opened before strategy-slot tagging was introduced are treated as
occupying every slot.  That fail-closed behavior prevents a legacy position
from being duplicated while the robot cannot identify its owner.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping


STRATEGY_SLOTS = ("smc", "trend", "price_action", "wyckoff")
_SLOT_CODES = {
    "smc": "SMC",
    "trend": "TRD",
    "price_action": "PA",
    "wyckoff": "WYC",
}
_CODE_TO_SLOT = {code: slot for slot, code in _SLOT_CODES.items()}
_SLOT_PATTERN = re.compile(r"(?:^|\|)S=([A-Z,]+)(?:\||$)")


def strategy_slots_for_decision(decision) -> tuple[str, ...]:
    """Return the aligned strategy slots claimed by a permitted decision."""
    entry_filter = getattr(decision, "entry_filter", None)
    if entry_filter is None:
        return ()
    return tuple(
        slot for slot in STRATEGY_SLOTS
        if bool(getattr(entry_filter, slot, False))
    )


def strategy_order_comment(base_comment: str, slots: Iterable[str]) -> str:
    """Encode strategy ownership in the broker-visible order comment."""
    codes = [
        _SLOT_CODES[slot]
        for slot in slots
        if slot in _SLOT_CODES
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
    return slots or frozenset(STRATEGY_SLOTS)


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
        slot for slot in candidate_slots if slot in STRATEGY_SLOTS
    ))
    if not candidates:
        return False, "No strategy slot is attached to the permitted decision"

    occupied = occupied_strategy_slots(position_list)
    duplicate_slots = tuple(slot for slot in candidates if slot in occupied)
    if duplicate_slots:
        labels = ", ".join(_SLOT_CODES[slot] for slot in duplicate_slots)
        return False, f"Strategy slot already has an open position: {labels}"

    return True, ""
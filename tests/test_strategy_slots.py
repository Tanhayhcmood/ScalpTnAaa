"""Regression tests for one-open-position capacity per strategy."""

from types import SimpleNamespace

from live_trading.trading.strategy_slots import (
    ACTIVE_STRATEGY_SLOT,
    STRATEGY_SLOTS,
    available_for_strategy_slots,
    occupied_strategy_slots,
    strategy_order_comment,
    strategy_slots_for_decision,
    strategy_slots_from_position,
)


def _decision(**votes):
    entry_filter = SimpleNamespace(
        smc=votes.get("smc", False),
        trend=votes.get("trend", False),
        price_action=votes.get("price_action", False),
        wyckoff=votes.get("wyckoff", False),
    )
    return SimpleNamespace(entry_filter=entry_filter)


def test_trend_only_decision_claims_only_the_active_strategy_slot():
    assert strategy_slots_for_decision(_decision(
        trend=True,
    )) == (ACTIVE_STRATEGY_SLOT,)


def test_order_comment_round_trip_preserves_strategy_slots():
    comment = strategy_order_comment("GSPv4", (ACTIVE_STRATEGY_SLOT,))

    assert comment == "GSPv4|S=XVTB"
    assert strategy_slots_from_position({"comment": comment}) == {
        ACTIVE_STRATEGY_SLOT
    }


def test_non_active_strategy_votes_cannot_claim_a_live_slot():
    assert strategy_slots_for_decision(_decision(smc=True)) == ()
    allowed, reason = available_for_strategy_slots(
        [{"comment": "GSPv4"}],
        ("smc",),
        max_open_positions=4,
    )

    assert allowed is False
    assert "No active strategy slot" in reason


def test_trend_only_decision_passes_with_capacity_available():
    candidate_slots = strategy_slots_for_decision(_decision(trend=True))

    allowed, reason = available_for_strategy_slots(
        [],
        candidate_slots,
        max_open_positions=4,
    )

    assert candidate_slots == (ACTIVE_STRATEGY_SLOT,)
    assert allowed is True
    assert reason == ""


def test_max_open_positions_still_blocks_a_trend_only_decision_at_four():
    candidate_slots = strategy_slots_for_decision(_decision(trend=True))
    positions = [
        {"comment": f"GSPv4|S=XVTB|position={index}"}
        for index in range(4)
    ]

    allowed, reason = available_for_strategy_slots(
        positions,
        candidate_slots,
        max_open_positions=4,
    )

    assert allowed is False
    assert reason == "Maximum open positions reached (4)"


def test_same_strategy_cannot_open_a_second_position():
    positions = [{"comment": "GSPv4|S=XVTB"}]

    allowed, reason = available_for_strategy_slots(
        positions, (ACTIVE_STRATEGY_SLOT,), max_open_positions=4
    )

    assert allowed is False
    assert "XVTB" in reason


def test_legacy_strategy_position_occupies_the_active_slot():
    positions = [{"comment": "GSPv4|S=SMC"}]

    allowed, reason = available_for_strategy_slots(
        positions, (ACTIVE_STRATEGY_SLOT,), max_open_positions=4
    )

    assert allowed is False
    assert "XVTB" in reason


def test_legacy_untagged_position_occupies_all_slots_fail_closed():
    positions = [{"comment": "GSPv4"}]

    assert occupied_strategy_slots(positions) == set(STRATEGY_SLOTS)
    allowed, reason = available_for_strategy_slots(
        positions, (ACTIVE_STRATEGY_SLOT,), max_open_positions=4
    )

    assert allowed is False
    assert "XVTB" in reason
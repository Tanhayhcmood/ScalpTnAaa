"""Regression tests for one-open-position capacity per strategy."""

from types import SimpleNamespace

from live_trading.trading.strategy_slots import (
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


def test_permitted_decision_claims_each_aligned_strategy_slot():
    assert strategy_slots_for_decision(_decision(
        smc=True, price_action=True
    )) == ("smc", "price_action")


def test_order_comment_round_trip_preserves_strategy_slots():
    comment = strategy_order_comment("GSPv4", ("smc", "price_action"))

    assert comment == "GSPv4|S=SMC,PA"
    assert strategy_slots_from_position({"comment": comment}) == {
        "smc", "price_action"
    }


def test_same_strategy_cannot_open_a_second_position():
    positions = [{"comment": "GSPv4|S=SMC"}]

    allowed, reason = available_for_strategy_slots(
        positions, ("smc",), max_open_positions=4
    )

    assert allowed is False
    assert "SMC" in reason


def test_unused_strategy_slot_can_open_while_another_is_occupied():
    positions = [{"comment": "GSPv4|S=SMC"}]

    allowed, reason = available_for_strategy_slots(
        positions, ("price_action",), max_open_positions=4
    )

    assert allowed is True
    assert reason == ""


def test_legacy_untagged_position_occupies_all_slots_fail_closed():
    positions = [{"comment": "GSPv4"}]

    assert occupied_strategy_slots(positions) == set(STRATEGY_SLOTS)
    allowed, reason = available_for_strategy_slots(
        positions, ("wyckoff",), max_open_positions=4
    )

    assert allowed is False
    assert "WYC" in reason
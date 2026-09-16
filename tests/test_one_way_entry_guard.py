"""Regression coverage for the directional entry safety boundary."""

from live_trading.trading.strategy_slots import one_way_entry_allowed


def test_opposite_direction_is_blocked_even_when_another_slot_is_free():
    allowed, reason = one_way_entry_allowed(
        [{"id": 101, "type": "SELL", "comment": "GSPv4|S=SMC"}],
        "BUY",
    )

    assert allowed is False
    assert "Opposite-direction" in reason
    assert "BUY" in reason


def test_same_direction_can_still_scale_when_hedging_is_disabled():
    allowed, reason = one_way_entry_allowed(
        [{"id": 101, "type": "SELL"}],
        "SELL",
    )

    assert allowed is True
    assert reason == ""


def test_unknown_direction_fails_closed():
    allowed, reason = one_way_entry_allowed(
        [{"id": 101, "type": "UNKNOWN"}],
        "BUY",
    )

    assert allowed is False
    assert "Cannot verify direction" in reason


def test_explicit_hedging_override_preserves_previous_behavior():
    allowed, reason = one_way_entry_allowed(
        [{"id": 101, "type": "SELL"}],
        "BUY",
        allow_hedged_positions=True,
    )

    assert allowed is True
    assert reason == ""
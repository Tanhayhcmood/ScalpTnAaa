"""RANGE confirmation policy regression tests."""

from live_trading.signals.decision_engine import (
    _effective_min_confirmations,
    _range_confirmation_gate,
)
from live_trading.signals.entry_filter import EntryFilterResult


def test_range_uses_two_confirmation_floor_when_strength_is_not_weak():
    assert _effective_min_confirmations(
        2, "RANGE", False, range_min_confirmations=2, strength="MODERATE"
    ) == 2


def test_weak_range_uses_the_stricter_confirmation_floor():
    assert _effective_min_confirmations(
        2,
        "RANGE",
        False,
        range_min_confirmations=2,
        range_weak_min_confirmations=3,
        strength="WEAK",
    ) == 3


def test_weak_range_floor_is_tunable():
    assert _effective_min_confirmations(
        2,
        "RANGE",
        False,
        range_min_confirmations=2,
        range_weak_min_confirmations=4,
        strength="WEAK",
    ) == 4


def test_range_does_not_add_a_counter_trend_vote_requirement():
    assert _effective_min_confirmations(
        2, "RANGE", True, range_min_confirmations=2, strength="MODERATE"
    ) == 2


def test_range_accepts_one_smc_confirmation():
    result = EntryFilterResult(
        allowed=True, direction="BUY", confirmation_count=1,
        smc=True, trend=False, price_action=False, wyckoff=False,
    )
    assert _range_confirmation_gate(result, 1) == (True, "")


def test_range_accepts_one_price_action_confirmation():
    result = EntryFilterResult(
        allowed=True, direction="BUY", confirmation_count=1,
        smc=False, trend=False, price_action=True, wyckoff=False,
    )
    assert _range_confirmation_gate(result, 1) == (True, "")


def test_range_rejects_zero_confirmations():
    result = EntryFilterResult(
        allowed=False, direction="NEUTRAL", confirmation_count=0,
        smc=False, trend=False, price_action=False, wyckoff=False,
    )
    allowed, reason = _range_confirmation_gate(result, 1)
    assert not allowed
    assert "0/1 confirmations" in reason


def test_range_rejects_one_confirmation_when_two_are_required():
    result = EntryFilterResult(
        allowed=False, direction="NEUTRAL", confirmation_count=1,
        smc=True, trend=False, price_action=False, wyckoff=False,
    )
    allowed, reason = _range_confirmation_gate(result, 2)
    assert not allowed
    assert "1/2 confirmations" in reason


def test_standalone_price_action_can_satisfy_two_vote_range_floor():
    result = EntryFilterResult(
        allowed=True, direction="BUY", confirmation_count=1,
        smc=False, trend=False, price_action=True, wyckoff=False,
    )
    allowed, reason = _range_confirmation_gate(
        result,
        2,
        price_action_standalone=True,
    )
    assert allowed is True
    assert reason == ""


def test_weak_range_does_not_allow_single_vote_standalone_override():
    result = EntryFilterResult(
        allowed=True, direction="BUY", confirmation_count=1,
        smc=False, trend=False, price_action=True, wyckoff=False,
    )
    allowed, reason = _range_confirmation_gate(result, 3)
    assert allowed is False
    assert "1/3 confirmations" in reason


def test_non_range_regime_keeps_global_confirmation_setting():
    assert _effective_min_confirmations(
        2, "WEAK_TREND_BULL", False, strength="WEAK"
    ) == 2

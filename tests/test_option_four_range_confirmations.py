"""RANGE confirmation policy regression tests."""

from live_trading.signals.decision_engine import (
    _effective_min_confirmations,
    _range_confirmation_gate,
)
from live_trading.signals.entry_filter import EntryFilterResult


def test_range_uses_one_confirmation_floor_when_strength_is_not_weak():
    assert _effective_min_confirmations(
        2, "RANGE", False, range_min_confirmations=2, strength="MODERATE"
    ) == 1


def test_weak_range_uses_the_single_engine_confirmation_floor():
    assert _effective_min_confirmations(
        2,
        "RANGE",
        False,
        range_min_confirmations=2,
        range_weak_min_confirmations=3,
        strength="WEAK",
    ) == 1


def test_weak_range_floor_is_capped_at_one_engine():
    assert _effective_min_confirmations(
        2,
        "RANGE",
        False,
        range_min_confirmations=2,
        range_weak_min_confirmations=4,
        strength="WEAK",
    ) == 1


def test_range_does_not_add_a_counter_trend_vote_requirement():
    assert _effective_min_confirmations(
        2, "RANGE", True, range_min_confirmations=2, strength="MODERATE"
    ) == 1


def test_range_rejects_one_smc_confirmation():
    result = EntryFilterResult(
        allowed=True, direction="BUY", confirmation_count=1,
        smc=True, trend=False, price_action=False, wyckoff=False,
    )
    allowed, reason = _range_confirmation_gate(result, 1)
    assert allowed is False
    assert "Trend confirmation" in reason


def test_range_rejects_one_price_action_confirmation():
    result = EntryFilterResult(
        allowed=True, direction="BUY", confirmation_count=1,
        smc=False, trend=False, price_action=True, wyckoff=False,
    )
    allowed, reason = _range_confirmation_gate(result, 1)
    assert allowed is False
    assert "Trend confirmation" in reason


def test_range_rejects_zero_confirmations():
    result = EntryFilterResult(
        allowed=False, direction="NEUTRAL", confirmation_count=0,
        smc=False, trend=False, price_action=False, wyckoff=False,
    )
    allowed, reason = _range_confirmation_gate(result, 1)
    assert not allowed
    assert "Trend confirmation" in reason


def test_range_accepts_trend_confirmation_when_a_stale_two_vote_floor_is_supplied():
    result = EntryFilterResult(
        allowed=False, direction="NEUTRAL", confirmation_count=1,
        smc=False, trend=True, price_action=False, wyckoff=False,
    )
    allowed, reason = _range_confirmation_gate(result, 2)
    assert allowed
    assert reason == ""


def test_standalone_price_action_does_not_satisfy_range_confirmation_floor():
    result = EntryFilterResult(
        allowed=True, direction="BUY", confirmation_count=1,
        smc=False, trend=False, price_action=True, wyckoff=False,
    )
    allowed, reason = _range_confirmation_gate(
        result,
        2,
        price_action_standalone=True,
    )
    assert allowed is False
    assert "Trend confirmation" in reason


def test_weak_range_accepts_single_trend_vote():
    result = EntryFilterResult(
        allowed=True, direction="BUY", confirmation_count=1,
        smc=False, trend=True, price_action=False, wyckoff=False,
    )
    allowed, reason = _range_confirmation_gate(result, 3)
    assert allowed is True
    assert reason == ""


def test_non_range_regime_keeps_global_confirmation_setting():
    assert _effective_min_confirmations(
        2, "WEAK_TREND_BULL", False, strength="WEAK"
    ) == 1

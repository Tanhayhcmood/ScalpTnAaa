"""Exact option 1 entry-gate tests.

Option 1 requires same-direction SMC + Price Action + Wyckoff.
EMA is intentionally not a required vote for this option.
"""

from live_trading.signals.decision_engine import _allow_without_smc_for_quality
from live_trading.signals.entry_filter import apply_entry_filter


def test_option_one_allows_without_ema_when_three_required_engines_agree():
    result = apply_entry_filter(
        smc_signal="BUY",
        ema_trend="NEUTRAL",
        pa_signal="BUY",
        wyckoff_signal="BUY",
        min_confirmations=2,
        require_smc_price_action_wyckoff=True,
    )

    assert result.allowed is True
    assert result.direction == "BUY"
    assert result.confirmation_count == 3


def test_option_one_blocks_when_wyckoff_disagrees():
    result = apply_entry_filter(
        smc_signal="BUY",
        ema_trend="BULLISH",
        pa_signal="BUY",
        wyckoff_signal="SELL",
        min_confirmations=2,
        require_smc_price_action_wyckoff=True,
    )

    assert result.allowed is False
    assert result.direction == "NEUTRAL"


def test_option_one_blocks_when_price_action_is_missing():
    result = apply_entry_filter(
        smc_signal="SELL",
        ema_trend="BEARISH",
        pa_signal="NEUTRAL",
        wyckoff_signal="SELL",
        min_confirmations=2,
        require_smc_price_action_wyckoff=True,
    )

    assert result.allowed is False
    assert result.direction == "NEUTRAL"


def test_price_action_can_open_without_other_engine_votes():
    result = apply_entry_filter(
        smc_signal="NEUTRAL",
        ema_trend="NEUTRAL",
        pa_signal="BUY",
        wyckoff_signal="NEUTRAL",
        min_confirmations=2,
        price_action_standalone=True,
    )

    assert result.allowed is True
    assert result.direction == "BUY"
    assert result.confirmation_count == 1
    assert result.price_action is True
    assert result.smc is False
    assert result.trend is False
    assert result.wyckoff is False


def test_non_price_action_engine_still_needs_ordinary_confirmations():
    result = apply_entry_filter(
        smc_signal="BUY",
        ema_trend="NEUTRAL",
        pa_signal="NEUTRAL",
        wyckoff_signal="NEUTRAL",
        min_confirmations=2,
        price_action_standalone=True,
    )

    assert result.allowed is False
    assert result.direction == "NEUTRAL"
    assert result.confirmation_count == 1

def test_price_action_standalone_reaches_quality_gate_without_two_confirmations():
    result = apply_entry_filter(
        smc_signal="NEUTRAL",
        ema_trend="NEUTRAL",
        pa_signal="BUY",
        wyckoff_signal="NEUTRAL",
        min_confirmations=2,
        price_action_standalone=True,
    )

    assert _allow_without_smc_for_quality(
        result, effective_min_confirmations=2, price_action_standalone=True
    ) is True


def test_non_price_action_still_needs_the_configured_quality_confirmations():
    result = apply_entry_filter(
        smc_signal="NEUTRAL",
        ema_trend="NEUTRAL",
        pa_signal="NEUTRAL",
        wyckoff_signal="BUY",
        min_confirmations=2,
        price_action_standalone=True,
    )

    assert _allow_without_smc_for_quality(
        result, effective_min_confirmations=2, price_action_standalone=True
    ) is False

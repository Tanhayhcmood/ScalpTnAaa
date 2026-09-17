"""Regression tests for the fixed Price Action + Trend entry policy."""

from live_trading.signals.decision_engine import _allow_without_smc_for_quality
from live_trading.signals.entry_filter import apply_entry_filter


def test_price_action_standalone_ignores_display_only_engines():
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
    assert result.confirmation_count == 1
    assert result.entry_reason == "PA_STANDALONE"


def test_wyckoff_disagreement_does_not_block_aligned_pa_and_trend():
    result = apply_entry_filter(
        smc_signal="BUY",
        ema_trend="BULLISH",
        pa_signal="BUY",
        wyckoff_signal="SELL",
        min_confirmations=2,
        require_smc_price_action_wyckoff=True,
    )

    assert result.allowed is True
    assert result.direction == "BUY"
    assert result.entry_reason == "PA+TREND_ALIGNED"


def test_trend_only_is_blocked_even_when_display_only_engines_agree():
    result = apply_entry_filter(
        smc_signal="SELL",
        ema_trend="BEARISH",
        pa_signal="NEUTRAL",
        wyckoff_signal="SELL",
        min_confirmations=2,
        require_smc_price_action_wyckoff=True,
    )

    assert result.allowed is False
    assert result.direction == "SELL"
    assert result.confirmation_count == 1
    assert result.entry_reason == "BLOCKED_TREND_ONLY"


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


def test_display_only_engine_cannot_authorize_an_entry():
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
    assert result.confirmation_count == 0
    assert result.entry_reason == "BLOCKED_NO_SIGNAL"

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

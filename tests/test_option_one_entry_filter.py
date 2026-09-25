"""Regression tests for retired multi-engine live-entry options.

SMC, Price Action, and Wyckoff remain diagnostic inputs, but live entries are
authorized only by Trend.
"""

from live_trading.signals.decision_engine import _allow_without_smc_for_quality
from live_trading.signals.entry_filter import apply_entry_filter


def test_legacy_smc_price_action_wyckoff_option_cannot_authorize_live_entry():
    result = apply_entry_filter(
        smc_signal="BUY",
        ema_trend="NEUTRAL",
        pa_signal="BUY",
        wyckoff_signal="BUY",
        min_confirmations=2,
        require_smc_price_action_wyckoff=True,
    )

    assert result.allowed is False
    assert result.direction == "NEUTRAL"
    assert result.confirmation_count == 0
    assert result.smc is False
    assert result.wyckoff is False


def test_legacy_option_cannot_override_trend_only_authorization():
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
    assert result.confirmation_count == 2


def test_strong_trend_can_authorize_without_price_action():
    result = apply_entry_filter(
        smc_signal="SELL",
        ema_trend="BEARISH",
        pa_signal="NEUTRAL",
        wyckoff_signal="SELL",
        min_confirmations=1,
        require_smc_price_action_wyckoff=True,
        trend_score=-60.0,
        trend_standalone=True,
        trend_standalone_min_score=55.0,
    )

    assert result.allowed is True
    assert result.direction == "SELL"
    assert result.confirmation_count == 1


def test_price_action_cannot_open_without_trend():
    result = apply_entry_filter(
        smc_signal="NEUTRAL",
        ema_trend="NEUTRAL",
        pa_signal="BUY",
        wyckoff_signal="NEUTRAL",
        min_confirmations=2,
        price_action_standalone=True,
    )

    assert result.allowed is False
    assert result.direction == "NEUTRAL"
    assert result.confirmation_count == 0
    assert result.price_action is False
    assert result.smc is False
    assert result.trend is False
    assert result.wyckoff is False


def test_smc_only_signal_is_not_a_live_entry_confirmation():
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

def test_trend_only_authorization_reaches_quality_gate_without_smc():
    result = apply_entry_filter(
        smc_signal="NEUTRAL",
        ema_trend="BULLISH",
        pa_signal="NEUTRAL",
        wyckoff_signal="NEUTRAL",
        min_confirmations=1,
        price_action_standalone=True,
    )

    assert _allow_without_smc_for_quality(
        result, effective_min_confirmations=1, price_action_standalone=True
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
        result, effective_min_confirmations=1, price_action_standalone=True
    ) is False

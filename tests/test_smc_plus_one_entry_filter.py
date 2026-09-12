"""Production entry-gate contract.

The live trading rule is SMC plus any one same-direction strategy.
"""

from live_trading.signals.entry_filter import apply_entry_filter


def test_smc_plus_trend_allows_entry():
    result = apply_entry_filter(
        smc_signal="BUY",
        ema_trend="BULLISH",
        pa_signal="NEUTRAL",
        wyckoff_signal="NEUTRAL",
        min_confirmations=2,
    )

    assert result.allowed is True
    assert result.direction == "BUY"
    assert result.confirmation_count == 2


def test_smc_plus_price_action_allows_entry():
    result = apply_entry_filter(
        smc_signal="SELL",
        ema_trend="NEUTRAL",
        pa_signal="SELL",
        wyckoff_signal="NEUTRAL",
        min_confirmations=2,
    )

    assert result.allowed is True
    assert result.direction == "SELL"
    assert result.confirmation_count == 2


def test_smc_plus_wyckoff_allows_entry():
    result = apply_entry_filter(
        smc_signal="BUY",
        ema_trend="NEUTRAL",
        pa_signal="NEUTRAL",
        wyckoff_signal="BUY",
        min_confirmations=2,
    )

    assert result.allowed is True
    assert result.direction == "BUY"
    assert result.confirmation_count == 2


def test_smc_alone_is_not_enough():
    result = apply_entry_filter(
        smc_signal="BUY",
        ema_trend="NEUTRAL",
        pa_signal="NEUTRAL",
        wyckoff_signal="NEUTRAL",
        min_confirmations=2,
    )

    assert result.allowed is False
    assert result.direction == "NEUTRAL"
    assert result.confirmation_count == 1
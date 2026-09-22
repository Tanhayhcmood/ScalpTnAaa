"""Regression tests for trend-entry confirmation and structural stop sizing."""

from live_trading.risk.capital_manager import CapitalInput, calc_trade_parameters
from live_trading.signals.entry_filter import apply_entry_filter


def test_trend_only_entry_passes_the_single_engine_confirmation_floor():
    result = apply_entry_filter(
        smc_signal="NEUTRAL",
        ema_trend="BEARISH",
        pa_signal="NEUTRAL",
        wyckoff_signal="NEUTRAL",
        min_confirmations=2,
    )

    assert result.allowed is True
    assert result.confirmation_count == 1


def test_sell_stop_does_not_use_a_tight_structural_level_below_atr_floor():
    result = calc_trade_parameters(
        CapitalInput(
            direction="SELL",
            entry_price=100.0,
            atr=2.0,
            account_balance=10_000.0,
            order_block_top=101.0,
            swing_high=103.0,
            resistance_level=102.0,
        )
    )

    # The structural distance is 3.5, but the protective floor is 3x ATR.
    assert result.stop_loss == 106.0
    assert result.sl_distance_usd == 6.0


def test_buy_stop_does_not_use_a_tight_structural_level_below_atr_floor():
    result = calc_trade_parameters(
        CapitalInput(
            direction="BUY",
            entry_price=100.0,
            atr=2.0,
            account_balance=10_000.0,
            order_block_bottom=99.0,
            swing_low=97.0,
            support_level=98.0,
        )
    )

    # The structural distance is 3.5, but the protective floor is 3x ATR.
    assert result.stop_loss == 94.0
    assert result.sl_distance_usd == 6.0
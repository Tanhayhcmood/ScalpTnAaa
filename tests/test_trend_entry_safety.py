"""Regression tests for trend-entry confirmation and structural stop sizing."""

from live_trading.risk.capital_manager import CapitalInput, calc_trade_parameters
from live_trading.signals.entry_filter import apply_entry_filter


def test_trend_only_entry_is_blocked_by_the_trend_confirmation_floor():
    result = apply_entry_filter(
        smc_signal="NEUTRAL",
        ema_trend="BEARISH",
        pa_signal="NEUTRAL",
        wyckoff_signal="NEUTRAL",
        min_confirmations=2,
    )

    assert result.allowed is False
    assert result.confirmation_count == 1


def test_sell_stop_uses_outer_structural_level_not_nearest_resistance():
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

    # Outer high 103.0 + 0.25 ATR buffer 0.5.
    assert result.stop_loss == 103.5
    assert result.sl_distance_usd == 3.5


def test_buy_stop_uses_outer_structural_level_not_nearest_support():
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

    # The BUY side already uses the outer low (min of the candidates).
    assert result.stop_loss == 96.5
    assert result.sl_distance_usd == 3.5
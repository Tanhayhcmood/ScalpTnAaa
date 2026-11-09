"""Regression tests for percentage-risk, broker-stepped lot sizing."""

from datetime import datetime, timedelta, timezone

from live_trading.risk.capital_manager import CapitalInput, calc_trade_parameters
from live_trading.risk.scalp_exit import (
    breakeven_stop_price,
    closed_m5_bars_since_entry,
    scalp_time_stop_due,
    split_close_volume,
)


def _params(balance: float, atr: float = 31.633333):
    return calc_trade_parameters(
        CapitalInput(
            direction="SELL",
            entry_price=4268.55,
            atr=atr,
            account_balance=balance,
            risk_percent=1.0,
        )
    )


def test_minimum_lot_cannot_silently_exceed_percentage_budget():
    params = _params(426.08)

    assert params.lot_size == 0.01
    assert params.risk_budget == 4.26
    assert params.risk_amount > 40.0
    assert params.min_lot_risk_exceeded is True


def test_lot_size_scales_down_as_protective_stop_gets_wider():
    narrow_stop = _params(5000.0, atr=2.0)
    wide_stop = _params(5000.0, atr=5.0)

    assert narrow_stop.lot_size == 0.08
    assert wide_stop.lot_size == 0.03
    assert narrow_stop.risk_amount <= narrow_stop.risk_budget + 0.01
    assert wide_stop.risk_amount <= wide_stop.risk_budget + 0.01
    assert narrow_stop.lot_size > wide_stop.lot_size


def test_sufficient_balance_keeps_requested_risk_capacity():
    params = _params(5000.0, atr=5.0)

    assert params.min_lot_risk_exceeded is False
    assert params.risk_amount <= params.risk_budget + 0.01


def test_scalp_structural_stop_clamps_to_configured_atr_envelope():
    too_tight = calc_trade_parameters(
        CapitalInput(
            direction="BUY",
            entry_price=100.0,
            atr=10.0,
            account_balance=10_000.0,
            support_level=93.0,
            sl_atr_multiplier=1.8,
            sl_min_atr_multiplier=1.0,
            structure_buffer_atr=0.2,
        )
    )
    too_wide = calc_trade_parameters(
        CapitalInput(
            direction="BUY",
            entry_price=100.0,
            atr=10.0,
            account_balance=10_000.0,
            support_level=80.0,
            sl_atr_multiplier=1.8,
            sl_min_atr_multiplier=1.0,
            structure_buffer_atr=0.2,
        )
    )

    assert too_tight.stop_loss_mode == "STRUCTURAL_MIN_FLOOR"
    assert too_tight.stop_loss == 90.0
    assert too_tight.sl_distance_usd / 10.0 == 1.0
    assert too_wide.stop_loss_mode == "STRUCTURE_TOO_FAR"
    assert too_wide.stop_loss_valid is False
    assert too_wide.stop_loss == 82.0
    assert too_wide.sl_distance_usd / 10.0 == 1.8


def test_partial_close_volume_rounds_down_to_broker_step():
    partial = split_close_volume(0.15, 0.01, 0.01)

    assert partial == 0.07
    assert round(0.15 - partial, 2) == 0.08
    assert split_close_volume(0.03, 0.01, 0.02) is None


def test_scalp_breakeven_stop_includes_spread_buffer():
    assert breakeven_stop_price(100.0, "BUY", 0.25, 2) == 100.25
    assert breakeven_stop_price(100.0, "SELL", 0.25, 2) == 99.75


def test_scalp_time_stop_counts_closed_m5_bars_and_respects_half_r():
    opened_at = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    candles = [
        {"time": (opened_at + timedelta(minutes=5 * index)).isoformat()}
        for index in range(1, 7)
    ]
    bars = closed_m5_bars_since_entry(candles, opened_at)

    assert bars == 6
    assert scalp_time_stop_due(bars, reached_half_r=False, time_stop_bars=6)
    assert not scalp_time_stop_due(5, reached_half_r=False, time_stop_bars=6)
    assert not scalp_time_stop_due(bars, reached_half_r=True, time_stop_bars=6)
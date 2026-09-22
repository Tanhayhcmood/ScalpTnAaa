"""Regression tests for percentage-risk, broker-stepped lot sizing."""

from live_trading.risk.capital_manager import CapitalInput, calc_trade_parameters


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
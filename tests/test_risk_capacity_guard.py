"""Regression tests for minimum-lot risk sizing."""

from live_trading.risk.capital_manager import CapitalInput, calc_trade_parameters


def _params(balance: float):
    return calc_trade_parameters(
        CapitalInput(
            direction="SELL",
            entry_price=4268.55,
            atr=31.633333,
            account_balance=balance,
            risk_percent=1.0,
        )
    )


def test_minimum_lot_cannot_silently_exceed_one_percent_budget():
    params = _params(426.08)

    assert params.lot_size == 0.01
    assert params.risk_budget == 4.26
    assert params.risk_amount > 40.0
    assert params.min_lot_risk_exceeded is True


def test_sufficient_balance_keeps_requested_risk_capacity():
    params = _params(5000.0)

    assert params.min_lot_risk_exceeded is False
    assert params.risk_amount <= params.risk_budget + 0.01
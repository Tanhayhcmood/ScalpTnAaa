"""Regression tests for minimum-lot risk sizing."""

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


def test_minimum_lot_cannot_silently_exceed_one_percent_budget():
    params = _params(426.08)

    assert params.lot_size == 0.01
    assert params.risk_budget == 4.26
    assert params.risk_amount > 40.0
    assert params.min_lot_risk_exceeded is True


def test_sufficient_balance_keeps_requested_risk_capacity():
    # A sufficiently funded account still cannot bypass the fixed-lot
    # $20 ceiling; use a normal-volatility ATR so this case exercises the
    # successful sizing path under the current production policy.
    params = _params(5000.0, atr=5.0)

    assert params.min_lot_risk_exceeded is False
    assert params.risk_amount <= params.risk_budget + 0.01
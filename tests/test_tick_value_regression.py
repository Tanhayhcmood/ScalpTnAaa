import pytest

from live_trading.risk.aggregate_risk import (
    build_symbol_risk_spec,
    size_order_for_aggregate_risk,
)


def test_xauusd_mismatched_tick_value_sizing_matches_contract_risk_within_two_percent():
    """Guard the broker tick-value mismatch that previously understated risk 10x."""
    symbol_params = {
        "symbol": "XAUUSD",
        "_mtapi_volume_units": "lots",
        "_mtapi_volume_scale": 1.0,
        "symbolInfo": {
            "tickValue": 0.1,
            "tickSize": 0.01,
            "contractSize": 100.0,
            "currencyProfit": "USD",
            "tickValueCurrency": "USD",
            "volumeMin": 0.01,
            "volumeStep": 0.01,
            "volumeMax": 50.0,
        },
    }
    spec = build_symbol_risk_spec(symbol_params, "USD")

    sizing = size_order_for_aggregate_risk(
        positions=[],
        direction="BUY",
        entry_price=2300.0,
        stop_loss=2290.0,
        account_balance=10_000.0,
        per_trade_risk_percent=1.0,
        aggregate_risk_percent=2.0,
        spec=spec,
    )

    independently_calculated_risk = sizing.lot_size * 100.0 * abs(2300.0 - 2290.0)
    assert sizing.lot_size == pytest.approx(0.10)
    assert sizing.new_risk == pytest.approx(independently_calculated_risk, rel=0.02)
    assert spec.tick_value_source == "contract_size_after_mismatch"
    assert spec.value_per_point_per_lot == pytest.approx(100.0)

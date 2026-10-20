from unittest.mock import patch

import pytest

from live_trading.risk.aggregate_risk import (
    AggregateRiskLimitError,
    OpenPositionRiskError,
    aggregate_open_risk,
    build_symbol_risk_spec,
    position_risk_amount,
    risk_per_lot,
    size_order_for_aggregate_risk,
)


def _symbol_params(**symbol_info):
    return {
        "symbolInfo": {
            "tickSize": 0.01,
            "tickValue": 1.0,
            "tickValueCurrency": "USD",
            "contractSize": 100.0,
            "volumeMin": 0.01,
            "volumeStep": 0.01,
            "volumeMax": 50.0,
            **symbol_info,
        }
    }


def _position(
    ticket,
    *,
    entry=2300.0,
    stop=2290.0,
    volume=0.01,
    direction="BUY",
):
    return {
        "id": str(ticket),
        "symbol": "XAUUSD",
        "type": direction,
        "volume": volume,
        "open_price": entry,
        "sl": stop,
    }


def test_one_open_trade_leaves_a_sized_order_within_aggregate_limit():
    spec = build_symbol_risk_spec(_symbol_params(), "USD")
    sizing = size_order_for_aggregate_risk(
        positions=[_position(1)],  # $10 of current risk
        direction="BUY",
        entry_price=2300.0,
        stop_loss=2290.0,
        account_balance=10_000.0,
        per_trade_risk_percent=1.0,
        aggregate_risk_percent=2.0,
        spec=spec,
    )

    assert sizing.existing_risk == pytest.approx(10.0)
    assert sizing.lot_size == pytest.approx(0.10)
    assert sizing.new_risk == pytest.approx(100.0)
    assert sizing.aggregate_risk == pytest.approx(110.0)
    assert sizing.aggregate_risk <= sizing.aggregate_limit
    assert sizing.position_count == 1


def test_multiple_positions_over_aggregate_limit_block_new_order():
    spec = build_symbol_risk_spec(_symbol_params(), "USD")
    positions = [
        _position(1, volume=0.11),  # $110
        _position(2, volume=0.11),  # $110
    ]

    assert aggregate_open_risk(positions, spec) == pytest.approx((220.0, 2))
    with pytest.raises(AggregateRiskLimitError, match="already meets/exceeds"):
        size_order_for_aggregate_risk(
            positions=positions,
            direction="BUY",
            entry_price=2300.0,
            stop_loss=2290.0,
            account_balance=10_000.0,
            per_trade_risk_percent=1.0,
            aggregate_risk_percent=2.0,
            spec=spec,
        )


def test_open_position_without_valid_stop_blocks_risk_sizing():
    spec = build_symbol_risk_spec(_symbol_params(), "USD")
    unprotected = _position(1)
    unprotected["sl"] = 0

    with pytest.raises(OpenPositionRiskError, match="SL must be finite and positive"):
        size_order_for_aggregate_risk(
            positions=[unprotected],
            direction="BUY",
            entry_price=2300.0,
            stop_loss=2290.0,
            account_balance=10_000.0,
            per_trade_risk_percent=1.0,
            aggregate_risk_percent=2.0,
            spec=spec,
        )


def test_tick_size_and_tick_value_produce_account_currency_risk():
    spec = build_symbol_risk_spec(
        _symbol_params(tickSize=0.01, tickValue=0.5), "USD"
    )

    assert risk_per_lot("BUY", 2300.0, 2299.25, spec) == pytest.approx(37.5)
    assert position_risk_amount(
        _position(1, entry=2300.0, stop=2299.25, volume=0.2), spec
    ) == pytest.approx(7.5)


def test_contract_size_fallback_requires_matching_account_currency():
    spec = build_symbol_risk_spec(
        {
            "symbolInfo": {
                "tickSize": 0.01,
                "contractSize": 100.0,
                "currencyProfit": "USD",
            }
        },
        "USD",
    )

    assert spec.tick_value_source == "contract_size_same_currency"
    assert spec.tick_value_per_lot == pytest.approx(1.0)
    assert risk_per_lot("SELL", 2300.0, 2301.0, spec) == pytest.approx(100.0)
    with pytest.raises(ValueError, match="cannot be matched"):
        build_symbol_risk_spec(
            {
                "symbolInfo": {
                    "tickSize": 0.01,
                    "contractSize": 100.0,
                    "currencyProfit": "USD",
                }
            },
            "EUR",
        )


def test_mismatched_tick_value_currency_is_rejected():
    with pytest.raises(ValueError, match="does not match account currency"):
        build_symbol_risk_spec(
            _symbol_params(tickValueCurrency="EUR"),
            "USD",
        )


def test_invalid_broker_volume_step_fails_closed():
    with pytest.raises(ValueError, match="volumeStep must be finite and positive"):
        build_symbol_risk_spec(
            _symbol_params(volumeStep=0),
            "USD",
        )


def test_duplicate_ticket_is_counted_once_using_worst_risk():
    spec = build_symbol_risk_spec(_symbol_params(), "USD")
    positions = [
        _position(7, volume=0.01),
        _position(7, volume=0.02),
    ]

    total, count = aggregate_open_risk(positions, spec)

    assert total == pytest.approx(20.0)
    assert count == 1


def test_risk_sized_volume_floors_to_broker_step():
    spec = build_symbol_risk_spec(_symbol_params(), "USD")
    sizing = size_order_for_aggregate_risk(
        positions=[],
        direction="BUY",
        entry_price=2300.0,
        stop_loss=2299.0,
        account_balance=10_100.0,
        per_trade_risk_percent=1.0,
        aggregate_risk_percent=2.0,
        spec=spec,
    )

    assert sizing.lot_size == pytest.approx(1.01)
    assert sizing.new_risk == pytest.approx(101.0)


def _xauusd_zero_tick_value_params(minimum_lot: float) -> dict:
    return {
        "symbol": "XAUUSD",
        "_tick_size_source": "env:XAUUSD_TICK_SIZE",
        "symbolInfo": {
            "tickSize": 0.01,
            "tickValue": 0,
            "contractSize": 100.0,
            "volumeMin": minimum_lot,
            "volumeStep": minimum_lot,
            "volumeMax": 50.0,
        },
    }


def test_xauusd_zero_tick_value_derives_contract_risk_and_allows_minimum_lot():
    spec = build_symbol_risk_spec(_xauusd_zero_tick_value_params(0.01), "USD")

    with patch("live_trading.risk.aggregate_risk.log.info") as guard_log:
        sizing = size_order_for_aggregate_risk(
            positions=[],
            direction="SELL",
            entry_price=2300.0,
            stop_loss=2300.0 + (3.587 * 3.0),
            account_balance=9171.44,
            per_trade_risk_percent=1.0,
            aggregate_risk_percent=2.0,
            spec=spec,
            account_equity=9171.44,
        )

    assert spec.tick_value_source == "contract_size_xauusd_usd_assumed"
    assert spec.tick_value_per_lot == pytest.approx(1.0)
    assert risk_per_lot("SELL", 2300.0, 2300.0 + (3.587 * 3.0), spec) == pytest.approx(
        1076.1
    )
    assert sizing.lot_size == pytest.approx(0.08)
    assert sizing.new_risk == pytest.approx(86.088)
    assert sizing.risk_budget == pytest.approx(91.7144)
    assert guard_log.call_count == 1
    assert "PRE_ORDER_RISK_GUARD" in guard_log.call_args.args[0]
    assert '"tickValue":0' in guard_log.call_args.args[17]
    assert "minimum_lot_risk=%s %s" in guard_log.call_args.args[0]


def test_xauusd_zero_tick_value_blocks_when_minimum_lot_exceeds_budget():
    spec = build_symbol_risk_spec(_xauusd_zero_tick_value_params(0.1), "USD")

    with (
        patch("live_trading.risk.aggregate_risk.log.info") as guard_log,
        pytest.raises(AggregateRiskLimitError, match="below the minimum-lot risk"),
    ):
        size_order_for_aggregate_risk(
            positions=[],
            direction="SELL",
            entry_price=2300.0,
            stop_loss=2300.0 + (3.587 * 3.0),
            account_balance=9171.44,
            per_trade_risk_percent=1.0,
            aggregate_risk_percent=2.0,
            spec=spec,
            account_equity=9171.44,
        )

    assert guard_log.call_count == 1
    assert guard_log.call_args.args[1] == "BLOCKED_MINIMUM_LOT"
    assert guard_log.call_args.args[24] == "107.61"

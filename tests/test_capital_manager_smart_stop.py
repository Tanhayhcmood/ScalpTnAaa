from live_trading.risk.capital_manager import CapitalInput, calc_trade_parameters


def _input(**overrides):
    values = dict(
        direction="BUY", entry_price=100.0, atr=1.0, account_balance=10_000.0,
        swing_low=98.0, sl_atr_multiplier=3.0, sl_min_atr_multiplier=1.5,
        structure_buffer_atr=0.2,
    )
    values.update(overrides)
    return CapitalInput(**values)


def test_buy_uses_nearest_structural_invalidation_with_atr_buffer():
    result = calc_trade_parameters(_input())

    assert result.stop_loss == 97.8
    assert result.stop_loss_mode == "STRUCTURAL"
    assert result.structural_level == 98.0
    assert result.stop_loss_valid is True


def test_sell_uses_directionally_valid_resistance():
    result = calc_trade_parameters(_input(direction="SELL", entry_price=100.0, swing_high=102.0))

    assert result.stop_loss == 102.2
    assert result.stop_loss_mode == "STRUCTURAL"
    assert result.structural_level == 102.0


def test_too_far_structure_blocks_instead_of_clipping_inside_invalidation():
    result = calc_trade_parameters(_input(swing_low=95.0))

    assert result.stop_loss_valid is False
    assert result.stop_loss_mode == "STRUCTURE_TOO_FAR"
    assert "risk envelope" in result.stop_loss_reason


def test_missing_structure_uses_bounded_atr_fallback():
    result = calc_trade_parameters(_input(swing_low=None))

    assert result.stop_loss_mode == "ATR_FALLBACK"
    assert result.stop_loss_valid is True
    assert result.stop_loss == 97.0


def test_lot_size_still_uses_actual_smart_stop_distance():
    result = calc_trade_parameters(_input())

    assert result.sl_distance_usd == 2.2
    assert result.risk_amount <= result.risk_budget

import importlib
import os

import pytest

from live_trading import config
from live_trading.risk import capital_manager
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


def test_default_sl_multiplier_is_two_and_config_bounds_hold(monkeypatch):
    original_sl = os.environ.get("SL_ATR_BASE_MULTIPLIER")
    original_profile = os.environ.get("SCALP_PROFILE")
    original_max_open = os.environ.get("MAX_OPEN_TRADES")
    try:
        monkeypatch.setenv("SCALP_PROFILE", "off")
        monkeypatch.delenv("SL_ATR_BASE_MULTIPLIER", raising=False)
        monkeypatch.delenv("MAX_OPEN_TRADES", raising=False)
        settings = importlib.reload(config)
        assert settings.SL_ATR_BASE_MULTIPLIER == 2.0
        assert settings.MAX_OPEN_TRADES == 2
        assert capital_manager.DEFAULT_SL_ATR_MULT == 2.0

        for boundary in ("1.5", "3.5"):
            monkeypatch.setenv("SL_ATR_BASE_MULTIPLIER", boundary)
            assert importlib.reload(config).SL_ATR_BASE_MULTIPLIER == float(boundary)

        for invalid in ("1.49", "3.51"):
            monkeypatch.setenv("SL_ATR_BASE_MULTIPLIER", invalid)
            with pytest.raises(SystemExit):
                importlib.reload(config)
    finally:
        if original_sl is None:
            monkeypatch.delenv("SL_ATR_BASE_MULTIPLIER", raising=False)
        else:
            monkeypatch.setenv("SL_ATR_BASE_MULTIPLIER", original_sl)
        if original_profile is None:
            monkeypatch.delenv("SCALP_PROFILE", raising=False)
        else:
            monkeypatch.setenv("SCALP_PROFILE", original_profile)
        if original_max_open is None:
            monkeypatch.delenv("MAX_OPEN_TRADES", raising=False)
        else:
            monkeypatch.setenv("MAX_OPEN_TRADES", original_max_open)
        importlib.reload(config)


def test_take_profit_distance_is_capped_at_four_protective_atr():
    result = calc_trade_parameters(
        CapitalInput(
            direction="BUY",
            entry_price=100.0,
            atr=3.0,
            account_balance=10_000.0,
            take_profit_rr=3.0,
            sl_atr_multiplier=3.5,
            sl_min_atr_multiplier=1.5,
        )
    )

    assert capital_manager.TP_MAX_ATR == 4.0
    assert result.take_profit == 112.0
    assert result.take_profit - result.entry_price <= 4.0 * 3.0

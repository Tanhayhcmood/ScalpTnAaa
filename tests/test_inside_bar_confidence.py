"""Confidence regression tests for Price Action continuation setups."""

from types import SimpleNamespace

from live_trading.signals.confidence_engine import _calc_pa_score


def _pa(**overrides):
    values = {
        "bullish_engulf": False,
        "bearish_engulf": False,
        "bullish_pin_bar": False,
        "bearish_pin_bar": False,
        "strong_bullish": False,
        "strong_bearish": False,
        "valid_bull_breakout": False,
        "valid_bear_breakout": False,
        "bullish_pullback": False,
        "bearish_pullback": False,
        "near_demand_zone": False,
        "near_supply_zone": False,
        "near_support": False,
        "near_resistance": False,
        "fake_bull_breakout": False,
        "fake_bear_breakout": False,
        "pa_signal": "NEUTRAL",
        "inside_bar_depth": 1,
        "bullish_inside_breakout": False,
        "bearish_inside_breakout": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_bullish_inside_bar_breakout_adds_directional_confidence_points():
    score, reasons = _calc_pa_score(
        _pa(bullish_inside_breakout=True, pa_signal="BUY"),
        "BUY",
    )

    assert score == 3.0
    assert any("Bullish Inside-Bar Breakout" in reason for reason in reasons)


def test_inside_bar_confidence_is_directional():
    score, reasons = _calc_pa_score(
        _pa(bearish_inside_breakout=True, pa_signal="SELL"),
        "BUY",
    )

    assert score == 0.0
    assert reasons == []
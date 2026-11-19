import importlib
import os

import pytest

from live_trading.signals.market_regime import REGIME_RULES
from live_trading.trading.active_entry_policy import ACTIVE_ENTRY_STRATEGY, evaluate_active_entry


def _decision(**overrides):
    values = {
        "allowed": True,
        "direction": "BUY",
        "regime": "STRONG_TREND_BULL",
        "trend": SimpleNamespace(trend="BULLISH"),
        "pa": SimpleNamespace(
            pa_signal="BUY",
            valid_bull_breakout=True,
            valid_bear_breakout=False,
            bullish_inside_breakout=False,
            bearish_inside_breakout=False,
            bullish_pullback=False,
            bearish_pullback=False,
        ),
        "entry_filter": SimpleNamespace(trend=True, price_action=True, smc=False, wyckoff=False),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_only_aligned_trend_pa_breakout_can_pass():
    result = evaluate_active_entry(_decision(), symbol="XAUUSD", timeframe="5m")
    assert result.allowed is True
    assert result.strategy == ACTIVE_ENTRY_STRATEGY


def test_range_entries_are_blocked():
    result = evaluate_active_entry(_decision(regime="RANGE"), symbol="XAUUSD", timeframe="5m")
    assert result.allowed is False
    assert "not a directional XAUUSD scalp setup" in result.reason


def test_low_volatility_entries_are_blocked():
    result = evaluate_active_entry(_decision(regime="LOW_VOLATILITY"), symbol="XAUUSD", timeframe="5m")
    assert result.allowed is False


def test_standalone_price_action_is_blocked():
    result = evaluate_active_entry(_decision(entry_filter=SimpleNamespace(trend=False, price_action=True, smc=False, wyckoff=False)), symbol="XAUUSD", timeframe="5m")
    assert result.allowed is False
    assert "Trend and Price Action confirmation" in result.reason


def test_standalone_trend_is_blocked():
    result = evaluate_active_entry(_decision(entry_filter=SimpleNamespace(trend=True, price_action=False, smc=False, wyckoff=False)), symbol="XAUUSD", timeframe="5m")
    assert result.allowed is False


def test_counter_trend_is_blocked():
    result = evaluate_active_entry(_decision(direction="SELL", trend=SimpleNamespace(trend="BULLISH"), pa=SimpleNamespace(pa_signal="SELL", valid_bear_breakout=True), entry_filter=SimpleNamespace(trend=True, price_action=True, smc=False, wyckoff=False)), symbol="XAUUSD", timeframe="5m")
    assert result.allowed is False


def test_non_five_minute_execution_is_blocked():
    result = evaluate_active_entry(_decision(), symbol="XAUUSD", timeframe="1m")
    assert result.allowed is False


def test_only_supported_scalp_regimes_can_pass():
    supported = {"STRONG_TREND_BULL", "STRONG_TREND_BEAR", "WEAK_TREND_BULL", "WEAK_TREND_BEAR", "PULLBACK_BULL", "PULLBACK_BEAR"}
    for regime in sorted(REGIME_RULES):
        result = evaluate_active_entry(_decision(regime=regime), symbol="XAUUSD", timeframe="5m")
        assert result.allowed is (regime in supported), (regime, result.reason)

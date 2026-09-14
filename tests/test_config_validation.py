"""
Config Validation Tests — live_trading/config.py
================================================
Tests that config.py correctly reads environment variables and uses safe defaults.
Does NOT test strategy thresholds — only that env vars are parsed and typed correctly.

These tests verify ENGINEERING correctness (env var reading, type conversion, defaults).
They do NOT verify strategy parameters (thresholds, lookbacks, signal logic).
"""
import os
import importlib
import sys
import pytest


def _reload_config(env_overrides: dict) -> object:
    """Reload live_trading.config with the given env vars applied."""
    # Backup existing env
    backup = {}
    for k in env_overrides:
        backup[k] = os.environ.get(k)
    try:
        os.environ.update(env_overrides)
        # Remove cached module so importlib re-evaluates env vars
        if "live_trading.config" in sys.modules:
            del sys.modules["live_trading.config"]
        import live_trading.config as cfg
        return cfg
    finally:
        # Restore original env
        for k, v in backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if "live_trading.config" in sys.modules:
            del sys.modules["live_trading.config"]


class TestConfigDefaults:
    """Verify that default values are correct when no env vars are set."""

    def test_symbol_default(self):
        """When SYMBOL is not set, default is XAUUSD (AMarkets reports gold as
        XAUUSD, not XAUUSDb -- see CHANGELOG 4.0.3 render.yaml SYMBOL fix)."""
        cfg = _reload_config({})
        assert cfg.SYMBOL == "XAUUSD"

    def test_risk_percent_default(self):
        cfg = _reload_config({})
        assert cfg.RISK_PERCENT == 1.0
        assert isinstance(cfg.RISK_PERCENT, float)

    def test_min_confirmations_default(self):
        """Ordinary entries keep the operator-selected default floor."""
        cfg = _reload_config({})
        assert cfg.MIN_CONFIRMATIONS == 1
        assert isinstance(cfg.MIN_CONFIRMATIONS, int)

    def test_trend_min_confirmations_default(self):
        cfg = _reload_config({})
        assert cfg.TREND_MIN_CONFIRMATIONS == 2
        assert isinstance(cfg.TREND_MIN_CONFIRMATIONS, int)

    def test_range_min_confirmations_default(self):
        cfg = _reload_config({})
        assert cfg.RANGE_MIN_CONFIRMATIONS == 1
        assert isinstance(cfg.RANGE_MIN_CONFIRMATIONS, int)

    def test_max_open_trades_default_is_one_per_strategy_slot(self):
        cfg = _reload_config({})
        assert cfg.MAX_OPEN_TRADES == 4
        assert isinstance(cfg.MAX_OPEN_TRADES, int)

    def test_daily_loss_limit_default(self):
        cfg = _reload_config({})
        assert cfg.DAILY_LOSS_LIMIT_PCT == 3.0
        assert isinstance(cfg.DAILY_LOSS_LIMIT_PCT, float)

    def test_max_drawdown_default(self):
        cfg = _reload_config({})
        assert cfg.MAX_DRAWDOWN_PCT == 8.0
        assert isinstance(cfg.MAX_DRAWDOWN_PCT, float)

    def test_slippage_points_default(self):
        cfg = _reload_config({})
        assert cfg.SLIPPAGE_POINTS == 30
        assert isinstance(cfg.SLIPPAGE_POINTS, int)

    def test_state_file_default(self):
        cfg = _reload_config({})
        assert cfg.STATE_FILE == "robot_state.json"

    def test_empty_token_default(self):
        """Empty METAAPI_TOKEN is a valid default (checked at startup)."""
        cfg = _reload_config({"METAAPI_TOKEN": ""})
        assert cfg.METAAPI_TOKEN == ""


class TestConfigEnvOverrides:
    """Verify that env var overrides are correctly applied."""

    def test_symbol_override(self):
        cfg = _reload_config({"SYMBOL": "EURUSD"})
        assert cfg.SYMBOL == "EURUSD"

    def test_risk_percent_override(self):
        cfg = _reload_config({"RISK_PERCENT": "0.5"})
        assert cfg.RISK_PERCENT == 0.5

    def test_daily_loss_override(self):
        cfg = _reload_config({"DAILY_LOSS_LIMIT_PCT": "5.0"})
        assert cfg.DAILY_LOSS_LIMIT_PCT == 5.0

    def test_slippage_override(self):
        cfg = _reload_config({"SLIPPAGE_POINTS": "50"})
        assert cfg.SLIPPAGE_POINTS == 50

    def test_state_file_override(self):
        cfg = _reload_config({"STATE_FILE": "/data/robot_state.json"})
        assert cfg.STATE_FILE == "/data/robot_state.json"

    def test_log_file_override(self):
        cfg = _reload_config({"LOG_FILE": "/data/robot.log"})
        assert cfg.LOG_FILE == "/data/robot.log"

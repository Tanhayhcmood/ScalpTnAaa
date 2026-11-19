from types import SimpleNamespace

from live_trading.signals.market_regime import REGIME_RULES
from live_trading.trading.active_entry_policy import (
    ACTIVE_ENTRY_STRATEGY,
    ACTIVE_ENTRY_TIMEFRAME,
    MINIMUM_DIRECTIONAL_ADX,
    evaluate_active_entry,
    strong_trend_validation_reason,
)


def _decision(**overrides):
    values = {
        "allowed": True,
        "direction": "BUY",
        "regime": "STRONG_TREND_BULL",
        "trend": SimpleNamespace(trend="BULLISH"),
        "quality_filter": SimpleNamespace(adx=25.0),
        "pa": SimpleNamespace(
            pa_signal="BUY",
            valid_bull_breakout=True,
            valid_bear_breakout=False,
            bullish_inside_breakout=False,
            bearish_inside_breakout=False,
            bullish_pullback=False,
            bearish_pullback=False,
        ),
        "entry_filter": SimpleNamespace(
            trend=True,
            price_action=True,
            smc=False,
            wyckoff=False,
        ),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_active_entry_policy_is_m1():
    assert ACTIVE_ENTRY_TIMEFRAME == "1m"
    result = evaluate_active_entry(
        _decision(), symbol="XAUUSD", timeframe="1m"
    )
    assert result.allowed is True
    assert result.strategy == ACTIVE_ENTRY_STRATEGY


def test_old_m5_execution_is_blocked():
    result = evaluate_active_entry(
        _decision(), symbol="XAUUSD", timeframe="5m"
    )
    assert result.allowed is False
    assert "execution timeframe must remain 1m" in result.reason


def test_range_entries_are_blocked():
    result = evaluate_active_entry(
        _decision(regime="RANGE"), symbol="XAUUSD", timeframe="1m"
    )
    assert result.allowed is False
    assert "not a directional XAUUSD scalp setup" in result.reason


def test_low_adx_entry_is_blocked():
    result = evaluate_active_entry(
        _decision(quality_filter=SimpleNamespace(adx=MINIMUM_DIRECTIONAL_ADX - 1)),
        symbol="XAUUSD",
        timeframe="1m",
    )
    assert result.allowed is False
    assert "ADX" in result.reason


def test_missing_adx_is_blocked():
    result = evaluate_active_entry(
        _decision(quality_filter=SimpleNamespace(adx=None)),
        symbol="XAUUSD",
        timeframe="1m",
    )
    assert result.allowed is False
    assert "ADX" in result.reason


def test_standalone_price_action_is_blocked():
    result = evaluate_active_entry(
        _decision(
            entry_filter=SimpleNamespace(
                trend=False,
                price_action=True,
                smc=False,
                wyckoff=False,
            )
        ),
        symbol="XAUUSD",
        timeframe="1m",
    )
    assert result.allowed is False
    assert "Trend and Price Action" in result.reason


def test_standalone_trend_is_blocked():
    result = evaluate_active_entry(
        _decision(
            entry_filter=SimpleNamespace(
                trend=True,
                price_action=False,
                smc=False,
                wyckoff=False,
            )
        ),
        symbol="XAUUSD",
        timeframe="1m",
    )
    assert result.allowed is False


def test_counter_trend_is_blocked():
    result = evaluate_active_entry(
        _decision(
            direction="SELL",
            trend=SimpleNamespace(trend="BULLISH"),
            pa=SimpleNamespace(
                pa_signal="SELL",
                valid_bear_breakout=True,
                bearish_inside_breakout=False,
                bearish_pullback=False,
            ),
            entry_filter=SimpleNamespace(
                trend=True,
                price_action=True,
                smc=False,
                wyckoff=False,
            ),
        ),
        symbol="XAUUSD",
        timeframe="1m",
    )
    assert result.allowed is False


def test_strong_trend_guard_still_blocks_missing_structural_confirmation():
    decision = _decision(
        quality_filter=SimpleNamespace(adx=45.0),
        trend=SimpleNamespace(trend="NEUTRAL"),
        pa=SimpleNamespace(
            pa_signal="BUY",
            valid_bull_breakout=False,
            bullish_inside_breakout=False,
            bullish_pullback=False,
        ),
    )
    reason = strong_trend_validation_reason(decision)
    assert reason is not None
    assert "final validation blocked" in reason


def test_only_supported_scalp_regimes_can_pass():
    supported = {
        "STRONG_TREND_BULL",
        "STRONG_TREND_BEAR",
        "WEAK_TREND_BULL",
        "WEAK_TREND_BEAR",
        "PULLBACK_BULL",
        "PULLBACK_BEAR",
    }
    for regime in sorted(REGIME_RULES):
        pa_direction = "BUY" if regime.endswith("BULL") else "SELL"
        pa_fields = {
            "pa_signal": pa_direction,
            "valid_bull_breakout": pa_direction == "BUY",
            "valid_bear_breakout": pa_direction == "SELL",
            "bullish_inside_breakout": False,
            "bearish_inside_breakout": False,
            "bullish_pullback": False,
            "bearish_pullback": False,
        }
        trend_direction = "BULLISH" if pa_direction == "BUY" else "BEARISH"
        result = evaluate_active_entry(
            _decision(
                regime=regime,
                direction=pa_direction,
                trend=SimpleNamespace(trend=trend_direction),
                pa=SimpleNamespace(**pa_fields),
            ),
            symbol="XAUUSD",
            timeframe="1m",
        )
        assert result.allowed is (regime in supported), (regime, result.reason)

from types import SimpleNamespace

from live_trading.trading.active_entry_policy import (
    ACTIVE_ENTRY_STRATEGY,
    evaluate_active_entry,
)


def _decision(**overrides):
    values = {
        "allowed": True,
        "direction": "BUY",
        "regime": "STRONG_TREND_BULL",
        "trend": SimpleNamespace(trend="BULLISH"),
        "pa": SimpleNamespace(
            valid_bull_breakout=True,
            valid_bear_breakout=False,
            bullish_inside_breakout=False,
            bearish_inside_breakout=False,
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


def test_only_active_strategy_can_pass_the_runtime_gate():
    result = evaluate_active_entry(
        _decision(),
        symbol="XAUUSD",
        timeframe="1m",
    )

    assert result.allowed is True
    assert result.strategy == ACTIVE_ENTRY_STRATEGY


def test_range_entries_are_blocked_even_when_legacy_decision_passes():
    result = evaluate_active_entry(
        _decision(regime="RANGE"),
        symbol="XAUUSD",
        timeframe="1m",
    )

    assert result.allowed is False
    assert "not an active volatility/trend regime" in result.reason


def test_standalone_price_action_without_trend_is_blocked():
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
    assert "Trend + Price Action" in result.reason


def test_smc_and_wyckoff_cannot_authorize_entries_independently():
    for engine in ("smc", "wyckoff"):
        result = evaluate_active_entry(
            _decision(
                entry_filter=SimpleNamespace(
                    trend=False,
                    price_action=False,
                    smc=engine == "smc",
                    wyckoff=engine == "wyckoff",
                )
            ),
            symbol="XAUUSD",
            timeframe="1m",
        )

        assert result.allowed is False
        assert "Trend + Price Action" in result.reason


def test_counter_trend_entries_are_blocked():
    result = evaluate_active_entry(
        _decision(
            direction="SELL",
            trend=SimpleNamespace(trend="BULLISH"),
        ),
        symbol="XAUUSD",
        timeframe="1m",
    )

    assert result.allowed is False
    assert "counter-trend" in result.reason


def test_old_generic_breakout_without_active_trend_breakout_pair_is_blocked():
    result = evaluate_active_entry(
        _decision(
            pa=SimpleNamespace(
                valid_bull_breakout=False,
                valid_bear_breakout=False,
                bullish_inside_breakout=False,
                bearish_inside_breakout=False,
            )
        ),
        symbol="XAUUSD",
        timeframe="1m",
    )

    assert result.allowed is False
    assert "no directional Price Action breakout" in result.reason


def test_non_one_minute_execution_is_blocked_without_changing_config():
    result = evaluate_active_entry(
        _decision(),
        symbol="XAUUSD",
        timeframe="5m",
    )

    assert result.allowed is False
    assert "execution timeframe must remain 1m" in result.reason
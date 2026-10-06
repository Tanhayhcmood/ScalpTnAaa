"""Regression tests for profit-lock activation and monotonic trailing."""

from live_trading.risk.adaptive_trailing_stop import (
    AdaptiveTrailingConfig,
    compute_adaptive_trail,
    detect_exhaustion,
    should_apply,
)


def candle(open_price, high, low, close, volume=100):
    return {
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def _candles():
    return [
        {"open": 100.0, "high": 102.0, "low": 98.0, "close": 100.0}
        for _ in range(20)
    ]


def _config():
    return AdaptiveTrailingConfig(
        atr_period=14,
        chandelier_atr_multiplier=2.5,
        profit_lock_trigger_r=0.5,
        profit_lock_r=0.1,
    )


def test_waits_until_half_r_then_locks_one_tenth_r_for_buy():
    waiting = compute_adaptive_trail(
        direction="BUY",
        current_price=104.9,
        candles=_candles(),
        cfg=_config(),
        entry_price=100.0,
        initial_sl=90.0,
        current_sl=90.0,
    )
    assert waiting.stage == "WAITING_FOR_PROFIT_LOCK"
    assert waiting.candidate_sl is None
    assert waiting.profit_lock_armed is False

    locked = compute_adaptive_trail(
        direction="BUY",
        current_price=105.0,
        candles=_candles(),
        cfg=_config(),
        entry_price=100.0,
        initial_sl=90.0,
        current_sl=90.0,
    )
    assert locked.stage == "PROFIT_LOCK"
    assert locked.candidate_sl == 101.0
    assert locked.profit_lock_armed is True
    assert locked.breakeven_armed is False


def test_profit_lock_remains_armed_after_retracement():
    decision = compute_adaptive_trail(
        direction="BUY",
        current_price=99.0,
        candles=_candles(),
        cfg=_config(),
        entry_price=100.0,
        initial_sl=90.0,
        current_sl=90.0,
        profit_lock_armed=True,
    )

    assert decision.stage == "PROFIT_LOCK"
    assert decision.candidate_sl == 101.0
    assert decision.profit_lock_armed is True


def test_sell_uses_symmetric_profit_lock_and_chandelier_only_after_one_r():
    locked = compute_adaptive_trail(
        direction="SELL",
        current_price=95.0,
        candles=_candles(),
        cfg=_config(),
        entry_price=100.0,
        initial_sl=110.0,
        current_sl=110.0,
    )
    assert locked.stage == "PROFIT_LOCK"
    assert locked.candidate_sl == 99.0

    trailed = compute_adaptive_trail(
        direction="SELL",
        current_price=90.0,
        candles=_candles(),
        cfg=_config(),
        entry_price=100.0,
        initial_sl=110.0,
        current_sl=99.0,
        lowest_price_since_entry=90.0,
        profit_lock_armed=True,
    )
    assert trailed.stage == "CHANDELIER"
    assert trailed.breakeven_armed is True
    assert trailed.candidate_sl == 100.0


def test_should_apply_never_moves_a_stop_backwards():
    assert should_apply("BUY", 101.0, 99.0, 0.05) is False
    assert should_apply("SELL", 99.0, 101.0, 0.05) is False
    assert should_apply("BUY", 99.0, 101.0, 0.05) is True
    assert should_apply("SELL", 101.0, 99.0, 0.05) is True


def _exhaustion_candles():
    candles = [candle(100, 102, 99, 101, 100) for _ in range(10)]
    for index, body in enumerate((0.7, 0.45, 0.2)):
        start = 101 + index * body
        candles.append(candle(start, start + body, start - 0.5, start + body, 50))
    return candles


def test_negative_floating_profit_never_builds_a_stop_candidate():
    decision = compute_adaptive_trail(
        "BUY",
        99.0,
        [candle(100, 101, 99, 99.5) for _ in range(30)],
        AdaptiveTrailingConfig(),
        entry_price=100.0,
        initial_sl=98.0,
        current_sl=98.0,
    )
    assert decision.stage == "WAITING_FOR_PROFIT_LOCK"
    assert decision.candidate_sl is None
    assert decision.floating_profit_price == 0.0


def test_one_r_keeps_the_profit_lock_and_arms_chandelier():
    decision = compute_adaptive_trail(
        "BUY",
        102.0,
        [candle(100, 101, 100, 100.5) for _ in range(30)],
        AdaptiveTrailingConfig(),
        entry_price=100.0,
        initial_sl=98.0,
        current_sl=98.0,
    )
    assert decision.stage == "PROFIT_LOCK"
    assert decision.candidate_sl == 100.2
    assert decision.profit_lock_armed is True
    assert decision.breakeven_armed is True
    assert decision.floating_profit_r_multiple == 1.0


def test_chandelier_uses_highest_price_since_entry():
    decision = compute_adaptive_trail(
        "BUY",
        105.0,
        [candle(100, 101, 100, 100.5) for _ in range(30)],
        AdaptiveTrailingConfig(chandelier_atr_multiplier=2.5),
        entry_price=100.0,
        initial_sl=98.0,
        current_sl=100.2,
        highest_price_since_entry=106.0,
        breakeven_armed=True,
    )
    assert decision.stage == "CHANDELIER"
    assert decision.highest_price_since_entry == 106.0
    assert decision.candidate_sl == 103.5


def test_chandelier_uses_lowest_price_for_sell():
    decision = compute_adaptive_trail(
        "SELL",
        95.0,
        [candle(100, 101, 100, 100.5) for _ in range(30)],
        AdaptiveTrailingConfig(chandelier_atr_multiplier=2.5),
        entry_price=100.0,
        initial_sl=102.0,
        current_sl=99.8,
        lowest_price_since_entry=94.0,
        breakeven_armed=True,
    )
    assert decision.stage == "CHANDELIER"
    assert decision.lowest_price_since_entry == 94.0
    assert decision.candidate_sl == 96.5


def test_risk_distance_uses_initial_sl_and_legacy_saved_risk_fallback():
    candles = _exhaustion_candles()
    from_initial_sl = compute_adaptive_trail(
        "BUY",
        102.0,
        candles,
        AdaptiveTrailingConfig(),
        entry_price=100.0,
        initial_sl=98.0,
        current_sl=98.0,
    )
    legacy_fallback = compute_adaptive_trail(
        "BUY",
        102.0,
        candles,
        AdaptiveTrailingConfig(),
        entry_price=100.0,
        initial_sl=0.0,
        risk_distance=2.0,
        current_sl=98.0,
    )
    for decision in (from_initial_sl, legacy_fallback):
        assert decision.risk_distance == 2.0
        assert decision.stage == "PROFIT_LOCK"
        assert decision.candidate_sl == 100.2


def test_missing_volume_does_not_count_as_declining():
    signals = detect_exhaustion(
        [candle(100, 102, 99, 101, 0) for _ in range(30)],
        "BUY",
        AdaptiveTrailingConfig(),
    )
    assert signals.volume_declining is False

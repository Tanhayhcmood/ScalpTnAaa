"""Dedicated RANGE playbook regression tests."""

from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.price_action_engine import PriceActionResult
from live_trading.signals.range_strategy import evaluate_range_entry
from live_trading.signals.smc_engine import SmcLiquiditySweep, SmcResult


def _pa(bullish: bool = True) -> PriceActionResult:
    return PriceActionResult(
        bullish_engulf=False,
        bearish_engulf=False,
        bullish_pin_bar=bullish,
        bearish_pin_bar=not bullish,
        strong_bullish=False,
        strong_bearish=False,
        near_demand_zone=False,
        near_supply_zone=False,
        near_support=False,
        near_resistance=False,
        valid_bull_breakout=False,
        valid_bear_breakout=False,
        fake_bull_breakout=False,
        fake_bear_breakout=False,
        bullish_pullback=False,
        bearish_pullback=False,
        pa_signal="BUY" if bullish else "SELL",
        pa_score=0.8,
    )


def _smc(direction: str, bar_index: int) -> SmcResult:
    sweep_type = "BULLISH" if direction == "BUY" else "BEARISH"
    return SmcResult(
        timeframe="M5",
        timestamp="2026-08-12T12:20:00+00:00",
        current_price=101.8,
        trend="BULLISH" if direction == "BUY" else "BEARISH",
        bos_signals=[],
        choch_signals=[],
        order_blocks=[],
        fair_value_gaps=[],
        liquidity_sweeps=[
            SmcLiquiditySweep(sweep_type, 100.0, 99.5, bar_index, "2026-08-12T12:20:00+00:00")
        ],
        equal_highs=[],
        equal_lows=[],
        mitigation_blocks=[],
        smc_signal=direction,
        smc_score=0.8,
    )


def _candles() -> list[OHLCV]:
    history = [
        OHLCV(
            time=f"2026-08-12T12:{i:02d}:00+00:00",
            open=105.0,
            high=110.0,
            low=100.0,
            close=105.0,
            volume=100.0,
        )
        for i in range(20)
    ]
    return history + [
        OHLCV(
            time="2026-08-12T12:20:00+00:00",
            open=101.5,
            high=102.1,
            low=99.5,
            close=101.8,
            volume=150.0,
        )
    ]


def test_range_buy_requires_edge_sweep_reversal_and_one_confirmation():
    result = evaluate_range_entry(
        _candles(), "BUY", _smc("BUY", 20), _pa(True), 1, 1
    )
    assert result.valid
    assert result.location == "SUPPORT"
    assert result.liquidity_sweep
    assert result.reversal_candle


def test_range_buy_accepts_one_confirmation_when_other_range_gates_pass():
    result = evaluate_range_entry(
        _candles(), "BUY", _smc("BUY", 20), _pa(True), 1, 1
    )
    assert result.valid
    assert result.confirmation_count == 1


def test_range_blocks_middle_of_range_even_with_confirmations():
    candles = _candles()
    candles[-1] = OHLCV(
        time=candles[-1].time,
        open=105.0,
        high=106.0,
        low=104.0,
        close=105.5,
        volume=100.0,
    )
    result = evaluate_range_entry(
        candles, "BUY", _smc("BUY", 20), _pa(True), 2, 2
    )
    assert not result.valid
    assert result.location == "MIDDLE"


def test_range_blocks_stale_liquidity_sweep():
    result = evaluate_range_entry(
        _candles(), "BUY", _smc("BUY", 10), _pa(True), 2, 2
    )
    assert not result.valid
    assert "Liquidity Sweep" in result.reason


def test_relaxed_range_filters_keep_context_without_blocking():
    candles = _candles()
    candles[-1] = OHLCV(
        time=candles[-1].time,
        open=105.0,
        high=106.0,
        low=104.0,
        close=105.5,
        volume=100.0,
    )
    result = evaluate_range_entry(
        candles,
        "BUY",
        _smc("BUY", 10),
        _pa(True),
        confirmation_count=1,
        min_confirmations=2,
        strict_filters=False,
    )
    assert result.valid
    assert result.location == "MIDDLE"
    assert "strict Option 2 filters disabled" in result.reason
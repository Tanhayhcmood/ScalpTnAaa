from types import SimpleNamespace

import pytest

from live_trading.signals.confidence_engine import (
    _calc_smc_score,
    calc_confidence,
)


@pytest.mark.parametrize("candidate", ["BUY", "SELL"])
def test_smc_structure_never_contributes_to_trade_confidence(candidate):
    smc = SimpleNamespace(
        smc_signal="BUY" if candidate == "BUY" else "SELL",
        smc_score=100.0,
        order_blocks=[{"top": 2600.0, "bottom": 2500.0}],
        fair_value_gaps=[{"top": 2600.0, "bottom": 2500.0}],
    )

    score, reasons = _calc_smc_score(smc, candidate)

    assert score == 0.0
    assert reasons == []


def test_full_confidence_result_is_unchanged_by_smc_structure():
    wyckoff = SimpleNamespace(
        wyckoff_signal="NEUTRAL",
        wyckoff_score=0.0,
        spring=False,
        upthrust=False,
        volume_confirmed=False,
        phase="NEUTRAL",
    )
    pa = SimpleNamespace(
        bullish_engulf=True,
        bearish_engulf=False,
        bullish_pin_bar=False,
        bearish_pin_bar=False,
        strong_bullish=False,
        strong_bearish=False,
        bullish_inside_breakout=False,
        bearish_inside_breakout=False,
        inside_bar_depth=0,
        valid_bull_breakout=True,
        valid_bear_breakout=False,
        bullish_pullback=False,
        bearish_pullback=False,
        near_demand_zone=False,
        near_support=False,
        near_supply_zone=False,
        near_resistance=False,
        fake_bull_breakout=False,
        fake_bear_breakout=False,
        pa_signal="BUY",
        pa_score=0.8,
    )
    trend = SimpleNamespace(trend="BULLISH", strength="STRONG")
    regime = SimpleNamespace(
        adx=30.0,
        atr_ratio=1.0,
        rules=SimpleNamespace(min_confidence=40.0),
    )
    no_smc = SimpleNamespace(smc_signal="NEUTRAL", smc_score=0.0)
    strong_smc = SimpleNamespace(
        smc_signal="BUY",
        smc_score=100.0,
        order_blocks=[object()],
        fair_value_gaps=[object()],
        liquidity_sweeps=[object()],
    )

    baseline = calc_confidence(
        no_smc, wyckoff, pa, trend, regime, "PRIME", "BUY", candles=[]
    )
    with_smc = calc_confidence(
        strong_smc, wyckoff, pa, trend, regime, "PRIME", "BUY", candles=[]
    )

    assert with_smc.confidence == baseline.confidence
    assert with_smc.components == baseline.components
    assert with_smc.reasoning == baseline.reasoning

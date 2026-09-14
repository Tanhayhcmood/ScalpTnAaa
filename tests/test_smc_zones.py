"""Focused regression tests for SMC zone lifecycle and retests."""

from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.smc_engine import (
    CFG_M5,
    SmcOrderBlock,
    _detect_fvgs,
    _compute_smc_signal,
    detect_order_block_fake_breakout,
    SmcResult,
)


def _candle(index: int, open_: float, high: float, low: float, close: float) -> OHLCV:
    return OHLCV(
        time=f"2026-08-11T12:{index:02d}:00+00:00",
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=100.0,
    )


def _empty_smc(ob: SmcOrderBlock) -> SmcResult:
    return SmcResult(
        timeframe="M5",
        timestamp="2026-08-11T12:04:00+00:00",
        current_price=99.80,
        trend="BULLISH",
        bos_signals=[],
        choch_signals=[],
        order_blocks=[ob],
        fair_value_gaps=[],
        liquidity_sweeps=[],
        equal_highs=[],
        equal_lows=[],
        mitigation_blocks=[],
        smc_signal="BUY",
        smc_score=0.8,
    )


def test_fvg_touch_is_partial_but_far_edge_is_full_fill():
    base = [
        _candle(0, 100.0, 100.2, 99.8, 100.1),
        _candle(1, 100.1, 100.4, 100.0, 100.2),
        _candle(2, 100.8, 101.2, 100.7, 101.0),
    ]

    partial = _detect_fvgs(
        base + [_candle(3, 100.9, 101.0, 100.45, 100.8)],
        CFG_M5,
    )
    assert partial
    assert partial[-1].filled is False
    assert partial[-1].fill_state == "PARTIAL"

    full = _detect_fvgs(
        base + [_candle(3, 100.9, 101.0, 100.15, 100.4)],
        CFG_M5,
    )
    assert full == []


def test_bullish_ob_retest_with_rejection_is_not_fake_breakout():
    ob = SmcOrderBlock(
        type="BULLISH",
        high=100.0,
        low=99.0,
        open=99.8,
        close=99.2,
        bar_index=10,
        time="2026-08-11T11:00:00+00:00",
        mitigated=True,
        mitigation_state="RETESTED",
    )
    # A close in the upper half after a bullish break is a valid retest.
    candles = [
        _candle(0, 99.6, 99.8, 99.4, 99.6),
        _candle(1, 99.6, 100.5, 99.5, 100.3),
        _candle(2, 99.4, 100.0, 99.2, 99.8),
    ]
    assert detect_order_block_fake_breakout(
        candles, _empty_smc(ob), "BUY"
    ) is None


def test_opposing_live_zone_suppresses_stale_bos_direction():
    ob = SmcOrderBlock(
        type="BULLISH",
        high=100.0,
        low=99.0,
        open=99.8,
        close=99.2,
        bar_index=10,
        time="2026-08-11T11:00:00+00:00",
        mitigated=True,
        mitigation_state="RETESTED",
    )
    signal, _score = _compute_smc_signal(
        "BEARISH",
        [],
        [],
        [ob],
        [],
        [],
        99.8,
        CFG_M5,
    )
    assert signal != "SELL"
"""Option 2 hard-gate tests for Fake Breakout inside an Order Block."""

from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.smc_engine import (
    SmcMitigationBlock,
    SmcOrderBlock,
    SmcResult,
    detect_order_block_fake_breakout,
)


def _candle(close: float, open_: float | None = None) -> OHLCV:
    open_price = close if open_ is None else open_
    return OHLCV(
        time="2026-08-11T12:00:00+00:00",
        open=open_price,
        high=max(open_price, close) + 0.20,
        low=min(open_price, close) - 0.20,
        close=close,
        volume=100.0,
    )


def _smc(ob: SmcOrderBlock, mitigated: bool = False) -> SmcResult:
    return SmcResult(
        timeframe="M5",
        timestamp="2026-08-11T12:00:00+00:00",
        current_price=100.50,
        trend="BULLISH" if ob.type == "BULLISH" else "BEARISH",
        bos_signals=[],
        choch_signals=[],
        order_blocks=[] if mitigated else [ob],
        fair_value_gaps=[],
        liquidity_sweeps=[],
        equal_highs=[],
        equal_lows=[],
        mitigation_blocks=[
            SmcMitigationBlock(
                original_ob=ob,
                mitigated_at_bar_index=2,
                mitigated_at_time="2026-08-11T12:00:00+00:00",
            )
        ] if mitigated else [],
        smc_signal="BUY" if ob.type == "BULLISH" else "SELL",
        smc_score=0.8,
    )


def test_option_two_blocks_bullish_fake_breakout_in_bullish_block():
    ob = SmcOrderBlock(
        type="BULLISH", high=100.00, low=99.00,
        open=99.80, close=99.20, bar_index=10,
        time="2026-08-11T11:00:00+00:00", mitigated=False,
    )

    result = detect_order_block_fake_breakout(
        [_candle(99.60), _candle(100.30), _candle(99.80)],
        _smc(ob),
        "BUY",
    )

    assert result == ob


def test_option_two_checks_mitigated_blocks_that_currently_hold_the_return():
    ob = SmcOrderBlock(
        type="BEARISH", high=101.00, low=100.00,
        open=100.20, close=100.80, bar_index=10,
        time="2026-08-11T11:00:00+00:00", mitigated=True,
    )

    result = detect_order_block_fake_breakout(
        [_candle(100.40), _candle(99.70), _candle(100.60)],
        _smc(ob, mitigated=True),
        "SELL",
    )

    assert result == ob


def test_option_two_does_not_block_without_a_close_back_inside_the_block():
    ob = SmcOrderBlock(
        type="BULLISH", high=100.00, low=99.00,
        open=99.80, close=99.20, bar_index=10,
        time="2026-08-11T11:00:00+00:00", mitigated=False,
    )

    result = detect_order_block_fake_breakout(
        [_candle(99.60), _candle(100.30), _candle(100.40)],
        _smc(ob),
        "BUY",
    )

    assert result is None
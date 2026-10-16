from types import SimpleNamespace

from live_trading.signals.decision_engine import _entry_breakout_block_reason
from live_trading.signals.price_action_engine import (
    CFG_M5,
    _detect_breakout_pullback,
)


def _bar(open_, high, low, close):
    return SimpleNamespace(open=open_, high=high, low=low, close=close)


def _breakout_flags(candles, *, support=(), resistance=()):
    return _detect_breakout_pullback(
        candles,
        list(support),
        list(resistance),
        [],
        [],
        CFG_M5,
        atr=1.0,
    )


def _entry_reason(direction, candles, *, level):
    flags = _breakout_flags(
        candles,
        support=[level] if direction == "SELL" else [],
        resistance=[level] if direction == "BUY" else [],
    )
    close = candles[-1].close
    near_level = abs(close - level) <= CFG_M5.level_tolerance
    pa = SimpleNamespace(
        near_resistance=near_level if direction == "BUY" else False,
        valid_bull_breakout=flags[0],
        bullish_pullback=flags[4],
        near_support=near_level if direction == "SELL" else False,
        valid_bear_breakout=flags[1],
        bearish_pullback=flags[5],
        fake_bull_breakout=flags[2],
        fake_bear_breakout=flags[3],
        bullish_inside_breakout=flags[6],
        bearish_inside_breakout=flags[7],
        breakout_overextended=False,
    )
    return flags, _entry_breakout_block_reason(direction, pa)


def test_staying_above_resistance_is_not_authorized_as_a_fresh_buy_breakout():
    candles = [
        _bar(100.05, 100.15, 99.98, 100.10),
        _bar(100.00, 100.25, 99.95, 100.22),
    ]

    flags, reason = _entry_reason("BUY", candles, level=100.0)

    assert not flags[0]
    assert reason == (
        "BUY blocked: price is near resistance without a confirmed "
        "breakout/retest"
    )


def test_staying_below_support_is_not_authorized_as_a_fresh_sell_breakdown():
    candles = [
        _bar(99.95, 100.02, 99.85, 99.90),
        _bar(100.00, 100.05, 99.75, 99.78),
    ]

    flags, reason = _entry_reason("SELL", candles, level=100.0)

    assert not flags[1]
    assert reason == (
        "SELL blocked: price is near support without a confirmed "
        "breakdown/retest"
    )


def test_close_crossing_above_resistance_remains_a_valid_buy_breakout():
    candles = [
        _bar(99.80, 100.00, 99.70, 99.90),
        _bar(99.98, 100.25, 99.95, 100.22),
    ]

    flags, reason = _entry_reason("BUY", candles, level=100.0)

    assert flags[0]
    assert reason is None


def test_close_crossing_below_support_remains_a_valid_sell_breakdown():
    candles = [
        _bar(100.20, 100.30, 100.05, 100.10),
        _bar(100.02, 100.05, 99.75, 99.78),
    ]

    flags, reason = _entry_reason("SELL", candles, level=100.0)

    assert flags[1]
    assert reason is None

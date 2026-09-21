from types import SimpleNamespace

from live_trading.signals.decision_engine import (
    _entry_location_block_reason,
    _target_room_block_reason,
)


def _pa(**overrides):
    values = {
        "near_resistance": False,
        "valid_bull_breakout": False,
        "bullish_pullback": False,
        "near_support": False,
        "valid_bear_breakout": False,
        "bearish_pullback": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_buy_at_resistance_requires_breakout_or_retest():
    assert _entry_location_block_reason(
        "BUY",
        _pa(near_resistance=True),
    )
    assert _entry_location_block_reason(
        "BUY",
        _pa(near_resistance=True, valid_bull_breakout=True),
    ) is None


def test_sell_at_support_requires_breakdown_or_retest():
    assert _entry_location_block_reason(
        "SELL",
        _pa(near_support=True),
    )
    assert _entry_location_block_reason(
        "SELL",
        _pa(near_support=True, bearish_pullback=True),
    ) is None


def test_target_room_gate_rejects_barrier_inside_required_rr():
    reason = _target_room_block_reason(
        "BUY",
        entry=100.0,
        stop_loss=98.0,
        structural_level=103.0,
        minimum_rr=1.5,
        atr=2.0,
    )
    assert reason is not None
    assert "structural room" in reason


def test_target_room_gate_allows_clear_room_and_ignores_wrong_side_level():
    assert _target_room_block_reason(
        "BUY",
        entry=100.0,
        stop_loss=98.0,
        structural_level=104.0,
        minimum_rr=1.5,
        atr=2.0,
    ) is None
    assert _target_room_block_reason(
        "SELL",
        entry=100.0,
        stop_loss=98.0,
        structural_level=103.0,
        minimum_rr=1.5,
        atr=2.0,
    ) is None
"""Option 4 regression tests: BOS/CHoCH freshness and event ordering."""

from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.quality_filter import _is_late_entry
from live_trading.signals.smc_engine import SmcBos, SmcChoch, SmcResult, get_latest_structure_event


def _flat_candles(count: int = 80) -> list[OHLCV]:
    return [OHLCV(time=f"2026-08-11T12:{i:02d}:00+00:00", open=100.0, high=100.2, low=99.8, close=100.0, volume=100.0) for i in range(count)]


def _smc(bos=None, choch=None) -> SmcResult:
    return SmcResult(timeframe="M5", timestamp="2026-08-11T12:00:00+00:00", current_price=100.0, trend="NEUTRAL", bos_signals=[] if bos is None else bos, choch_signals=[] if choch is None else choch, order_blocks=[], fair_value_gaps=[], liquidity_sweeps=[], equal_highs=[], equal_lows=[], mitigation_blocks=[], smc_signal="NEUTRAL", smc_score=0.0)


def test_newer_bos_beats_older_choch_for_candidate_structure():
    old_choch = SmcChoch("BUY", 100.0, 20, "2026-08-11T12:20:00+00:00")
    new_bos = SmcBos("SELL", 100.0, 70, "2026-08-11T13:10:00+00:00")
    assert get_latest_structure_event(_smc([new_bos], [old_choch])) is new_bos


def test_structure_event_older_than_24_closed_bars_is_late():
    candles = _flat_candles()
    assert _is_late_entry(candles, len(candles) - 26) is True


def test_structure_event_at_24_closed_bars_is_still_fresh():
    candles = _flat_candles()
    assert _is_late_entry(candles, len(candles) - 25) is False

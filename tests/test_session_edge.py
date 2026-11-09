from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
import os

from live_trading.signals.session_edge import (
    compute_asian_range,
    daily_anchored_vwap,
    evaluate_entry_context,
    get_session_edge_mode,
)

UTC = timezone.utc


def _bar(timestamp, *, high=100.4, low=100.0, close=100.2, volume=1.0):
    return {
        "time": timestamp.isoformat().replace("+00:00", "Z"),
        "open": close,
        "high": max(high, close),
        "low": min(low, close),
        "close": close,
        "volume": volume,
    }


def _asian_day(*, high=100.4, low=100.0):
    start = datetime(2026, 11, 9, 0, 0, tzinfo=UTC)
    return [
        _bar(start + timedelta(minutes=5 * index), high=high, low=low)
        for index in range(84)
    ]


class SessionEdgeTests(unittest.TestCase):
    def test_asian_range_uses_only_utc_midnight_to_0700(self):
        bars = _asian_day()
        bars.append(_bar(datetime(2026, 11, 9, 7, 0, tzinfo=UTC), high=150.0, low=90.0))
        result = compute_asian_range(bars)
        self.assertEqual(result["bar_count"], 84)
        self.assertAlmostEqual(result["high"], 100.4)
        self.assertAlmostEqual(result["low"], 100.0)
        self.assertTrue(result["valid"])

    def test_range_size_validity_bounds(self):
        valid = compute_asian_range(_asian_day(high=100.4, low=100.0))
        too_small = compute_asian_range(_asian_day(high=100.2, low=100.0))
        too_large = compute_asian_range(_asian_day(high=101.8, low=100.0))
        self.assertTrue(valid["valid"])
        self.assertFalse(too_small["valid"])
        self.assertFalse(too_large["valid"])
        self.assertEqual(too_small["status"], "NO_SESSION_EDGE")
        self.assertEqual(too_large["status"], "NO_SESSION_EDGE")

    def test_daily_vwap_resets_at_0000_utc(self):
        yesterday = _bar(datetime(2026, 11, 8, 23, 55, tzinfo=UTC), high=100.0, low=100.0, close=100.0, volume=1000)
        today = _bar(datetime(2026, 11, 9, 0, 0, tzinfo=UTC), high=200.0, low=200.0, close=200.0, volume=1)
        self.assertEqual(daily_anchored_vwap([yesterday, today]), 200.0)

    def test_shadow_is_the_default_mode(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_session_edge_mode(), "shadow")

    def test_matching_sweep_is_the_only_counter_vwap_exception(self):
        bars = _asian_day()
        bars.append(_bar(datetime(2026, 11, 9, 7, 0, tzinfo=UTC), high=100.2, low=99.8, close=100.2))
        # High-volume Asian bars keep VWAP above the sweep close; the low sweep
        # then permits a matching BUY only while the day's edge is unused.
        for bar in bars[:84]:
            bar["close"] = 100.4
            bar["open"] = 100.4
            bar["high"] = 100.5
            bar["low"] = 100.0
            bar["volume"] = 100.0
        result = evaluate_entry_context(bars, "BUY", mode="shadow")
        used = evaluate_entry_context(
            bars, "BUY", mode="shadow", session_edge_trade_dates={"2026-11-09"}
        )
        self.assertTrue(result["session_edge"]["active"])
        self.assertTrue(result["vwap_filter"]["asian_edge_override"])
        self.assertTrue(result["vwap_filter"]["would_allow"])
        self.assertFalse(used["vwap_filter"]["asian_edge_override"])
        self.assertFalse(used["vwap_filter"]["would_allow"])


if __name__ == "__main__":
    unittest.main()

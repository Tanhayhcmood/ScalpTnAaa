from __future__ import annotations

import csv
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from live_trading.signals.gold_engine import OHLCV
from live_trading.trading.active_entry_policy import ACTIVE_ENTRY_TIMEFRAME
from scripts import backtest_session_edge, walk_forward_report
from scripts.backtest_core import ALLOWED_DIRECTIONS, M5_TIMEFRAME, simulate_trade
from scripts.convert_histdata import convert_histdata


def _decision(direction: str, entry: float, stop: float, target: float):
    return SimpleNamespace(
        direction=direction,
        trade_params=SimpleNamespace(
            entry_price=entry,
            stop_loss=stop,
            take_profit=target,
            break_even_at=entry + (entry - stop),
            break_even_sl=entry,
            trailing_activation_at=entry + (entry - stop),
            trailing_stop_distance=(entry - stop) * 0.5,
        ),
    )


class BacktestCoreTests(unittest.TestCase):
    def test_shared_m5_constant_and_both_directions_default(self):
        self.assertEqual(M5_TIMEFRAME, ACTIVE_ENTRY_TIMEFRAME)
        self.assertEqual(M5_TIMEFRAME, "5m")
        self.assertEqual(ALLOWED_DIRECTIONS, frozenset({"BUY", "SELL"}))

    def test_stop_wins_when_stop_and_target_are_touched_in_one_bar(self):
        bars = [
            OHLCV("2026-06-01T00:00:00Z", 100, 100.2, 99.8, 100, 10),
            OHLCV("2026-06-01T00:05:00Z", 100, 103, 98, 101, 10),
        ]
        result = simulate_trade(bars, [0, 0.30], 0, _decision("BUY", 100, 99, 102))
        self.assertIsNotNone(result)
        self.assertEqual(result["exit_reason"], "stop")
        self.assertEqual(result["exit_price"], 99)
        self.assertAlmostEqual(float(result["net"]), -1.30)

    def test_live_break_even_trail_moves_stop_for_next_bar(self):
        bars = [
            OHLCV("2026-06-01T00:00:00Z", 100, 100.2, 99.8, 100, 10),
            OHLCV("2026-06-01T00:05:00Z", 100, 100.8, 99.5, 100.7, 10),
            OHLCV("2026-06-01T00:10:00Z", 100.7, 101, 100.1, 100.5, 10),
        ]
        result = simulate_trade(bars, [0, 0.30, 0.30], 0, _decision("BUY", 100, 99, 110))
        self.assertIsNotNone(result)
        self.assertEqual(result["exit_reason"], "stop")
        self.assertEqual(result["exit_index"], 2)
        self.assertAlmostEqual(float(result["net"]), -0.10)

    def test_buy_sell_mirror_symmetry(self):
        bars = [
            OHLCV("2026-06-01T00:00:00Z", 100, 100.2, 99.8, 100, 10),
            OHLCV("2026-06-01T00:05:00Z", 100, 100.8, 99.5, 100.7, 10),
            OHLCV("2026-06-01T00:10:00Z", 100.7, 101, 100.1, 100.5, 10),
        ]
        pivot = 500.0
        mirrored = [
            OHLCV(
                bar.time,
                pivot - bar.open,
                pivot - bar.low,
                pivot - bar.high,
                pivot - bar.close,
                bar.volume,
            )
            for bar in bars
        ]
        buy = simulate_trade(bars, [0, 0.30, 0.30], 0, _decision("BUY", 100, 99, 110))
        sell = simulate_trade(
            mirrored,
            [0, 0.30, 0.30],
            0,
            _decision("SELL", pivot - 100, pivot - 99, pivot - 110),
        )
        self.assertIsNotNone(buy)
        self.assertIsNotNone(sell)
        self.assertEqual(buy["exit_index"], sell["exit_index"])
        self.assertEqual(buy["exit_reason"], sell["exit_reason"])
        self.assertAlmostEqual(float(buy["net"]), float(sell["net"]))
        self.assertAlmostEqual(float(buy["r"]), float(sell["r"]))

    def test_session_edge_zero_entries_prints_top_blocked_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bars.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["time", "open", "high", "low", "close", "tick_volume"])
                start = datetime(2026, 6, 1, tzinfo=timezone.utc)
                for i in range(80):
                    stamp = start + timedelta(minutes=5 * i)
                    price = 2300 + i * 0.01
                    writer.writerow(
                        [stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), price, price + 0.2, price - 0.2, price, 10]
                    )
            observed_timeframes: list[str] = []

            def blocked_engine(*args, **kwargs):
                observed_timeframes.append(kwargs["timeframe"])
                return SimpleNamespace(
                    allowed=False,
                    blocked_reasons=["synthetic blocked reason"],
                    trade_params=None,
                )

            stderr = io.StringIO()
            stdout = io.StringIO()
            with patch.object(backtest_session_edge, "run_decision_engine", blocked_engine):
                with redirect_stderr(stderr), redirect_stdout(stdout):
                    code = backtest_session_edge.run(path, warmup=50)
            self.assertEqual(code, 2)
            self.assertTrue(observed_timeframes)
            self.assertTrue(all(value == "5m" for value in observed_timeframes))
            self.assertIn("Top blocked reasons", stderr.getvalue())
            self.assertIn("synthetic blocked reason", stderr.getvalue())

    def test_bootstrap_intervals_are_seeded(self):
        trades = [
            {"net": 1.0, "r": 0.5, "exit_index": 1},
            {"net": -0.5, "r": -0.25, "exit_index": 2},
            {"net": 2.0, "r": 1.0, "exit_index": 3},
        ]
        first = walk_forward_report.bootstrap_ci(trades, samples=200, seed=1234)
        second = walk_forward_report.bootstrap_ci(trades, samples=200, seed=1234)
        self.assertEqual(first, second)

    def test_walk_forward_marks_sub_30_trade_slices_insufficient(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "three-months.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["time", "open", "high", "low", "close", "tick_volume"])
                for month in (1, 2, 3):
                    start = datetime(2026, month, 1, tzinfo=timezone.utc)
                    previous = 2000.0
                    for i in range(50):
                        stamp = start + timedelta(minutes=5 * i)
                        open_price = previous
                        close = open_price + 0.1
                        writer.writerow(
                            [
                                stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                open_price,
                                open_price + 2.5,
                                open_price - 0.2,
                                close,
                                10,
                            ]
                        )
                        previous = close

            def allowed_engine(history, **kwargs):
                entry = float(history[-1].close)
                return SimpleNamespace(
                    allowed=True,
                    direction="BUY",
                    trade_params=SimpleNamespace(
                        entry_price=entry,
                        stop_loss=entry - 1,
                        take_profit=entry + 2,
                        risk_reward_ratio=2,
                    ),
                    minimum_rr=1.0,
                    blocked_reasons=[],
                )

            stdout = io.StringIO()
            with patch.object(walk_forward_report, "run_decision_engine", allowed_engine):
                with redirect_stdout(stdout):
                    code = walk_forward_report.run_report(
                        path,
                        warmup=50,
                        train_months=2,
                        test_months=1,
                        samples=100,
                        atr_multipliers=[float(walk_forward_report.SL_ATR_BASE_MULTIPLIER)],
                        tp_rrs=[float(walk_forward_report.FIXED_TP_RR)],
                    )
            self.assertEqual(code, 0)
            self.assertIn("insufficient (<30)", stdout.getvalue())
            self.assertIn("NO RELIABLE EVIDENCE", stdout.getvalue())


class HistDataConverterTests(unittest.TestCase):
    def test_m1_to_m5_uses_utc_offset_and_sums_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "input.csv"
            output = Path(tmp) / "output.csv"
            with source.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["date", "time", "o", "h", "l", "c", "v"])
                for minute in range(11):
                    open_price = 100 + minute
                    writer.writerow(
                        [
                            "2026.06.01",
                            f"00:{minute:02d}",
                            open_price,
                            open_price + 2,
                            open_price - 1,
                            open_price + 1,
                            minute + 1,
                        ]
                    )
            count, skipped = convert_histdata(
                source, output, source_offset_hours=-5
            )
            with output.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(count, 2)
            self.assertEqual(skipped, 1)
            self.assertEqual(rows[0]["time"], "2026-06-01T05:00:00Z")
            self.assertEqual(rows[0]["open"], "100")
            self.assertEqual(rows[0]["high"], "106")
            self.assertEqual(rows[0]["low"], "99")
            self.assertEqual(rows[0]["close"], "105")
            self.assertEqual(rows[0]["tick_volume"], "15")


if __name__ == "__main__":
    unittest.main()

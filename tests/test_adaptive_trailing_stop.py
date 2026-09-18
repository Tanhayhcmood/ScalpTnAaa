import unittest

from live_trading.risk.adaptive_trailing_stop import (
    AdaptiveTrailingConfig,
    compute_adaptive_trail,
    detect_exhaustion,
)


def candle(open_price, high, low, close, volume=100):
    return {
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


class AdaptiveTrailingStopTests(unittest.TestCase):
    def _exhaustion_candles(self):
        candles = []
        for _ in range(10):
            candles.append(candle(100, 102, 99, 101, 100))
        for index, body in enumerate((0.7, 0.45, 0.2)):
            start = 101 + index * body
            candles.append(candle(start, start + body, start - 0.5, start + body, 50))
        return candles

    def test_stage_one_holds_until_one_r(self):
        candles = [
            candle(100, 102, 99, 101, 100)
            for _ in range(30)
        ]
        decision = compute_adaptive_trail(
            "BUY",
            100.5,
            candles,
            AdaptiveTrailingConfig(),
            entry_price=100.0,
            initial_sl=98.0,
            current_sl=98.0,
        )
        self.assertEqual(decision.stage, "WAITING_FOR_1R")
        self.assertIsNone(decision.candidate_sl)
        self.assertLess(decision.floating_profit_r_multiple, 1.0)
        self.assertGreater(decision.atr, 0)

    def test_negative_floating_profit_never_builds_a_stop_candidate(self):
        candles = [candle(100, 101, 99, 99.5, 100) for _ in range(30)]
        decision = compute_adaptive_trail(
            "BUY",
            99.0,
            candles,
            AdaptiveTrailingConfig(),
            entry_price=100.0,
            initial_sl=98.0,
            current_sl=98.0,
        )
        self.assertEqual(decision.stage, "WAITING_FOR_1R")
        self.assertIsNone(decision.candidate_sl)
        self.assertEqual(decision.floating_profit_price, 0.0)

    def test_one_r_moves_stop_exactly_to_breakeven(self):
        candles = [candle(100, 101, 100, 100.5, 100) for _ in range(30)]
        decision = compute_adaptive_trail(
            "BUY",
            102.0,
            candles,
            AdaptiveTrailingConfig(),
            entry_price=100.0,
            initial_sl=98.0,
            current_sl=98.0,
        )
        self.assertEqual(decision.stage, "BREAKEVEN")
        self.assertEqual(decision.candidate_sl, 100.0)
        self.assertTrue(decision.breakeven_armed)
        self.assertAlmostEqual(decision.floating_profit_r_multiple, 1.0)

    def test_chandelier_uses_highest_price_since_entry(self):
        candles = [candle(100, 101, 100, 100.5, 100) for _ in range(30)]
        decision = compute_adaptive_trail(
            "BUY",
            105.0,
            candles,
            AdaptiveTrailingConfig(chandelier_atr_multiplier=2.5),
            entry_price=100.0,
            initial_sl=98.0,
            current_sl=100.0,
            highest_price_since_entry=106.0,
            breakeven_armed=True,
        )
        self.assertEqual(decision.stage, "CHANDELIER")
        self.assertEqual(decision.highest_price_since_entry, 106.0)
        self.assertAlmostEqual(decision.candidate_sl, 103.5)

    def test_chandelier_uses_lowest_price_for_sell(self):
        candles = [candle(100, 101, 100, 100.5, 100) for _ in range(30)]
        decision = compute_adaptive_trail(
            "SELL",
            95.0,
            candles,
            AdaptiveTrailingConfig(chandelier_atr_multiplier=2.5),
            entry_price=100.0,
            initial_sl=102.0,
            current_sl=100.0,
            lowest_price_since_entry=94.0,
            breakeven_armed=True,
        )
        self.assertEqual(decision.stage, "CHANDELIER")
        self.assertEqual(decision.lowest_price_since_entry, 94.0)
        self.assertAlmostEqual(decision.candidate_sl, 96.5)

    def test_risk_distance_is_derived_from_initial_sl(self):
        candles = self._exhaustion_candles()
        decision = compute_adaptive_trail(
            "BUY",
            102.0,
            candles,
            AdaptiveTrailingConfig(),
            entry_price=100.0,
            initial_sl=98.0,
            current_sl=98.0,
        )
        self.assertEqual(decision.risk_distance, 2.0)
        self.assertEqual(decision.stage, "BREAKEVEN")

    def test_legacy_zero_initial_sl_falls_back_to_saved_risk(self):
        candles = [candle(100, 101, 100, 100.5, 100) for _ in range(30)]
        decision = compute_adaptive_trail(
            "BUY",
            102.0,
            candles,
            AdaptiveTrailingConfig(),
            entry_price=100.0,
            initial_sl=0.0,
            risk_distance=2.0,
            current_sl=98.0,
        )
        self.assertEqual(decision.risk_distance, 2.0)
        self.assertEqual(decision.stage, "BREAKEVEN")

    def test_missing_volume_does_not_count_as_declining(self):
        candles = [candle(100, 102, 99, 101, 0) for _ in range(30)]
        signals = detect_exhaustion(
            candles,
            "BUY",
            AdaptiveTrailingConfig(),
        )
        self.assertFalse(signals.volume_declining)


if __name__ == "__main__":
    unittest.main()
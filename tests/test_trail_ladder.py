import unittest

from live_trading.risk.adaptive_trailing_stop import (
    AdaptiveTrailingConfig,
    compute_adaptive_trail,
    should_apply,
)


def candles():
    return [
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}
        for _ in range(30)
    ]


class TrailLadderTests(unittest.TestCase):
    def test_buy_and_sell_ladder_only_tightens(self):
        cases = (
            ("BUY", 92.0, 104.0, 106.4, 108.0, 108.0, 92.0, 1),
            ("SELL", 108.0, 96.0, 93.6, 92.0, 100.0, 92.0, -1),
        )
        cfg = AdaptiveTrailingConfig(chandelier_atr_multiplier=1.5)

        for (
            direction,
            initial_sl,
            be_price,
            lock_price,
            chandelier_price,
            highest,
            lowest,
            sign,
        ) in cases:
            with self.subTest(direction=direction):
                be = compute_adaptive_trail(
                    direction,
                    be_price,
                    candles(),
                    cfg,
                    entry_price=100.0,
                    initial_sl=initial_sl,
                    current_sl=initial_sl,
                )
                self.assertEqual(be.stage, "BREAKEVEN")
                expected_be = 100.0 + (0.2 * sign)
                self.assertAlmostEqual(be.candidate_sl, expected_be)
                self.assertTrue(
                    should_apply(direction, initial_sl, be.candidate_sl, 0.05)
                )

                lock = compute_adaptive_trail(
                    direction,
                    lock_price,
                    candles(),
                    cfg,
                    entry_price=100.0,
                    initial_sl=initial_sl,
                    current_sl=be.candidate_sl,
                )
                self.assertEqual(lock.stage, "LOCK")
                expected_lock = 100.0 + (1.6 * sign)
                self.assertAlmostEqual(lock.candidate_sl, expected_lock)
                self.assertTrue(
                    should_apply(direction, be.candidate_sl, lock.candidate_sl, 0.05)
                )

                chandelier = compute_adaptive_trail(
                    direction,
                    chandelier_price,
                    candles(),
                    cfg,
                    entry_price=100.0,
                    initial_sl=initial_sl,
                    current_sl=lock.candidate_sl,
                    highest_price_since_entry=highest,
                    lowest_price_since_entry=lowest,
                )
                self.assertEqual(chandelier.stage, "CHANDELIER")
                self.assertTrue(
                    should_apply(
                        direction,
                        lock.candidate_sl,
                        chandelier.candidate_sl,
                        0.05,
                    )
                )

                worse_candidate = chandelier.candidate_sl - (0.1 * sign)
                self.assertFalse(
                    should_apply(
                        direction,
                        chandelier.candidate_sl,
                        worse_candidate,
                        0.05,
                    )
                )


if __name__ == "__main__":
    unittest.main()

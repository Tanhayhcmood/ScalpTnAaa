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
    def test_normal_distance_uses_configured_atr_multiplier(self):
        candles = [
            candle(100, 102, 99, 101, 100)
            for _ in range(30)
        ]
        decision = compute_adaptive_trail(
            "BUY",
            101,
            candles,
            AdaptiveTrailingConfig(
                atr_period=14,
                normal_multiplier=2.0,
                exhaustion_confirm_count=3,
            ),
        )
        self.assertEqual(decision.mode, "NORMAL")
        self.assertGreater(decision.atr, 0)
        self.assertAlmostEqual(decision.distance, decision.atr * 2.0)

    def test_two_confirmations_switch_to_tightening(self):
        candles = []
        for index in range(10):
            candles.append(candle(100, 102, 99, 101, 100))
        for index, body in enumerate((0.7, 0.45, 0.2)):
            start = 101 + index * body
            candles.append(candle(start, start + body, start - 0.5, start + body, 50))
        # Momentum is directional and volume is lower; body contraction plus
        # either signal must be enough for the default 2-of-3 policy.
        signals = detect_exhaustion(
            candles,
            "BUY",
            AdaptiveTrailingConfig(momentum_lookback=3),
        )
        self.assertGreaterEqual(signals.active_count, 2)
        decision = compute_adaptive_trail("BUY", candles[-1]["close"], candles, AdaptiveTrailingConfig())
        self.assertEqual(decision.mode, "TIGHTENING")
        self.assertLess(decision.multiplier, 2.0)

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
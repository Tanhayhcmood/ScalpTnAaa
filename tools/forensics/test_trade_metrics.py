"""Unit tests for dependency-free trade forensic calculations."""
import unittest

from trade_metrics import daily_loss_metrics, trade_metrics


class TradeMetricsTests(unittest.TestCase):
    def test_buy_r_multiple(self):
        self.assertAlmostEqual(trade_metrics("BUY", 100, 95, 110)["r_multiple"], 2.0)

    def test_sell_r_multiple(self):
        self.assertAlmostEqual(trade_metrics("SELL", 100, 105, 90)["r_multiple"], 2.0)

    def test_buy_mfe_mae_in_price_and_r(self):
        result = trade_metrics("BUY", 100, 95, 101, highs=[102, 108], lows=[98, 93])
        self.assertEqual((result["mfe_price"], result["mae_price"]), (8.0, 7.0))
        self.assertAlmostEqual(result["mfe_r"], 1.6)
        self.assertAlmostEqual(result["mae_r"], 1.4)

    def test_sell_mfe_mae_in_price_and_r(self):
        result = trade_metrics("SELL", 100, 105, 99, highs=[102, 107], lows=[98, 92])
        self.assertEqual((result["mfe_price"], result["mae_price"]), (8.0, 7.0))
        self.assertAlmostEqual(result["mfe_r"], 1.6)
        self.assertAlmostEqual(result["mae_r"], 1.4)

    def test_zero_risk_is_rejected(self):
        with self.assertRaises(ValueError):
            trade_metrics("BUY", 100, 100, 105)

    def test_daily_limit_used_and_profit_floor(self):
        self.assertEqual(
            daily_loss_metrics(10_000, 9_700, 3),
            {"daily_loss_pct": 3.0, "daily_loss_limit_pct": 3.0, "daily_risk_used_pct": 100.0},
        )
        self.assertEqual(daily_loss_metrics(10_000, 10_100, 3)["daily_risk_used_pct"], 0.0)

    def test_daily_limit_used_can_exceed_100_percent(self):
        self.assertEqual(daily_loss_metrics(10_000, 9_550, 3)["daily_risk_used_pct"], 150.0)


if __name__ == "__main__":
    unittest.main()

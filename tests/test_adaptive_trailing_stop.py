import importlib
import os
import unittest
from unittest.mock import patch

from live_trading.risk.adaptive_trailing_stop import (
    AdaptiveTrailingConfig,
    compute_adaptive_trail,
    detect_exhaustion,
    should_apply,
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

    def test_one_r_moves_stop_to_breakeven(self):
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
        self.assertEqual(decision.candidate_sl, 100.2)
        self.assertTrue(decision.breakeven_armed)
        self.assertAlmostEqual(decision.floating_profit_r_multiple, 1.0)

    def test_lock_arms_at_one_and_a_half_r(self):
        candles = [candle(100, 101, 100, 100.5, 100) for _ in range(30)]
        decision = compute_adaptive_trail(
            "BUY",
            103.0,
            candles,
            AdaptiveTrailingConfig(),
            entry_price=100.0,
            initial_sl=98.0,
            current_sl=100.2,
        )
        self.assertEqual(decision.stage, "LOCK")
        self.assertEqual(decision.candidate_sl, 101.0)

    def test_xauusd_stages_match_nine_dollar_r_simulation(self):
        candles = [candle(2000, 2001, 2000, 2000.5, 100) for _ in range(30)]

        def decide(price, high=None, stage=None):
            return compute_adaptive_trail(
                "BUY",
                price,
                candles,
                AdaptiveTrailingConfig(),
                entry_price=2000.0,
                initial_sl=1991.0,
                current_sl=1991.0,
                highest_price_since_entry=high,
                highest_stage=stage,
                protective_atr=4.0,
            )

        below_be = decide(2008.99)
        be = decide(2009.0)
        lock = decide(2013.5, high=2013.5, stage=be.stage)
        before_chandelier = decide(2017.99, high=2017.99, stage=lock.stage)
        chandelier = decide(2018.0, high=2018.0, stage=before_chandelier.stage)

        self.assertIsNone(below_be.candidate_sl)
        self.assertEqual(be.stage, "BREAKEVEN")
        self.assertEqual(be.candidate_sl, 2000.2)
        self.assertEqual(lock.stage, "LOCK")
        self.assertEqual(lock.candidate_sl, 2004.5)
        self.assertEqual(before_chandelier.stage, "LOCK")
        self.assertEqual(chandelier.stage, "CHANDELIER")
        self.assertGreater(chandelier.candidate_sl, lock.candidate_sl)

    def test_chandelier_stays_at_least_point_seventy_five_r_behind_extreme(self):
        candles = [candle(2000, 2001, 2000, 2000.5, 100) for _ in range(30)]
        decision = compute_adaptive_trail(
            "BUY",
            2020.0,
            candles,
            AdaptiveTrailingConfig(chandelier_atr_multiplier=0.8),
            entry_price=2000.0,
            initial_sl=1991.0,
            current_sl=2004.5,
            highest_price_since_entry=2020.0,
            protective_atr=1.0,
        )
        self.assertEqual(decision.stage, "CHANDELIER")
        self.assertAlmostEqual(
            decision.highest_price_since_entry - decision.candidate_sl,
            0.75 * 9.0,
        )

    def test_stage_does_not_fall_back_after_profit_retraces(self):
        candles = [candle(100, 101, 100, 100.5, 100) for _ in range(30)]
        reached = compute_adaptive_trail(
            "BUY",
            104.0,
            candles,
            AdaptiveTrailingConfig(),
            entry_price=100.0,
            initial_sl=98.0,
            current_sl=101.0,
            highest_price_since_entry=104.0,
        )
        pulled_back = compute_adaptive_trail(
            "BUY",
            100.5,
            candles,
            AdaptiveTrailingConfig(),
            entry_price=100.0,
            initial_sl=98.0,
            current_sl=101.0,
            highest_price_since_entry=104.0,
            highest_stage=reached.stage,
        )
        self.assertEqual(reached.stage, "CHANDELIER")
        self.assertEqual(pulled_back.stage, "CHANDELIER")
        self.assertIsNone(pulled_back.candidate_sl)

    def test_spread_is_used_for_breakeven_and_buffer_is_fallback(self):
        candles = [candle(100, 101, 100, 100.5, 100) for _ in range(30)]
        with_spread = compute_adaptive_trail(
            "BUY", 102.0, candles, AdaptiveTrailingConfig(),
            entry_price=100.0, initial_sl=98.0, current_sl=98.0,
            spread_price=0.35,
        )
        without_spread = compute_adaptive_trail(
            "BUY", 102.0, candles, AdaptiveTrailingConfig(),
            entry_price=100.0, initial_sl=98.0, current_sl=98.0,
        )
        self.assertEqual(with_spread.candidate_sl, 100.35)
        self.assertEqual(without_spread.candidate_sl, 100.2)

    def test_identical_rounded_stop_is_never_applied(self):
        self.assertFalse(should_apply("BUY", 100.204, 100.203, 0.05))
        self.assertFalse(should_apply("SELL", 99.996, 99.995, 0.05))
        self.assertTrue(should_apply("BUY", 100.0, 100.05, 0.05))

    def test_chandelier_uses_highest_price_since_entry(self):
        candles = [candle(100, 101, 100, 100.5, 100) for _ in range(30)]
        decision = compute_adaptive_trail(
            "BUY",
            105.0,
            candles,
            AdaptiveTrailingConfig(chandelier_atr_multiplier=2.5),
            entry_price=100.0,
            initial_sl=98.0,
            current_sl=100.4,
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
            current_sl=99.6,
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
        self.assertEqual(decision.candidate_sl, 100.2)

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

    def test_restart_without_trade_history_never_uses_current_sl_for_risk(self):
        from live_trading.trading.live_loop import GoldScalperLive

        live = GoldScalperLive.__new__(GoldScalperLive)
        live.trade_history = []
        baseline = live._restore_trail_baseline({
            "id": 123,
            "type": "BUY",
            "open_price": 2000.0,
            "sl": 1999.8,
            "tp": 2027.0,
        })
        self.assertEqual(baseline["risk_distance"], 0.0)
        self.assertIsNone(baseline["initial_sl"])
        self.assertTrue(baseline["allow_chandelier_only"])
        self.assertEqual(baseline["trail_stage"], "CHANDELIER")

    def test_restart_recovers_r_from_logged_tp_and_rr(self):
        from live_trading.trading.live_loop import GoldScalperLive

        live = GoldScalperLive.__new__(GoldScalperLive)
        live.trade_history = [{
            "position_id": 123,
            "direction": "BUY",
            "entry": 2000.0,
            "tp": 2027.0,
            "rr": 3.0,
        }]
        baseline = live._restore_trail_baseline({
            "id": 123,
            "type": "BUY",
            "open_price": 2000.0,
            "sl": 1999.8,
            "tp": 2027.0,
        })
        self.assertEqual(baseline["risk_distance"], 9.0)
        self.assertEqual(baseline["initial_sl"], 1991.0)
        self.assertFalse(baseline["allow_chandelier_only"])

    def test_environment_values_reach_live_trailing_config(self):
        import live_trading.config as config_module
        import live_trading.trading.live_loop as live_loop_module

        values = {
            "TRAIL_BE_ARM_R": "1.25",
            "TRAIL_LOCK_ARM_R": "1.75",
            "TRAIL_LOCK_PROFIT_R": "0.6",
            "TRAIL_BE_BUFFER_PRICE": "0.3",
            "TRAIL_CHANDELIER_ARM_R": "2.25",
            "TRAIL_CHANDELIER_ATR_MULTIPLIER": "1.2",
            "TRAIL_MIN_DISTANCE_R": "0.9",
            "TRAIL_MIN_STEP_PRICE": "0.07",
            "TRAIL_MODIFY_COOLDOWN_S": "24",
        }
        try:
            with patch.dict(os.environ, values):
                importlib.reload(config_module)
                importlib.reload(live_loop_module)
                cfg = live_loop_module._make_adaptive_trailing_config()
                self.assertEqual(cfg.breakeven_arm_r, 1.25)
                self.assertEqual(cfg.lock_arm_r, 1.75)
                self.assertEqual(cfg.lock_profit_r, 0.6)
                self.assertEqual(cfg.breakeven_buffer_price, 0.3)
                self.assertEqual(cfg.chandelier_arm_r, 2.25)
                self.assertEqual(cfg.chandelier_atr_multiplier, 1.2)
                self.assertEqual(cfg.min_distance_r, 0.9)
                self.assertEqual(cfg.min_step_price, 0.07)
                self.assertEqual(cfg.modify_cooldown_s, 24.0)
                self.assertEqual(
                    live_loop_module.TRAIL_MODIFY_COOLDOWN_S, 24.0
                )
        finally:
            importlib.reload(config_module)
            importlib.reload(live_loop_module)

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
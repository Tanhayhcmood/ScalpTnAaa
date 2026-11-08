from datetime import datetime, timedelta, timezone
import importlib
import os

from live_trading.risk import capital_manager
from live_trading.signals.decision_engine import _rr_requirements
from live_trading.trading.loss_streak import evaluate_loss_streak_pause


def test_tp_rr_default_is_identical_to_legacy_two_r(monkeypatch):
    original = os.environ.get("TP_RR")
    try:
        monkeypatch.delenv("TP_RR", raising=False)
        manager = importlib.reload(capital_manager)
        assert manager.FIXED_TP_RR == 2.0

        defaults = {
            "direction": "BUY",
            "entry_price": 2500.0,
            "atr": 10.0,
            "account_balance": 10_000.0,
            "sl_atr_multiplier": 2.0,
            "sl_min_atr_multiplier": 1.5,
        }
        default_params = manager.calc_trade_parameters(
            manager.CapitalInput(**defaults)
        )
        explicit_params = manager.calc_trade_parameters(
            manager.CapitalInput(**defaults, take_profit_rr=2.0)
        )
        assert default_params == explicit_params
    finally:
        if original is None:
            monkeypatch.delenv("TP_RR", raising=False)
        else:
            monkeypatch.setenv("TP_RR", original)
        importlib.reload(capital_manager)


def test_tp_rr_environment_value_sets_default_target(monkeypatch):
    original = os.environ.get("TP_RR")
    try:
        monkeypatch.setenv("TP_RR", "1.25")
        manager = importlib.reload(capital_manager)
        assert manager.FIXED_TP_RR == 1.25
        params = manager.calc_trade_parameters(
            manager.CapitalInput(
                direction="BUY",
                entry_price=2500.0,
                atr=10.0,
                account_balance=10_000.0,
                sl_atr_multiplier=2.0,
                sl_min_atr_multiplier=1.5,
            )
        )
        assert params.risk_reward_ratio == 1.25
    finally:
        if original is None:
            monkeypatch.delenv("TP_RR", raising=False)
        else:
            monkeypatch.setenv("TP_RR", original)
        importlib.reload(capital_manager)


def test_rr_thresholds_cap_only_non_range_when_tp_rr_is_lower():
    assert _rr_requirements("TREND", 1.8, 1.5, 1.2) == (1.2, 1.2)
    assert _rr_requirements("TREND", 1.5, 1.5, 1.8) == (1.5, 1.3)
    assert _rr_requirements("TREND", 1.8, 1.5, 2.0) == (1.8, 1.3)
    assert _rr_requirements("RANGE", 2.0, 1.5, 0.8) == (1.5, 1.3)


def _closed_trade(position_id, profit, close_time):
    return {
        "position_id": str(position_id),
        "entry": 100.0,
        "lot": 0.01,
        "status": "CLOSED",
        "profit": profit,
        "close_time": close_time.isoformat(),
    }


def test_loss_streak_threshold_starts_pause_from_latest_loss():
    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    history = [
        _closed_trade(i, -1.0, now - timedelta(minutes=20 - i * 5))
        for i in range(1, 4)
    ]

    result = evaluate_loss_streak_pause(
        history, pause_count=3, pause_minutes=60, now_utc=now
    )

    assert result.consecutive_losses == 3
    assert result.active
    assert result.pause_until == now + timedelta(minutes=55)
    assert "3 consecutive losing" in result.reason


def test_win_resets_consecutive_loss_streak():
    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    history = [
        _closed_trade(1, -1.0, now - timedelta(minutes=20)),
        _closed_trade(2, -1.0, now - timedelta(minutes=15)),
        _closed_trade(3, 0.0, now - timedelta(minutes=10)),
        _closed_trade(4, -1.0, now - timedelta(minutes=5)),
    ]

    result = evaluate_loss_streak_pause(
        history, pause_count=3, pause_minutes=60, now_utc=now
    )

    assert result.consecutive_losses == 1
    assert not result.active
    assert result.pause_until is None


def test_open_trade_does_not_count_and_expired_pause_allows_entries():
    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    history = [
        _closed_trade(1, -1.0, now - timedelta(minutes=180)),
        _closed_trade(2, -1.0, now - timedelta(minutes=120)),
        _closed_trade(3, -1.0, now - timedelta(minutes=61)),
        {
            "position_id": "open",
            "entry": 100.0,
            "lot": 0.01,
            "status": "OPEN",
            "profit": -20.0,
        },
    ]

    result = evaluate_loss_streak_pause(
        history, pause_count=3, pause_minutes=60, now_utc=now
    )

    assert result.consecutive_losses == 3
    assert not result.active
    assert result.pause_until == now - timedelta(minutes=1)

from datetime import datetime, timezone

from live_trading.risk.guardian import RiskGuardian, account_is_demo


def test_reset_halt_can_reset_daily_baseline_without_resetting_equity_peak(tmp_path):
    guardian = RiskGuardian(4.0, 12.0)
    guardian.initialize(1000.0, 1100.0)
    guardian._trigger("DAILY LOSS LIMIT")

    assert guardian.reset_halt(
        reset_daily_baseline=True,
        current_balance=950.0,
    )
    assert guardian.is_halted is False
    assert guardian._day_open_balance == 950.0
    assert guardian._equity_peak == 1100.0
    assert guardian._last_day == datetime.now(timezone.utc).date()


def test_daily_baseline_reset_requires_positive_balance():
    guardian = RiskGuardian(4.0, 12.0)
    guardian.initialize(1000.0, 1000.0)
    guardian._trigger("DAILY LOSS LIMIT")

    assert guardian.reset_halt(
        reset_daily_baseline=True,
        current_balance=0.0,
    ) is False
    assert guardian.is_halted is True


def test_demo_reset_can_rebase_daily_loss_and_drawdown():
    guardian = RiskGuardian(4.0, 12.0)
    guardian._save_state = lambda: None
    guardian.initialize(1000.0, 1100.0)
    guardian._trigger("MAX DRAWDOWN STOP")

    assert guardian.reset_halt(
        reset_daily_baseline=True,
        current_balance=900.0,
        reset_equity_peak=True,
        current_equity=875.0,
    )
    assert guardian.is_halted is False
    assert guardian._day_open_balance == 900.0
    assert guardian._equity_peak == 875.0


def test_equity_reset_requires_positive_equity_without_partial_mutation():
    guardian = RiskGuardian(4.0, 12.0)
    guardian._save_state = lambda: None
    guardian.initialize(1000.0, 1100.0)
    guardian._trigger("MAX DRAWDOWN STOP")

    assert guardian.reset_halt(
        reset_daily_baseline=True,
        current_balance=950.0,
        reset_equity_peak=True,
        current_equity=0.0,
    ) is False
    assert guardian._day_open_balance == 1000.0
    assert guardian._equity_peak == 1100.0
    assert guardian.is_halted is True


def test_demo_detection_fails_closed_for_real_and_unknown_accounts():
    assert account_is_demo({"server": "AMarkets-Demo"}) is True
    assert account_is_demo({"trade_mode": "REAL"}) is False
    assert account_is_demo({"is_demo": 0}, allow_env_override=True) is False
    assert account_is_demo({"trade_mode": "CONTEST"}, allow_env_override=True) is False
    assert account_is_demo(
        {"is_demo": True, "trade_mode": "REAL"}, allow_env_override=True
    ) is False
    assert account_is_demo({"server": "Broker-Real"}, allow_env_override=True) is False
    assert account_is_demo({"server": "Broker-Unknown"}) is False
    assert account_is_demo({"server": "Broker-Unknown"}, allow_env_override=True) is True
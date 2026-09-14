from datetime import datetime, timezone

from live_trading.risk.guardian import RiskGuardian


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
from datetime import datetime, timedelta, timezone
import json

import live_trading.risk.guardian as guardian_module
from live_trading.risk.guardian import RiskGuardian


def _daily_loss_halt(guardian, start=10_000.0, current=9_502.0):
    guardian.initialize(start, start)
    status = guardian.check(current, current)
    assert status.halted is True
    assert "DAILY LOSS LIMIT" in status.reason
    return status


def test_manual_reset_rebases_daily_loss_and_new_losses_still_halt():
    guardian = RiskGuardian(3.0, 8.0)
    _daily_loss_halt(guardian)

    assert guardian.reset_halt(
        current_balance=9_502.0, current_equity=9_502.0, request_id="reset-1"
    )
    baseline = guardian.manual_reset_baseline
    assert baseline["realized_day_pnl"] == -498.0
    assert baseline["balance"] == 9_502.0
    assert baseline["equity"] == baseline["equity_peak"] == 9_502.0
    assert baseline["reset_at"]
    assert guardian._daily_loss_limit_pct == 3.0

    after_reset = guardian.check(9_502.0, 9_502.0)
    assert after_reset.halted is False
    assert after_reset.daily_pnl == 0.0
    assert after_reset.daily_pnl_pct == 0.0
    assert after_reset.drawdown_pct == 0.0

    # A further 3% loss from reset-time balance still trips the unchanged limit.
    after_new_loss = guardian.check(9_502.0 * 0.97, 9_502.0 * 0.97)
    assert after_new_loss.halted is True
    assert "DAILY LOSS LIMIT" in after_new_loss.reason
    assert abs(after_new_loss.daily_pnl_pct + 3.0) < 1e-8


def test_manual_reset_rebases_peak_drawdown_and_new_drawdown_still_halts():
    guardian = RiskGuardian(3.0, 8.0)
    guardian.initialize(10_000.0, 10_000.0)
    assert guardian.check(10_000.0, 9_000.0).halted is True

    assert guardian.reset_halt(
        current_balance=10_000.0, current_equity=9_000.0, request_id="reset-dd"
    )
    assert guardian._equity_peak == 9_000.0
    assert guardian.check(10_000.0, 9_000.0).halted is False

    after_new_drawdown = guardian.check(10_000.0, 9_000.0 * 0.92)
    assert after_new_drawdown.halted is True
    assert "MAX DRAWDOWN STOP" in after_new_drawdown.reason
    assert guardian._max_drawdown_pct == 8.0


def test_reset_while_running_is_noop_and_duplicate_request_cannot_clear_new_halt():
    guardian = RiskGuardian(3.0, 8.0)
    guardian.initialize(10_000.0, 10_000.0)
    original_peak = guardian._equity_peak
    assert guardian.reset_halt(
        current_balance=9_502.0, current_equity=9_502.0, request_id="not-halted"
    ) is False
    assert guardian.manual_reset_baseline is None
    assert guardian._equity_peak == original_peak
    assert guardian.is_halted is False

    _daily_loss_halt(guardian)
    assert guardian.reset_halt(
        current_balance=9_502.0, current_equity=9_502.0, request_id="once"
    )
    assert guardian.check(9_502.0 * 0.97, 9_502.0 * 0.97).halted is True
    saved_baseline = guardian.manual_reset_baseline
    assert guardian.reset_halt(
        current_balance=9_217.0, current_equity=9_217.0, request_id="once"
    ) is False
    assert guardian.is_halted is True
    assert guardian.manual_reset_baseline == saved_baseline


def test_invalid_live_snapshot_does_not_clear_halt():
    guardian = RiskGuardian(3.0, 8.0)
    _daily_loss_halt(guardian)

    assert guardian.reset_halt(
        current_balance=0.0, current_equity=9_502.0, request_id="invalid"
    ) is False
    assert guardian.is_halted is True
    assert guardian.manual_reset_baseline is None
    assert guardian.reset_halt(
        current_balance="bad", current_equity=9_502.0, request_id="invalid-2"
    ) is False
    assert guardian.is_halted is True


def test_manual_reset_baseline_survives_restore_and_remains_effective(tmp_path):
    old_path = guardian_module._GUARDIAN_STATE_FILE
    guardian_module._GUARDIAN_STATE_FILE = str(tmp_path / "guardian-state.json")
    try:
        guardian = RiskGuardian(3.0, 8.0)
        _daily_loss_halt(guardian)
        assert guardian.reset_halt(
            current_balance=9_502.0, current_equity=9_502.0, request_id="persisted"
        )
        saved = json.loads((tmp_path / "guardian-state.json").read_text())
        assert saved["manual_reset_baseline"]["realized_day_pnl"] == -498.0

        restored = RiskGuardian(3.0, 8.0)
        assert restored.restore_state(saved) is True
        assert restored.manual_reset_baseline["request_id"] == "persisted"
        assert restored.check(9_502.0, 9_502.0).halted is False
        assert restored.check(9_502.0 * 0.97, 9_502.0 * 0.97).halted is True
    finally:
        guardian_module._GUARDIAN_STATE_FILE = old_path


def test_manual_reset_baseline_expires_on_new_utc_day():
    guardian = RiskGuardian(3.0, 8.0)
    _daily_loss_halt(guardian)
    assert guardian.reset_halt(
        current_balance=9_502.0, current_equity=9_502.0, request_id="day-change"
    )

    guardian._last_day = datetime.now(timezone.utc).date() - timedelta(days=1)
    new_day = guardian.check(9_502.0, 9_502.0)
    assert guardian.manual_reset_baseline is None
    assert guardian._day_open_balance == 9_502.0
    assert new_day.halted is False
    assert new_day.daily_pnl == 0.0
    assert guardian.check(9_502.0 * 0.97, 9_502.0 * 0.97).halted is True

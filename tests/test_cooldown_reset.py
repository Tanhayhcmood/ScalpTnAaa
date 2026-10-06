import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from live_trading.risk.guardian import RiskGuardian
from live_trading.trading import live_loop as live_loop_module
from live_trading.trading.live_loop import GoldScalperLive
from telegram_panel.api.handlers.dashboard import _cooldown_reset_admin_allowed
from telegram_panel.config.constants import BotRole


def _engine():
    engine = GoldScalperLive.__new__(GoldScalperLive)
    engine._cooldown_reset_at = None
    engine._cooldown_untils_by_direction = {}
    engine._cooldown_untils_by_level = {}
    engine._consecutive_loss_count = 0
    engine._last_entry_bar_time = None
    engine._last_entry_direction = ""
    engine._last_acc_info = {}
    engine.trade_history = []
    engine.guardian = RiskGuardian(3.0, 8.0)
    engine.paused = True
    engine._last_trade_permission = {}
    engine._write_state = lambda *args, **kwargs: None
    return engine


def test_real_account_reset_clears_cooldowns_but_preserves_guardian_lock():
    engine = _engine()
    engine.guardian._save_state = lambda: None
    engine.guardian.initialize(1000.0, 1000.0)
    engine.guardian._trigger("DAILY LOSS LIMIT")
    engine._cooldown_untils_by_direction = {
        "BUY": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    }
    engine._cooldown_untils_by_level = {"slot-1": "2099-01-01T00:00:00+00:00"}
    engine._consecutive_loss_count = 3
    engine._last_entry_direction = "BUY"

    engine._reset_cooldowns_and_resume(
        "telegram_id=77",
        {
            "server": "Broker-Real",
            "balance": 800.0,
            "equity": 800.0,
            "is_demo": False,
        },
    )

    assert engine._cooldown_untils_by_direction == {}
    assert engine._cooldown_untils_by_level == {}
    assert engine._consecutive_loss_count == 0
    assert engine._last_entry_direction == ""
    assert engine.guardian.is_halted is True
    assert engine.paused is True


def test_demo_account_reset_clears_guardian_and_persists_cleared_state():
    engine = _engine()
    engine.guardian._save_state = lambda: None
    engine.guardian.initialize(1000.0, 1000.0)
    engine.guardian._trigger("MAX DRAWDOWN STOP")
    engine._cooldown_untils_by_direction = {"SELL": "2099-01-01T00:00:00+00:00"}
    engine._cooldown_untils_by_level = {"slot-2": "2099-01-01T00:00:00+00:00"}
    engine._consecutive_loss_count = 2

    engine._reset_cooldowns_and_resume(
        "telegram_id=77",
        {
            "server": "AMarkets-Demo",
            "balance": 900.0,
            "equity": 875.0,
            "is_demo": True,
        },
    )

    assert engine.guardian.is_halted is False
    assert engine.guardian._day_open_balance == 900.0
    assert engine.guardian._equity_peak == 875.0
    assert engine.paused is False

    stored = json.loads(json.dumps({"entry_cooldown": engine._cooldown_state()}))
    restored = _engine()
    restored._restore_cooldown_state(stored)
    assert restored._cooldown_reset_at == engine._cooldown_reset_at
    assert restored._cooldown_untils_by_direction == {}
    assert restored._cooldown_untils_by_level == {}
    assert restored._consecutive_loss_count == 0


def test_stop_loss_history_rebuilds_direction_and_level_blocks():
    engine = _engine()
    engine.trade_history = [{
        "status": "CLOSED",
        "close_time": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),
        "close_reason": "SL",
        "profit": -10.0,
        "direction": "BUY",
        "strategy_slots": ["slot-1"],
    }]

    engine._refresh_cooldown_controls()

    assert "BUY" in engine._cooldown_untils_by_direction
    assert "slot-1" in engine._cooldown_untils_by_level
    assert engine._consecutive_loss_count == 1


def test_cooldown_disabled_clears_stop_blocks_but_tracks_loss_streak(monkeypatch):
    engine = _engine()
    engine.trade_history = [{
        "status": "CLOSED",
        "close_time": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        "close_reason": "SL",
        "profit": -10.0,
        "direction": "SELL",
        "strategy_slots": ["slot-2"],
    }]
    monkeypatch.setattr(live_loop_module, "COOLDOWN_ENABLED", False)

    engine._refresh_cooldown_controls()

    assert engine._cooldown_untils_by_direction == {}
    assert engine._cooldown_untils_by_level == {}
    assert engine._consecutive_loss_count == 1


def test_robot_state_writer_persists_cooldown_reset_marker(monkeypatch):
    engine = _engine()
    engine._last_guardian_status = None
    engine._trail_baselines = {}
    engine._last_trailing_statuses = {}
    engine._last_candle_telemetry = {}
    engine._history_sync_status = {}
    engine.last_decision = None
    engine._last_bar_times = {}
    engine.loop_count = 0
    engine._last_open_positions = []
    engine._cooldown_reset_at = datetime.now(timezone.utc).isoformat()
    written = {}
    monkeypatch.setattr(
        live_loop_module,
        "write_robot_state",
        lambda **kwargs: written.update(kwargs),
    )

    GoldScalperLive._write_state(engine, "RUNNING")

    assert written["extra"]["entry_cooldown"]["reset_at"] == engine._cooldown_reset_at


def test_restored_active_cooldown_survives_when_history_is_trimmed():
    engine = _engine()
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    engine._restore_cooldown_state({
        "entry_cooldown": {
            "by_direction": {"BUY": future},
            "by_level": {"slot-3": future},
            "consecutive_loss_count": 2,
        }
    })
    engine._refresh_cooldown_controls()

    assert engine._cooldown_untils_by_direction["BUY"] == future
    assert engine._cooldown_untils_by_level["slot-3"] == future
    assert engine._consecutive_loss_count == 2


def test_cooldown_reset_command_authorization_requires_admin_role():
    admin = SimpleNamespace(
        role=BotRole.ADMIN,
        permissions=SimpleNamespace(can_control_robot=True),
    )
    owner = SimpleNamespace(
        role=BotRole.OWNER,
        permissions=SimpleNamespace(can_control_robot=True),
    )
    viewer = SimpleNamespace(
        role=BotRole.VIEWER,
        permissions=SimpleNamespace(can_control_robot=True),
    )
    admin_without_control = SimpleNamespace(
        role=BotRole.ADMIN,
        permissions=SimpleNamespace(can_control_robot=False),
    )

    assert _cooldown_reset_admin_allowed(admin) is True
    assert _cooldown_reset_admin_allowed(owner) is True
    assert _cooldown_reset_admin_allowed(viewer) is False
    assert _cooldown_reset_admin_allowed(admin_without_control) is False

"""
Regression tests for the 3-bug connection-status fix.

Bug 1 (state_writer): connection_status was derived from robot loop state
       (PAUSED/WAITING/etc.) instead of actual MT5 connector state.
Bug 2 (redis_ipc + state_writer): RECONNECT command was not in the panel
       command map so clicking Reconnect in the panel did nothing.
Bug 3 (mt5_service): _normalize_state_to_snapshot treated PAUSED as
       "connected" and ignored the explicit connection_status field.
"""
import json
import os
import sys
import tempfile
from unittest.mock import patch

import pytest

# ─── helpers ──────────────────────────────────────────────────────────────────

def _make_state_writer_call(status: str, connector_connected: bool) -> dict:
    """
    Call write_robot_state with a mocked connector.is_connected() and return
    the dict that would be written to the state file.
    """
    from live_trading.utils import state_writer as sw

    written: list[dict] = []

    def _capture_write(path: str, data: dict) -> None:
        written.append(data)

    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = os.path.join(tmpdir, "state.json")
        with (
            patch.object(sw, "STATE_FILE", state_path),
            patch("live_trading.utils.state_writer._safe_write", side_effect=_capture_write),
            patch(
                "live_trading.mt5.connector.is_connected",
                return_value=connector_connected,
            ),
        ):
            sw.write_robot_state(
                status=status,
                decision=None,
                open_position=None,
                account_info={"balance": 300.0, "equity": 300.0},
                trade_history=[],
                loop_count=1,
            )

    assert written, "write_robot_state did not call _safe_write"
    return written[0]


# ─── Bug 1: state_writer uses actual connector state ──────────────────────────

class TestStateWriterConnectionStatus:

    def test_paused_robot_with_disconnected_bridge_shows_disconnected(self):
        """PAUSED + bridge offline → connection_status must be 'disconnected'."""
        state = _make_state_writer_call("PAUSED", connector_connected=False)
        assert state["connection_status"] == "disconnected", (
            f"Expected 'disconnected' but got '{state['connection_status']}'. "
            "Bug 1 regression: PAUSED robot should not show connected when "
            "the mt5rest bridge is unreachable."
        )
        assert state["mt5_status"] == "disconnected"
        # account_info sub-dict must also reflect disconnected
        assert state["account_info"]["connection_status"] == "disconnected"

    def test_paused_robot_with_connected_bridge_shows_connected(self):
        """PAUSED + bridge online → connection_status must be 'connected'."""
        state = _make_state_writer_call("PAUSED", connector_connected=True)
        assert state["connection_status"] == "connected"

    def test_waiting_with_disconnected_bridge_shows_disconnected(self):
        """WAITING + bridge offline → must be 'disconnected' (was 'connected' before fix)."""
        state = _make_state_writer_call("WAITING", connector_connected=False)
        assert state["connection_status"] == "disconnected"

    def test_running_with_connected_bridge_shows_connected(self):
        """Normal RUNNING + bridge online → 'connected'."""
        state = _make_state_writer_call("RUNNING", connector_connected=True)
        assert state["connection_status"] == "connected"

    def test_disconnected_status_always_disconnected(self):
        """DISCONNECTED status regardless of connector mock → 'disconnected'."""
        # connector returns False when disconnected anyway, but belt-and-braces
        state = _make_state_writer_call("DISCONNECTED", connector_connected=False)
        assert state["connection_status"] == "disconnected"

    def test_fallback_when_connector_import_fails(self):
        """If connector import fails, fallback logic must not crash and must
        produce a sensible value (disconnected for PAUSED, connected for RUNNING)."""
        from live_trading.utils import state_writer as sw

        written: list[dict] = []

        def _capture(path, data):
            written.append(data)

        with patch("live_trading.utils.state_writer._safe_write", side_effect=_capture):
            # Simulate import error by patching the module to raise
            with patch.dict("sys.modules", {"live_trading.mt5.connector": None}):
                sw.write_robot_state(
                    status="RUNNING",
                    decision=None,
                    open_position=None,
                    account_info={"balance": 300.0, "equity": 300.0},
                    trade_history=[],
                    loop_count=1,
                )

        # Fallback: RUNNING → connected
        assert written[0]["connection_status"] == "connected"


# ─── Bug 2: RECONNECT command is mapped in both command maps ──────────────────

class TestReconnectCommandMapping:

    def test_panel_redis_ipc_maps_reconnect_to_restart_mt5(self):
        """redis_ipc._PANEL_COMMAND_MAP must translate RECONNECT → restart_mt5."""
        from telegram_panel.redis_ipc import _PANEL_COMMAND_MAP
        assert "RECONNECT" in _PANEL_COMMAND_MAP, (
            "RECONNECT missing from telegram_panel.redis_ipc._PANEL_COMMAND_MAP. "
            "Clicking Reconnect in the Telegram panel sends nothing to the robot."
        )
        assert _PANEL_COMMAND_MAP["RECONNECT"] == "restart_mt5", (
            f"RECONNECT should map to 'restart_mt5', got '{_PANEL_COMMAND_MAP['RECONNECT']}'"
        )

    def test_robot_state_writer_maps_reconnect_to_restart_mt5(self):
        """state_writer._PANEL_COMMAND_MAP (file-based fallback) must also map RECONNECT."""
        from live_trading.utils.state_writer import _PANEL_COMMAND_MAP
        assert "RECONNECT" in _PANEL_COMMAND_MAP, (
            "RECONNECT missing from live_trading.utils.state_writer._PANEL_COMMAND_MAP. "
            "File-based fallback path ignores the Reconnect command."
        )
        assert _PANEL_COMMAND_MAP["RECONNECT"] == "restart_mt5"

    def test_redis_ipc_sends_reconnect_as_restart_mt5(self):
        """redis_send_command('RECONNECT', {}) must write 'restart_mt5: True' to Redis."""
        from telegram_panel import redis_ipc

        written: dict = {}

        class FakeRedis:
            def get(self, key): return json.dumps(written.copy()) if written else None
            def set(self, key, val, ex=None): written.update(json.loads(val))
            def ping(self): return True

        # Patch _get_client directly so REDIS_URL='' in the test env doesn't abort early.
        with patch.object(redis_ipc, "_get_client", return_value=FakeRedis()):
            ok = redis_ipc.redis_send_command("RECONNECT", {})

        assert ok is True
        assert written.get("restart_mt5") is True, (
            f"Expected 'restart_mt5: True' in Redis command dict, got: {written}"
        )


# ─── Bug 3: mt5_service._normalize_state_to_snapshot ──────────────────────────

class TestNormalizeStateToSnapshot:

    def _normalize(self, state: dict) -> dict:
        from telegram_panel.services.mt5_service import MT5Service
        return MT5Service._normalize_state_to_snapshot(state)

    def test_paused_without_explicit_field_shows_disconnected(self):
        """PAUSED robot state without explicit connection_status → 'disconnected'."""
        snap = self._normalize({"status": "PAUSED", "account": {}})
        assert snap["connection_status"] == "disconnected", (
            "Bug 3 regression: PAUSED status should not show 'connected' "
            "when no explicit connection_status field is present."
        )
        assert snap["account_info"]["connection_status"] == "disconnected"

    def test_explicit_connected_field_wins_over_paused_status(self):
        """Explicit 'connected' field must win even when robot status is PAUSED."""
        snap = self._normalize({
            "status": "PAUSED",
            "connection_status": "connected",
            "account": {},
        })
        assert snap["connection_status"] == "connected"

    def test_explicit_disconnected_field_wins_over_running_status(self):
        """Explicit 'disconnected' field must win even when robot status is RUNNING."""
        snap = self._normalize({
            "status": "RUNNING",
            "connection_status": "disconnected",
            "account": {},
        })
        assert snap["connection_status"] == "disconnected"

    def test_running_without_explicit_field_shows_connected(self):
        """RUNNING without explicit field → 'connected' (normal case)."""
        snap = self._normalize({"status": "RUNNING", "account": {}})
        assert snap["connection_status"] == "connected"

    def test_stopped_without_explicit_field_shows_disconnected(self):
        snap = self._normalize({"status": "STOPPED", "account": {}})
        assert snap["connection_status"] == "disconnected"

    def test_disconnected_status_shows_disconnected(self):
        snap = self._normalize({"status": "DISCONNECTED", "account": {}})
        assert snap["connection_status"] == "disconnected"

    def test_waiting_without_explicit_field_shows_connected(self):
        snap = self._normalize({"status": "WAITING", "account": {}})
        assert snap["connection_status"] == "connected"

    def test_scanning_without_explicit_field_shows_connected(self):
        snap = self._normalize({"status": "SCANNING", "account": {}})
        assert snap["connection_status"] == "connected"

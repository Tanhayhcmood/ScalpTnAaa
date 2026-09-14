"""
State Persistence Tests — live_trading/utils/state_writer.py
============================================================
Tests that state files are written correctly, read back correctly,
and are robust to corruption.

These tests verify ENGINEERING correctness only.
No strategy logic, no signal computation, no trading behaviour is tested.
"""
import json
import os
import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch


class TestSafeWrite:
    """Test atomic write behaviour of _safe_write."""

    def test_write_creates_file(self, tmp_path):
        state_file = str(tmp_path / "robot_state.json")
        data = {"status": "RUNNING", "loop_count": 1}
        from live_trading.utils.state_writer import _safe_write
        _safe_write(state_file, data)
        assert Path(state_file).exists()

    def test_write_produces_valid_json(self, tmp_path):
        state_file = str(tmp_path / "robot_state.json")
        data = {"status": "RUNNING", "loop_count": 42, "value": 1.5}
        from live_trading.utils.state_writer import _safe_write
        _safe_write(state_file, data)
        with open(state_file, "r") as f:
            loaded = json.load(f)
        assert loaded["status"] == "RUNNING"
        assert loaded["loop_count"] == 42

    def test_write_overwrites_existing(self, tmp_path):
        state_file = str(tmp_path / "robot_state.json")
        from live_trading.utils.state_writer import _safe_write
        _safe_write(state_file, {"status": "RUNNING"})
        _safe_write(state_file, {"status": "STOPPED"})
        with open(state_file, "r") as f:
            loaded = json.load(f)
        assert loaded["status"] == "STOPPED"

    def test_no_tmp_file_left_after_write(self, tmp_path):
        state_file = str(tmp_path / "robot_state.json")
        from live_trading.utils.state_writer import _safe_write
        _safe_write(state_file, {"status": "RUNNING"})
        assert not Path(state_file + ".tmp").exists()


class TestReadCommands:
    """Test read_commands() robustness to corruption and missing files."""

    def test_read_nonexistent_file_returns_empty(self, tmp_path):
        nonexistent = str(tmp_path / "missing_commands.json")
        with patch("live_trading.utils.state_writer.COMMANDS_FILE", nonexistent):
            from live_trading.utils import state_writer
            result = state_writer.read_commands()
        assert result == {}

    def test_read_valid_commands(self, tmp_path):
        cmd_file = str(tmp_path / "robot_commands.json")
        with open(cmd_file, "w") as f:
            json.dump({"pause": True}, f)
        with patch("live_trading.utils.state_writer.COMMANDS_FILE", cmd_file):
            from live_trading.utils import state_writer
            result = state_writer.read_commands()
        assert result.get("pause") is True

    def test_read_corrupted_file_returns_empty(self, tmp_path):
        cmd_file = str(tmp_path / "robot_commands.json")
        with open(cmd_file, "w") as f:
            f.write("{not valid json{{")
        with patch("live_trading.utils.state_writer.COMMANDS_FILE", cmd_file):
            from live_trading.utils import state_writer
            result = state_writer.read_commands()
        assert result == {}

    def test_read_empty_file_returns_empty(self, tmp_path):
        cmd_file = str(tmp_path / "robot_commands.json")
        with open(cmd_file, "w") as f:
            f.write("")
        with patch("live_trading.utils.state_writer.COMMANDS_FILE", cmd_file):
            from live_trading.utils import state_writer
            result = state_writer.read_commands()
        assert result == {}


class TestStateFileSerialization:
    """Test that state dict serializes and deserializes without data loss."""

    def test_round_trip_basic(self, tmp_path):
        state_file = str(tmp_path / "robot_state.json")
        from live_trading.utils.state_writer import _safe_write
        payload = {
            "status": "RUNNING",
            "loop_count": 100,
            "balance": 10250.75,
            "recent_trades": [
                {"position_id": "abc", "direction": "BUY", "confidence": 82.5}
            ],
            "guardian": {
                "halted": False,
                "daily_pnl": 42.0,
                "drawdown_pct": 1.2,
            }
        }
        _safe_write(state_file, payload)
        with open(state_file, "r") as f:
            loaded = json.load(f)
        assert loaded["status"] == "RUNNING"
        assert loaded["loop_count"] == 100
        assert abs(loaded["balance"] - 10250.75) < 0.001
        assert len(loaded["recent_trades"]) == 1
        assert loaded["recent_trades"][0]["direction"] == "BUY"
        assert loaded["guardian"]["halted"] is False

    def test_none_values_serialized_as_null(self, tmp_path):
        state_file = str(tmp_path / "robot_state.json")
        from live_trading.utils.state_writer import _safe_write
        _safe_write(state_file, {"position": None, "error": None})
        with open(state_file, "r") as f:
            loaded = json.load(f)
        assert loaded["position"] is None
        assert loaded["error"] is None

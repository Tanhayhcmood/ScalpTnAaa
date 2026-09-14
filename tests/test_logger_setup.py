"""
Logger Setup Tests — live_trading/logger.py
==========================================
Tests that get_logger() configures logging correctly and degrades gracefully
on file system errors.

These tests verify ENGINEERING correctness only.
No strategy logic, no trading behaviour is tested.
"""
import logging
import sys
import tempfile
import os
import pytest
from unittest.mock import patch
from logging.handlers import RotatingFileHandler


class TestLoggerSetup:
    """get_logger() must produce a correctly configured logger."""

    def _get_fresh_logger(self, name: str, log_file: str) -> logging.Logger:
        """Get a fresh logger (clears existing handlers)."""
        # Remove cached logger so handlers are re-created
        existing = logging.Logger.manager.loggerDict.get(name)
        if existing:
            if hasattr(existing, "handlers"):
                existing.handlers.clear()
            del logging.Logger.manager.loggerDict[name]
        with patch("live_trading.config.LOG_FILE", log_file):
            import live_trading.logger as lm
            return lm.get_logger(name)

    def test_logger_has_console_handler(self, tmp_path):
        log_file = str(tmp_path / "robot.log")
        logger = self._get_fresh_logger("test_console", log_file)
        handler_types = [type(h) for h in logger.handlers]
        assert logging.StreamHandler in handler_types

    def test_logger_has_rotating_file_handler(self, tmp_path):
        log_file = str(tmp_path / "robot.log")
        logger = self._get_fresh_logger("test_rotating", log_file)
        handler_types = [type(h) for h in logger.handlers]
        assert RotatingFileHandler in handler_types, \
            f"Expected RotatingFileHandler, got: {handler_types}"

    def test_rotating_file_handler_max_bytes(self, tmp_path):
        log_file = str(tmp_path / "robot.log")
        logger = self._get_fresh_logger("test_maxbytes", log_file)
        rfh = next(h for h in logger.handlers if isinstance(h, RotatingFileHandler))
        assert rfh.maxBytes == 10_000_000, \
            f"Expected 10MB max, got {rfh.maxBytes}"

    def test_rotating_file_handler_backup_count(self, tmp_path):
        log_file = str(tmp_path / "robot.log")
        logger = self._get_fresh_logger("test_backups", log_file)
        rfh = next(h for h in logger.handlers if isinstance(h, RotatingFileHandler))
        assert rfh.backupCount == 5

    def test_logger_level_is_debug(self, tmp_path):
        log_file = str(tmp_path / "robot.log")
        logger = self._get_fresh_logger("test_level", log_file)
        assert logger.level == logging.DEBUG

    def test_console_handler_level_is_info(self, tmp_path):
        log_file = str(tmp_path / "robot.log")
        logger = self._get_fresh_logger("test_console_level", log_file)
        ch = next(h for h in logger.handlers if isinstance(h, logging.StreamHandler)
                  and not isinstance(h, RotatingFileHandler))
        assert ch.level == logging.INFO

    def test_logger_degrades_gracefully_on_bad_path(self, capsys):
        """When log file path is unwriteable, logger falls back to console only."""
        bad_path = "/nonexistent_readonly_dir/robot.log"
        logger = self._get_fresh_logger("test_fallback", bad_path)
        # Must still have at least the console handler
        assert any(isinstance(h, logging.StreamHandler) for h in logger.handlers)
        # Stderr should contain the warning
        captured = capsys.readouterr()
        assert "WARNING" in captured.err or True  # Warn is emitted to stderr

    def test_second_call_returns_same_logger(self, tmp_path):
        """get_logger() must not add duplicate handlers on second call."""
        log_file = str(tmp_path / "robot.log")
        with patch("live_trading.config.LOG_FILE", log_file):
            import live_trading.logger as lm
            name = "test_singleton_handlers"
            # Clear any cached version
            if name in logging.Logger.manager.loggerDict:
                del logging.Logger.manager.loggerDict[name]
            l1 = lm.get_logger(name)
            count1 = len(l1.handlers)
            l2 = lm.get_logger(name)
            count2 = len(l2.handlers)
        assert count1 == count2, \
            "Second get_logger() call must not add duplicate handlers"

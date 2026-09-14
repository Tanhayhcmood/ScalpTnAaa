"""
Settings Validation Tests — telegram_panel/config/settings.py
=============================================================
Tests that panel settings validate() correctly enforces required fields.

These tests verify ENGINEERING correctness only.
No strategy logic, no signal computation, no trading behaviour is tested.
"""
import os
import pytest
from unittest.mock import patch


def _make_settings(overrides: dict):
    """Create a Settings object with specified overrides applied as env vars."""
    import sys
    if "telegram_panel.config.settings" in sys.modules:
        del sys.modules["telegram_panel.config.settings"]
    with patch.dict(os.environ, overrides, clear=False):
        from telegram_panel.config.settings import Settings
        return Settings.from_env()


class TestRequiredFields:
    """validate() must return errors for missing required fields."""

    def test_missing_bot_token_returns_error(self):
        s = _make_settings({"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_OWNER_ID": "12345",
                             "PANEL_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="})
        errors = s.validate()
        assert any("TELEGRAM_BOT_TOKEN" in e for e in errors), f"Expected BOT_TOKEN error, got: {errors}"

    def test_missing_owner_id_returns_error(self):
        s = _make_settings({"TELEGRAM_BOT_TOKEN": "abc:def", "TELEGRAM_OWNER_ID": "0",
                             "PANEL_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="})
        errors = s.validate()
        assert any("TELEGRAM_OWNER_ID" in e for e in errors), f"Expected OWNER_ID error, got: {errors}"

    def test_missing_encryption_key_returns_error(self):
        s = _make_settings({"TELEGRAM_BOT_TOKEN": "abc:def", "TELEGRAM_OWNER_ID": "12345",
                             "PANEL_ENCRYPTION_KEY": ""})
        errors = s.validate()
        assert any("PANEL_ENCRYPTION_KEY" in e for e in errors), f"Expected ENCRYPTION_KEY error, got: {errors}"

    def test_all_required_fields_present_no_error(self):
        # Use a known valid Fernet key format (44 URL-safe base64 chars = 32 bytes)
        from cryptography.fernet import Fernet
        valid_key = Fernet.generate_key().decode()
        s = _make_settings({
            "TELEGRAM_BOT_TOKEN": "123456:ABCdef",
            "TELEGRAM_OWNER_ID": "999999",
            "PANEL_ENCRYPTION_KEY": valid_key,
        })
        errors = s.validate()
        assert errors == [], f"Expected no errors, got: {errors}"

    def test_malformed_encryption_key_returns_error(self):
        s = _make_settings({
            "TELEGRAM_BOT_TOKEN": "123456:ABCdef",
            "TELEGRAM_OWNER_ID": "999999",
            "PANEL_ENCRYPTION_KEY": "not-a-valid-fernet-key",
        })
        errors = s.validate()
        assert any("PANEL_ENCRYPTION_KEY" in e for e in errors)

    def test_multiple_missing_fields_returns_multiple_errors(self):
        s = _make_settings({
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_OWNER_ID": "0",
            "PANEL_ENCRYPTION_KEY": "",
        })
        errors = s.validate()
        assert len(errors) >= 3


class TestOptionalFieldParsing:
    """Optional env vars parse correctly."""

    def test_admin_ids_parsed_correctly(self):
        s = _make_settings({"TELEGRAM_ADMIN_IDS": "111,222,333"})
        assert 111 in s.telegram.admin_ids
        assert 222 in s.telegram.admin_ids
        assert 333 in s.telegram.admin_ids

    def test_admin_ids_empty_is_empty_list(self):
        s = _make_settings({"TELEGRAM_ADMIN_IDS": ""})
        assert s.telegram.admin_ids == []

    def test_debug_flag_parsed(self):
        s = _make_settings({"DEBUG": "1"})
        assert s.debug is True

    def test_debug_flag_default_false(self):
        s = _make_settings({"DEBUG": "0"})
        assert s.debug is False

"""
Audit Masking Tests — telegram_panel/security/audit.py
=====================================================
Tests that the _mask_if_sensitive() utility correctly masks credential-like
field values in audit logs (Security fix F-01 / M-04).

These tests verify ENGINEERING correctness only.
No strategy logic, no trading behaviour is tested.
"""
import pytest


class TestMaskIfSensitive:
    """_mask_if_sensitive() masks known credential fields and passes others through."""

    @pytest.fixture
    def mask_fn(self):
        from telegram_panel.security.audit import _mask_if_sensitive
        return _mask_if_sensitive

    def test_password_field_is_masked(self, mask_fn):
        assert mask_fn("password", "my-broker-password") == "***MASKED***"

    def test_token_field_is_masked(self, mask_fn):
        assert mask_fn("token", "abc123token") == "***MASKED***"

    def test_api_key_field_is_masked(self, mask_fn):
        assert mask_fn("api_key", "some-key") == "***MASKED***"

    def test_encryption_key_field_is_masked(self, mask_fn):
        assert mask_fn("encryption_key", "fernet-key-value") == "***MASKED***"

    def test_panel_encryption_key_field_is_masked(self, mask_fn):
        assert mask_fn("panel_encryption_key", "fernet-key-value") == "***MASKED***"

    def test_non_sensitive_field_passes_through(self, mask_fn):
        assert mask_fn("username", "my_username") == "my_username"

    def test_symbol_field_passes_through(self, mask_fn):
        assert mask_fn("symbol", "XAUUSD") == "XAUUSD"

    def test_none_field_name_passes_through(self, mask_fn):
        assert mask_fn(None, "some-value") == "some-value"

    def test_case_insensitive_matching(self, mask_fn):
        assert mask_fn("PASSWORD", "secret") == "***MASKED***"
        assert mask_fn("Password", "secret") == "***MASKED***"

    def test_empty_value_is_masked_if_sensitive_field(self, mask_fn):
        # Even empty strings in sensitive fields should be masked consistently
        # (no information about whether a credential was set)
        assert mask_fn("password", "") == "***MASKED***"

    def test_broker_password_field_is_masked(self, mask_fn):
        assert mask_fn("broker_password", "real-password") == "***MASKED***"

    def test_mt5_password_field_is_masked(self, mask_fn):
        assert mask_fn("mt5_password", "real-password") == "***MASKED***"

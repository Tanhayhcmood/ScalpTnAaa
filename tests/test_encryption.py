"""
Encryption Service Tests — telegram_panel/storage/encryption.py
===============================================================
Tests encryption/decryption correctness and the startup enforcement fix.

These tests verify ENGINEERING correctness only.
No strategy logic, no trading behaviour is tested.
"""
import pytest


class TestEncryptionRoundTrip:
    """Data encrypted with a key must decrypt back to the original plaintext."""

    @pytest.fixture
    def valid_key(self):
        from cryptography.fernet import Fernet
        return Fernet.generate_key().decode()

    @pytest.fixture
    def service(self, valid_key):
        from telegram_panel.storage.encryption import EncryptionService
        return EncryptionService(valid_key)

    def test_encrypt_decrypt_roundtrip(self, service):
        plaintext = "my-secret-password-123"
        ciphertext = service.encrypt(plaintext)
        assert ciphertext != plaintext
        assert not ciphertext.startswith("b64:")
        recovered = service.decrypt(ciphertext)
        assert recovered == plaintext

    def test_empty_string_encrypts_to_empty(self, service):
        assert service.encrypt("") == ""

    def test_decrypt_empty_returns_none(self, service):
        assert service.decrypt("") is None

    def test_decrypt_wrong_ciphertext_returns_none(self, service):
        result = service.decrypt("not-valid-ciphertext")
        assert result is None

    def test_is_secure_true_with_valid_key(self, service):
        assert service.is_secure is True

    def test_unicode_roundtrip(self, service):
        plaintext = "رمز عبور: ⓈⒺⒸⓇⒺⓉ"
        recovered = service.decrypt(service.encrypt(plaintext))
        assert recovered == plaintext


class TestEncryptionWithoutKey:
    """Without a key, service falls back to base64 (legacy — not recommended)."""

    @pytest.fixture
    def service_no_key(self):
        from telegram_panel.storage.encryption import EncryptionService
        return EncryptionService("")

    def test_is_secure_false_without_key(self, service_no_key):
        assert service_no_key.is_secure is False

    def test_encrypt_refuses_without_key(self, service_no_key):
        with pytest.raises(RuntimeError, match="encryption is unavailable"):
            service_no_key.encrypt("test")

    def test_legacy_base64_can_still_be_read_for_migration(self, service_no_key):
        """Legacy records remain readable so operators can re-encrypt them."""
        assert service_no_key.decrypt("b64:dGhpcy1pcy1sZWdhY3k=") == "this-is-legacy"

class TestKeyGeneration:
    """generate_key() produces valid Fernet keys."""

    def test_generate_key_returns_string(self):
        from telegram_panel.storage.encryption import EncryptionService
        key = EncryptionService.generate_key()
        assert isinstance(key, str)

    def test_generated_key_is_valid_fernet_key(self):
        from telegram_panel.storage.encryption import EncryptionService
        from cryptography.fernet import Fernet
        key = EncryptionService.generate_key()
        # Should not raise
        Fernet(key.encode())

    def test_two_generated_keys_are_different(self):
        from telegram_panel.storage.encryption import EncryptionService
        key1 = EncryptionService.generate_key()
        key2 = EncryptionService.generate_key()
        assert key1 != key2

    def test_generated_key_works_for_encryption(self):
        from telegram_panel.storage.encryption import EncryptionService
        key = EncryptionService.generate_key()
        svc = EncryptionService(key)
        assert svc.is_secure is True
        recovered = svc.decrypt(svc.encrypt("production-password"))
        assert recovered == "production-password"

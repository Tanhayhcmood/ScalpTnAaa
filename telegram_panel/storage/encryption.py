"""
Encryption service — AES-128 via Fernet (symmetric encryption).
Used to store broker credentials securely in SQLite.
Never stores raw passwords anywhere.
"""

import base64
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class EncryptionService:
    """
    Symmetric encryption for sensitive storage using Fernet.
    Key must be a 32-byte URL-safe base64-encoded string.
    """

    def __init__(self, key: str = "") -> None:
        self._fernet = None
        if key:
            self._init_fernet(key)

    def _init_fernet(self, key: str) -> None:
        try:
            from cryptography.fernet import Fernet, InvalidToken
            self._InvalidToken = InvalidToken
            # Validate key is correct format
            key_bytes = key.encode() if isinstance(key, str) else key
            self._fernet = Fernet(key_bytes)
            logger.info("Encryption service initialized successfully")
        except ImportError as e:
            raise RuntimeError(
                "cryptography is required for credential encryption. "
                "Install it before starting the Telegram panel."
            ) from e
        except Exception as e:
            raise ValueError(
                "PANEL_ENCRYPTION_KEY is not a valid Fernet key. "
                "Generate one with: python -m telegram_panel.main --generate-key"
            ) from e
    @staticmethod
    def generate_key() -> str:
        """Generate a new 32-byte Fernet key. Store in PANEL_ENCRYPTION_KEY env var."""
        from cryptography.fernet import Fernet
        return Fernet.generate_key().decode()

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a string. Returns encrypted base64 string."""
        if not plaintext:
            return ""
        if not self._fernet:
            raise RuntimeError(
                "Credential encryption is unavailable. "
                "Set a valid PANEL_ENCRYPTION_KEY before storing credentials."
            )
        try:
            encrypted = self._fernet.encrypt(plaintext.encode("utf-8"))
            return encrypted.decode("utf-8")
        except Exception as e:
            logger.error("Encryption failed; refusing to store insecure credential data")
            raise RuntimeError("Credential encryption failed") from e
    def decrypt(self, ciphertext: str) -> Optional[str]:
        """Decrypt a string. Returns plaintext or None on failure."""
        if not ciphertext:
            return None
        # Handle legacy base64 obfuscation
        if ciphertext.startswith("b64:"):
            try:
                return base64.b64decode(ciphertext[4:]).decode("utf-8")
            except Exception as e:
                logger.error(f"Base64 decode failed: {e}")
                return None
        if self._fernet:
            try:
                decrypted = self._fernet.decrypt(ciphertext.encode("utf-8"))
                return decrypted.decode("utf-8")
            except Exception as e:
                logger.error(f"Decryption failed: {e}")
                return None
        logger.error("No decryption key available; refusing to decrypt credential")
        return None

    @property
    def is_secure(self) -> bool:
        """True if real encryption (Fernet) is active."""
        return self._fernet is not None

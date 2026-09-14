"""
Settings — Single source of truth for all configuration.
Reads from environment variables with sensible defaults.
Never hardcodes credentials or secrets.
"""

import os
import json
from dataclasses import dataclass, field
from typing import Optional
from functools import lru_cache


@dataclass
class TelegramSettings:
    bot_token: str = ""
    owner_id: int = 0
    admin_ids: list[int] = field(default_factory=list)


@dataclass
class DatabaseSettings:
    # Path is read from PANEL_DB_PATH env var so Render persistent disk path works.
    # Defaults to project-relative path for local development.
    path: str = os.getenv("PANEL_DB_PATH", "telegram_panel/storage/data/panel.db")
    echo: bool = False
    pool_size: int = 5
    max_overflow: int = 10


@dataclass
class SecuritySettings:
    encryption_key: str = ""          # 32-byte Fernet key (base64)
    session_timeout_minutes: int = 60
    rate_limit_window_seconds: int = 60
    rate_limit_max_requests: int = 30
    audit_log_retention_days: int = 90


@dataclass
class RobotSettings:
    # Paths to robot configuration files (relative to project root)
    config_path: str = "robot_config.json"
    state_path: str = "robot_state.json"
    log_path: str = "logs/robot.log"
    # Interface mode: 'file' | 'socket' | 'http'
    interface_mode: str = "file"
    interface_host: str = "127.0.0.1"
    interface_port: int = 9876
    # For cross-service HTTP reads (e.g. on Render): full base URL of the robot service.
    # When set, ROBOT_INTERFACE_MODE=http will call {base_url}/status instead of localhost.
    base_url: str = ""
    heartbeat_interval_seconds: int = 30


@dataclass
class MT5Settings:
    host: str = "127.0.0.1"
    port: int = 18812
    timeout_seconds: int = 10


@dataclass
class NotificationSettings:
    heartbeat_interval_seconds: int = 600
    max_queue_size: int = 1000
    retry_attempts: int = 3
    retry_delay_seconds: float = 2.0


@dataclass
class LoggingSettings:
    level: str = "INFO"
    format: str = "json"
    path: str = "telegram_panel/storage/data/logs/panel.log"
    max_bytes: int = 10_000_000   # 10 MB
    backup_count: int = 5


@dataclass
class Settings:
    telegram: TelegramSettings = field(default_factory=TelegramSettings)
    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)
    robot: RobotSettings = field(default_factory=RobotSettings)
    mt5: MT5Settings = field(default_factory=MT5Settings)
    notifications: NotificationSettings = field(default_factory=NotificationSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    debug: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        """Load settings from environment variables."""
        s = cls()

        # Telegram
        s.telegram.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        owner_raw = os.environ.get("TELEGRAM_OWNER_ID", "0").strip()
        try:
            s.telegram.owner_id = int(owner_raw) if owner_raw else 0
        except (ValueError, OverflowError):
            s.telegram.owner_id = 0

        admins_raw = os.environ.get("TELEGRAM_ADMIN_IDS", "")
        s.telegram.admin_ids = [
            int(x.strip()) for x in admins_raw.split(",")
            if x.strip().isdigit()
        ]

        # Database
        s.database.path = os.environ.get(
            "PANEL_DB_PATH", "telegram_panel/storage/data/panel.db"
        )

        # Security
        s.security.encryption_key = os.environ.get("PANEL_ENCRYPTION_KEY", "").strip()
        try:
            s.security.session_timeout_minutes = int(
                os.environ.get("SESSION_TIMEOUT_MINUTES", "60")
            )
        except ValueError:
            raise ValueError(
                "SESSION_TIMEOUT_MINUTES must be an integer (e.g. '60'). "
                f"Got: {os.environ.get('SESSION_TIMEOUT_MINUTES')!r}"
            )

        # Robot
        s.robot.config_path = os.environ.get("ROBOT_CONFIG_PATH", "robot_config.json")
        s.robot.state_path = os.environ.get("ROBOT_STATE_PATH", "robot_state.json")
        s.robot.log_path = os.environ.get("ROBOT_LOG_PATH", "logs/robot.log")
        s.robot.interface_mode = os.environ.get("ROBOT_INTERFACE_MODE", "file")
        s.robot.interface_host = os.environ.get("ROBOT_INTERFACE_HOST", "127.0.0.1")
        s.robot.base_url = os.environ.get("ROBOT_BASE_URL", "")
        try:
            s.robot.interface_port = int(os.environ.get("ROBOT_INTERFACE_PORT", "9876"))
        except ValueError:
            raise ValueError(
                "ROBOT_INTERFACE_PORT must be an integer. "
                f"Got: {os.environ.get('ROBOT_INTERFACE_PORT')!r}"
            )
        try:
            s.robot.heartbeat_interval_seconds = int(
                os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "30")
            )
        except ValueError:
            raise ValueError(
                "HEARTBEAT_INTERVAL_SECONDS must be an integer. "
                f"Got: {os.environ.get('HEARTBEAT_INTERVAL_SECONDS')!r}"
            )

        # MT5
        s.mt5.host = os.environ.get("MT5_HOST", "127.0.0.1")
        try:
            s.mt5.port = int(os.environ.get("MT5_PORT", "18812"))
        except ValueError:
            raise ValueError(
                "MT5_PORT must be an integer. "
                f"Got: {os.environ.get('MT5_PORT')!r}"
            )
        try:
            s.mt5.timeout_seconds = int(os.environ.get("MT5_TIMEOUT_SECONDS", "10"))
        except ValueError:
            raise ValueError(
                "MT5_TIMEOUT_SECONDS must be an integer. "
                f"Got: {os.environ.get('MT5_TIMEOUT_SECONDS')!r}"
            )

        # Logging
        s.logging.level = os.environ.get("LOG_LEVEL", "INFO")
        s.logging.path = os.environ.get(
            "PANEL_LOG_PATH", "telegram_panel/storage/data/logs/panel.log"
        )

        s.debug = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")

        return s

    @classmethod
    def from_file(cls, path: str) -> "Settings":
        """Load settings from a JSON config file."""
        if not os.path.exists(path):
            return cls.from_env()

        with open(path, "r") as f:
            data = json.load(f)

        s = cls.from_env()  # start from env, overlay with file values

        tg = data.get("telegram", {})
        if tg.get("bot_token"):
            s.telegram.bot_token = tg["bot_token"]
        if tg.get("owner_id"):
            s.telegram.owner_id = int(tg["owner_id"])
        if tg.get("admin_ids"):
            s.telegram.admin_ids = [int(x) for x in tg["admin_ids"]]

        db = data.get("database", {})
        if db.get("path"):
            s.database.path = db["path"]

        sec = data.get("security", {})
        if sec.get("encryption_key"):
            s.security.encryption_key = sec["encryption_key"]
        if sec.get("session_timeout_minutes"):
            s.security.session_timeout_minutes = int(sec["session_timeout_minutes"])

        robot = data.get("robot", {})
        if robot.get("config_path"):
            s.robot.config_path = robot["config_path"]
        if robot.get("state_path"):
            s.robot.state_path = robot["state_path"]
        if robot.get("interface_mode"):
            s.robot.interface_mode = robot["interface_mode"]

        return s

    def validate(self) -> list[str]:
        """Return list of validation errors (empty = valid)."""
        errors = []
        if not self.telegram.bot_token:
            errors.append("TELEGRAM_BOT_TOKEN is required")
        if not self.telegram.owner_id:
            errors.append("TELEGRAM_OWNER_ID is required")

        # PANEL_ENCRYPTION_KEY is mandatory for production security.
        # Without it, broker passwords are stored as reversible base64 in SQLite
        # (Security finding A-01 — HIGH severity).
        # Generate a key: python -m telegram_panel.main --generate-key
        if not self.security.encryption_key:
            errors.append(
                "PANEL_ENCRYPTION_KEY is required. "
                "Without it, broker credentials are stored as reversible base64. "
                "Generate a key: python -m telegram_panel.main --generate-key"
            )
        else:
            # Validate key format: must be a valid Fernet key (URL-safe base64, 44 chars)
            try:
                import base64
                key_bytes = self.security.encryption_key.encode()
                decoded = base64.urlsafe_b64decode(key_bytes + b"==")
                if len(decoded) != 32:
                    errors.append(
                        "PANEL_ENCRYPTION_KEY is not a valid 32-byte Fernet key. "
                        "Generate a new key: python -m telegram_panel.main --generate-key"
                    )
            except Exception:
                errors.append(
                    "PANEL_ENCRYPTION_KEY is malformed (not valid URL-safe base64). "
                    "Generate a new key: python -m telegram_panel.main --generate-key"
                )

        return errors


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton settings instance."""
    config_file = os.environ.get("PANEL_CONFIG_FILE", "telegram_panel/config/panel.json")
    return Settings.from_file(config_file)

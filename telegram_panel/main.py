"""
GoldScalperPro Telegram Control Panel — Entry Point
====================================================
Run with:
    python -m telegram_panel.main
or:
    python telegram_panel/main.py

Environment variables required:
    TELEGRAM_BOT_TOKEN   — BotFather token
    TELEGRAM_OWNER_ID    — Your Telegram user ID

Optional:
    TELEGRAM_ADMIN_IDS   — Comma-separated Telegram IDs for admins
    PANEL_ENCRYPTION_KEY — Fernet key for credential encryption
    PANEL_CONFIG_FILE    — Path to JSON config (default: telegram_panel/config/panel.json)
    DEBUG                — "1" for verbose logging

The existing trading robot is NOT modified. This panel runs
as a completely separate process alongside the robot.
"""

import asyncio
import logging
import os
import signal
import sys

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def setup_logging(level: str = "INFO", log_path: str = "") -> None:
    """Configure structured logging."""
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if log_path:
        import os
        from logging.handlers import RotatingFileHandler
        log_dir = os.path.dirname(log_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_path,
                maxBytes=10_000_000,
                backupCount=5,
                encoding="utf-8",
            )
        )

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
        datefmt=datefmt,
        handlers=handlers,
    )

    # Reduce noise from libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


class TelegramPanel:
    """
    Top-level entry point for the Telegram Control Panel.

    Usage::
        panel = TelegramPanel()
        asyncio.run(panel.run())
    """

    def __init__(self, config_file: str = "") -> None:
        from .config.settings import Settings
        config_file = config_file or os.environ.get(
            "PANEL_CONFIG_FILE", "telegram_panel/config/panel.json"
        )
        self._settings = Settings.from_file(config_file)
        self._bot_app = None
        self._shutdown_called: bool = False  # guard against double-call from signal + finally

    def validate(self) -> bool:
        """Check required settings before starting."""
        errors = self._settings.validate()
        if errors:
            for error in errors:
                logger.error(f"Configuration error: {error}")
            return False
        return True

    async def run(self) -> None:
        """Start the panel and run until interrupted."""
        setup_logging(
            self._settings.logging.level,
            self._settings.logging.path,
        )

        logger.info("═" * 50)
        logger.info("  GoldScalperPro Telegram Control Panel v1.0")
        logger.info("═" * 50)

        if not self.validate():
            logger.error(
                "Invalid configuration. Set TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_ID "
                "environment variables and retry."
            )
            sys.exit(1)

        from .core.bot import BotApplication
        self._bot_app = BotApplication(self._settings)

        # Register graceful shutdown signals
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self._shutdown()))

        try:
            await self._bot_app.start()
            logger.info("Panel is running. Press Ctrl+C to stop.")
            await self._bot_app.run_polling()
        except (KeyboardInterrupt, SystemExit):
            pass
        except Exception as e:
            logger.exception(f"Fatal error: {e}")
            # Re-raise so server.py wrapper can trigger its 30-second grace
            # period and then restart the service via Render.  Without this
            # re-raise the wrapper sees a clean return (exit code 0) and exits
            # the process silently, preventing the restart cycle.
            raise
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        """Graceful shutdown — idempotent (safe to call more than once)."""
        if self._shutdown_called:
            return
        self._shutdown_called = True
        if self._bot_app:
            try:
                await self._bot_app.stop()
            except Exception as exc:
                logger.warning(f"bot_app.stop() raised during shutdown: {exc}")
        # NOTE: do NOT call loop.stop() here.  This module may run inside
        # server.py's asyncio.run() event loop which owns the health server and
        # the 30-second grace period.  Calling loop.stop() bypasses all of that:
        # the process exits immediately (exit code 0) before the health server
        # can respond to Render's checks or the grace period can run, causing
        # every deploy to be marked update_failed with earlyExit.


def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="GoldScalperPro Telegram Control Panel"
    )
    parser.add_argument(
        "--config",
        default="telegram_panel/config/panel.json",
        help="Path to JSON config file (default: telegram_panel/config/panel.json)",
    )
    parser.add_argument(
        "--generate-key",
        action="store_true",
        help="Generate a new encryption key and exit",
    )
    args = parser.parse_args()

    if args.generate_key:
        from .storage.encryption import EncryptionService
        key = EncryptionService.generate_key()
        print(f"\n🔑 New encryption key generated:\n\n{key}\n")
        print("Add this to your environment:\n  PANEL_ENCRYPTION_KEY=" + key)
        print("\n⚠️  Store this key securely — losing it means losing access to encrypted credentials.\n")
        sys.exit(0)

    panel = TelegramPanel(args.config)
    asyncio.run(panel.run())


if __name__ == "__main__":
    main()

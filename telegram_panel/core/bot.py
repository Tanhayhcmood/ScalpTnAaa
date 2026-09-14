"""
Bot Application — assembles and wires all components together.
Single responsibility: dependency injection and lifecycle management.

This is the composition root. All dependencies are resolved here.
"""

import logging
import asyncio
from typing import Optional
from telegram.ext import Application, ApplicationBuilder, Defaults
from telegram import BotCommand

from ..config.settings import Settings
from ..storage.database import Database, get_database
from ..storage.encryption import EncryptionService
from ..storage.repositories.account_repo import AccountRepository
from ..storage.repositories.user_repo import UserRepository
from ..storage.repositories.settings_repo import SettingsRepository
from ..storage.repositories.notification_repo import NotificationRepository
from ..storage.repositories.audit_repo import AuditRepository
from ..storage.repositories.report_repo import ReportRepository
from ..storage.repositories.session_repo import SessionRepository
from ..services.robot_service import RobotService
from ..services.mt5_service import MT5Service
from ..services.account_service import AccountService
from ..services.trade_service import TradeService
from ..services.risk_service import RiskService
from ..services.strategy_service import StrategyService
from ..services.report_service import ReportService
from ..services.system_service import SystemService
from ..services.notification_service import NotificationService
from ..api.handlers.dashboard import DashboardHandler
from ..api.handlers.accounts import AccountsHandler
from ..api.handlers.trading import TradingHandler
from ..api.handlers.risk import RiskHandler
from ..api.handlers.strategy import StrategyHandler
from ..api.handlers.reports import ReportsHandler
from ..api.handlers.notifications_handler import NotificationsHandler
from ..api.handlers.system import SystemHandler
from ..api.middleware.auth import AuthMiddleware
from ..api.middleware.rate_limiter import RateLimiter
from ..api.formatters.messages import MessageFormatter
from ..api.router import Router
from .event_bus import EventBus, Events
from .heartbeat import HeartbeatMonitor

logger = logging.getLogger(__name__)


class BotApplication:
    """
    Assembles the complete Telegram panel.
    Call start() to run; call stop() to shut down cleanly.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._app: Optional[Application] = None
        self._event_bus: Optional[EventBus] = None
        self._heartbeat: Optional[HeartbeatMonitor] = None
        self._notification_service: Optional[NotificationService] = None
        self._db: Optional[Database] = None
        self._stop_event: Optional[asyncio.Event] = None

    async def start(self) -> None:
        """Build all components and start the bot."""
        logger.info("Initializing Telegram Control Panel...")

        # ── Database ──────────────────────────────────────────────────────
        self._db = get_database()
        await self._db.initialize()

        # ── Repositories ──────────────────────────────────────────────────
        account_repo = AccountRepository(self._db)
        user_repo = UserRepository(self._db)
        settings_repo = SettingsRepository(self._db)
        notif_repo = NotificationRepository(self._db)
        audit_repo = AuditRepository(self._db)
        report_repo = ReportRepository(self._db)
        session_repo = SessionRepository(self._db)

        # ── Security ──────────────────────────────────────────────────────
        encryption = EncryptionService(self._settings.security.encryption_key)

        # ── Robot / MT5 interfaces ────────────────────────────────────────
        # These services are the composition root's shared dependencies for
        # handlers and business services.  Construct them before injection so
        # the panel can start even when the robot is temporarily disconnected.
        robot_svc = RobotService(
            state_path=self._settings.robot.state_path,
            config_path=self._settings.robot.config_path,
            interface_mode=self._settings.robot.interface_mode,
            base_url=self._settings.robot.base_url,
        )
        snapshot_path = self._settings.robot.state_path.replace(
            "state", "mt5_snapshot"
        )
        mt5_svc = MT5Service(
            snapshot_path=snapshot_path,
            base_url=self._settings.robot.base_url,
        )

        account_svc = AccountService(account_repo, encryption, mt5_svc, robot_svc)
        trade_svc = TradeService(mt5_svc)
        risk_svc = RiskService(settings_repo, robot_svc)
        strategy_svc = StrategyService(settings_repo, robot_svc)
        report_svc = ReportService(report_repo)
        system_svc = SystemService(robot_svc)
        notif_svc = NotificationService(
            notification_repo=notif_repo,
            owner_id=self._settings.telegram.owner_id,
            admin_ids=self._settings.telegram.admin_ids,
            max_retries=self._settings.notifications.retry_attempts,
            retry_delay=self._settings.notifications.retry_delay_seconds,
        )
        await notif_svc.initialize_defaults()
        self._notification_service = notif_svc

        # ── Middleware ────────────────────────────────────────────────────
        auth = AuthMiddleware(
            user_repo=user_repo,
            audit_repo=audit_repo,
            owner_id=self._settings.telegram.owner_id,
            admin_ids=self._settings.telegram.admin_ids,
        )
        rate_limiter = RateLimiter(
            max_requests=self._settings.security.rate_limit_max_requests,
            window_seconds=self._settings.security.rate_limit_window_seconds,
            exempt_ids=frozenset({self._settings.telegram.owner_id}),
        )
        formatter = MessageFormatter()

        # ── Handlers ──────────────────────────────────────────────────────
        dashboard_handler = DashboardHandler(
            robot_svc, mt5_svc, account_svc, system_svc, auth, formatter
        )
        accounts_handler = AccountsHandler(account_svc, auth, formatter)
        trading_handler = TradingHandler(trade_svc, auth, formatter)
        risk_handler = RiskHandler(risk_svc, auth, formatter)
        strategy_handler = StrategyHandler(strategy_svc, auth, formatter)
        reports_handler = ReportsHandler(report_svc, account_svc, auth, formatter)
        notif_handler = NotificationsHandler(notif_svc, auth, formatter)
        system_handler = SystemHandler(
            system_svc, robot_svc, user_repo, audit_repo, auth, formatter,
            robot_log_path=self._settings.robot.log_path,
        )

        # ── Router ────────────────────────────────────────────────────────
        router = Router(
            dashboard=dashboard_handler,
            accounts=accounts_handler,
            trading=trading_handler,
            risk=risk_handler,
            strategy=strategy_handler,
            reports=reports_handler,
            notifications=notif_handler,
            system=system_handler,
            auth=auth,
            rate_limiter=rate_limiter,
            formatter=formatter,
        )

        # ── Telegram Application ──────────────────────────────────────────
        defaults = Defaults(parse_mode="HTML", tzinfo=None)
        builder = (
            ApplicationBuilder()
            .token(self._settings.telegram.bot_token)
            .defaults(defaults)
            .concurrent_updates(True)
        )
        self._app = builder.build()

        # Register all handlers
        router.register(self._app)

        # Set bot commands
        await self._app.initialize()
        await self._app.bot.set_my_commands([
            BotCommand("start", "Main menu"),
            BotCommand("dashboard", "Dashboard"),
            BotCommand("menu", "Main menu"),
            BotCommand("status", "Robot status"),
            BotCommand("help", "Help"),
        ])

        # ── Event Bus ─────────────────────────────────────────────────────
        self._event_bus = EventBus()
        await self._event_bus.start()

        # Wire up notifications to events
        self._wire_event_notifications(notif_svc, formatter)

        # ── Heartbeat ─────────────────────────────────────────────────────
        self._heartbeat = HeartbeatMonitor(
            robot_service=robot_svc,
            event_bus=self._event_bus,
            interval_seconds=self._settings.robot.heartbeat_interval_seconds,
            heartbeat_notify_interval=self._settings.notifications.heartbeat_interval_seconds,
            mt5_service=mt5_svc,
        )
        await self._heartbeat.start()

        # ── Notification Service (inject bot) ─────────────────────────────
        notif_svc.set_bot(self._app)
        await notif_svc.start()

        # ── Start Polling ─────────────────────────────────────────────────
        await self._app.start()
        logger.info("✅ Telegram Control Panel is running")

        # Notify owner
        try:
            await self._app.bot.send_message(
                chat_id=self._settings.telegram.owner_id,
                text=(
                    "🟢 <b>GoldScalperPro Panel started</b>\n"
                    "Send /start to open the control panel."
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Could not send startup message to owner: {e}")

    async def run_polling(self) -> None:
        """Run the bot in polling mode (blocking until stopped)."""
        if not self._app:
            raise RuntimeError("Call start() before run_polling()")
        # Create a named stop event so stop() can unblock this coroutine.
        # The old code used asyncio.Event() without saving a reference, which
        # meant stop() had no way to signal it — the coroutine blocked forever
        # and SIGTERM could only kill the process by calling loop.stop().
        self._stop_event = asyncio.Event()
        await self._app.updater.start_polling(
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
        )
        # Block here until stop() sets the event (SIGTERM / clean shutdown)
        # or until an exception propagates from the updater.
        await self._stop_event.wait()

    async def stop(self) -> None:
        """Clean shutdown of all components."""
        logger.info("Shutting down Telegram Control Panel...")
        # Unblock run_polling() first so panel.run() can return cleanly.
        # Must happen before stopping the updater to avoid a deadlock where
        # run_polling() waits on _stop_event while stop() waits for updater.
        if self._stop_event and not self._stop_event.is_set():
            self._stop_event.set()
        if self._heartbeat:
            await self._heartbeat.stop()
        if self._notification_service:
            await self._notification_service.stop()
        if self._event_bus:
            await self._event_bus.stop()
        if self._app:
            try:
                if self._app.updater and self._app.updater.running:
                    await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception as e:
                logger.error(f"Error during shutdown: {e}")
        logger.info("Telegram Control Panel stopped")

    def _wire_event_notifications(
        self, notif_svc: NotificationService, formatter: MessageFormatter
    ) -> None:
        """Connect event bus events to notification service."""
        from ..config.constants import NotificationType

        async def on_heartbeat(data: dict) -> None:
            msg = formatter.heartbeat(
                data.get("status", "unknown"),
                data.get("uptime_seconds", 0),
            )
            await notif_svc.notify(NotificationType.HEARTBEAT, msg)

        async def on_connection_lost(data: dict) -> None:
            msg = f"🔴 <b>Connection Lost</b>\nRobot lost broker connection."
            await notif_svc.notify_all_admins(NotificationType.CONNECTION_LOST, msg)

        async def on_connection_restored(data: dict) -> None:
            msg = f"🟢 <b>Connection Restored</b>\nBroker connection re-established."
            await notif_svc.notify_all_admins(NotificationType.CONNECTION_RESTORED, msg)

        async def on_robot_error(data: dict) -> None:
            msg = formatter.error_alert(data.get("error", "Unknown error"))
            await notif_svc.notify_all_admins(NotificationType.ERROR, msg)

        async def on_system_restart(data: dict) -> None:
            msg = f"🔄 <b>System Restart</b>\n{data.get('reason', '')}"
            await notif_svc.notify_all_admins(NotificationType.SYSTEM_RESTART, msg)

        # FIX: Events.TRADE_OPENED was defined but never published/consumed —
        # newly opened trades never reached Telegram. HeartbeatMonitor now
        # publishes this event as soon as it detects a new open position; this
        # handler formats and sends the actual notification.
        async def on_trade_opened(data: dict) -> None:
            pos = data.get("position")
            if pos is None:
                return
            msg = formatter.trade_opened(pos)
            await notif_svc.notify_all_admins(NotificationType.TRADE_OPEN, msg)

        self._event_bus.subscribe(Events.HEARTBEAT, on_heartbeat)
        self._event_bus.subscribe(Events.CONNECTION_LOST, on_connection_lost)
        self._event_bus.subscribe(Events.CONNECTION_RESTORED, on_connection_restored)
        self._event_bus.subscribe(Events.ROBOT_ERROR, on_robot_error)
        self._event_bus.subscribe(Events.SYSTEM_RESTART, on_system_restart)
        self._event_bus.subscribe(Events.TRADE_OPENED, on_trade_opened)

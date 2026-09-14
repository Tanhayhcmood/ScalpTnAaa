"""
Notification Service — sends Telegram notifications to configured users.
Runs on a queue to never block the trading engine.
"""

import asyncio
import logging
from typing import Optional, Any
from ..config.constants import NotificationType
from ..models.notification import NotificationSetting, NotificationLog
from ..storage.repositories.notification_repo import NotificationRepository
from ..utils.time import utc_now

logger = logging.getLogger(__name__)


class NotificationService:
    """
    Queued notification delivery.
    Supports per-type enable/disable, cooldowns, and retry logic.
    """

    def __init__(
        self,
        notification_repo: NotificationRepository,
        bot_app=None,   # Injected after bot starts (avoids circular import)
        owner_id: int = 0,
        admin_ids: Optional[list[int]] = None,
        max_retries: int = 3,
        retry_delay: float = 2.0,
    ) -> None:
        self._repo = notification_repo
        self._bot_app = bot_app
        self._owner_id = owner_id
        self._admin_ids = admin_ids or []
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._worker_task: Optional[asyncio.Task] = None
        self._running = False

    def set_bot(self, bot_app) -> None:
        """Inject bot reference after startup (break circular dep)."""
        self._bot_app = bot_app

    async def start(self) -> None:
        self._running = True
        self._worker_task = asyncio.create_task(self._worker())
        logger.info("Notification service started")

    async def stop(self) -> None:
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("Notification service stopped")

    async def notify(
        self,
        notification_type: NotificationType,
        message: str,
        recipients: Optional[list[int]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """
        Enqueue a notification for delivery.
        Never blocks — returns immediately.
        """
        # Check if this type is enabled globally
        setting = await self._repo.get_setting(notification_type)
        if setting and not setting.enabled:
            return

        # Check cooldown
        if setting and setting.cooldown_seconds > 0 and setting.last_sent_at:
            last_sent_at = setting.last_sent_at
            if last_sent_at.tzinfo is None:
                last_sent_at = last_sent_at.replace(tzinfo=utc_now().tzinfo)
            elapsed = (utc_now() - last_sent_at).total_seconds()
            if elapsed < setting.cooldown_seconds:
                logger.debug(f"Skipping {notification_type} — cooldown")
                return

        # Determine recipients
        if recipients is None:
            recipients = self._get_default_recipients(notification_type)

        payload = {
            "type": notification_type,
            "message": message,
            "recipients": recipients,
            "metadata": metadata or {},
        }

        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning(f"Notification queue full — dropped {notification_type}")

    async def notify_all_admins(
        self, notification_type: NotificationType, message: str
    ) -> None:
        recipients = [self._owner_id] + self._admin_ids
        await self.notify(notification_type, message, recipients=recipients)

    async def get_settings(
        self, telegram_id: Optional[int] = None
    ) -> list[NotificationSetting]:
        return await self._repo.get_settings(telegram_id)

    async def update_setting(
        self,
        notification_type: NotificationType,
        enabled: bool,
        telegram_id: Optional[int] = None,
    ) -> None:
        setting = NotificationSetting(
            notification_type=notification_type,
            enabled=enabled,
            user_telegram_id=telegram_id,
        )
        await self._repo.upsert_setting(setting)

    async def initialize_defaults(self) -> None:
        """Create default settings for all notification types."""
        for ntype in NotificationType:
            existing = await self._repo.get_setting(ntype)
            if not existing:
                setting = NotificationSetting(
                    notification_type=ntype,
                    enabled=True,
                    user_telegram_id=None,
                    cooldown_seconds=30 if ntype == NotificationType.HEARTBEAT else 0,
                )
                await self._repo.upsert_setting(setting)

    # ─── Private ─────────────────────────────────────────────────────────────

    async def _worker(self) -> None:
        while self._running:
            try:
                payload = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                await self._deliver(payload)
                self._queue.task_done()
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Notification worker error: {e}")

    async def _deliver(self, payload: dict[str, Any]) -> None:
        ntype: NotificationType = payload["type"]
        message: str = payload["message"]
        recipients: list[int] = payload["recipients"]

        if not self._bot_app:
            logger.warning("Bot not initialized — notification dropped")
            return

        for recipient in recipients:
            for attempt in range(1, self._max_retries + 1):
                try:
                    msg = await self._bot_app.bot.send_message(
                        chat_id=recipient,
                        text=message,
                        parse_mode="HTML",
                    )
                    # Log success
                    log = NotificationLog(
                        id=None,
                        notification_type=ntype,
                        recipient_telegram_id=recipient,
                        message_text=message,
                        success=True,
                        message_id=msg.message_id,
                    )
                    await self._repo.log_notification(log)
                    await self._repo.update_last_sent(ntype)
                    break
                except Exception as e:
                    if attempt == self._max_retries:
                        logger.error(
                            f"Failed to send {ntype} to {recipient} "
                            f"after {self._max_retries} attempts: {e}"
                        )
                        log = NotificationLog(
                            id=None,
                            notification_type=ntype,
                            recipient_telegram_id=recipient,
                            message_text=message,
                            success=False,
                            error_message=str(e),
                        )
                        await self._repo.log_notification(log)
                    else:
                        await asyncio.sleep(self._retry_delay * attempt)

    def _get_default_recipients(self, notification_type: NotificationType) -> list[int]:
        """Owner gets everything; admins get everything except heartbeat."""
        recipients = [self._owner_id] if self._owner_id else []
        if notification_type != NotificationType.HEARTBEAT:
            recipients.extend(self._admin_ids)
        return list(set(recipients))

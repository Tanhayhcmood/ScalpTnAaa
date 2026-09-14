"""
Notification Repository.
"""

import logging
from typing import Optional
from ..database import Database
from ...models.notification import NotificationSetting, NotificationLog
from ...config.constants import NotificationType
from ...utils.time import parse_utc, utc_now

logger = logging.getLogger(__name__)


class NotificationRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_settings(self, telegram_id: Optional[int] = None) -> list[NotificationSetting]:
        async with self._db.connection() as db:
            if telegram_id is not None:
                cursor = await db.execute(
                    """SELECT * FROM notification_settings
                       WHERE user_telegram_id=? OR user_telegram_id IS NULL
                       ORDER BY notification_type""",
                    (telegram_id,),
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM notification_settings WHERE user_telegram_id IS NULL"
                )
            rows = await cursor.fetchall()
        return [self._row_to_setting(r) for r in rows]

    async def get_setting(
        self,
        notification_type: NotificationType,
        telegram_id: Optional[int] = None,
    ) -> Optional[NotificationSetting]:
        async with self._db.connection() as db:
            cursor = await db.execute(
                """SELECT * FROM notification_settings
                   WHERE notification_type=? AND user_telegram_id IS ?""",
                (notification_type.value, telegram_id),
            )
            row = await cursor.fetchone()
        return self._row_to_setting(row) if row else None

    async def upsert_setting(self, setting: NotificationSetting) -> None:
        async with self._db.connection() as db:
            await db.execute(
                """INSERT INTO notification_settings
                   (notification_type, user_telegram_id, enabled, cooldown_seconds, last_sent_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(notification_type, user_telegram_id)
                   DO UPDATE SET enabled=excluded.enabled,
                   cooldown_seconds=excluded.cooldown_seconds,
                   updated_at=datetime('now')""",
                (
                    setting.notification_type.value,
                    setting.user_telegram_id,
                    1 if setting.enabled else 0,
                    setting.cooldown_seconds,
                    setting.last_sent_at.isoformat() if setting.last_sent_at else None,
                ),
            )
            await db.commit()

    async def update_last_sent(self, notification_type: NotificationType) -> None:
        async with self._db.connection() as db:
            await db.execute(
                "UPDATE notification_settings SET last_sent_at=? WHERE notification_type=?",
                (utc_now().isoformat(), notification_type.value),
            )
            await db.commit()

    async def log_notification(self, log: NotificationLog) -> None:
        async with self._db.connection() as db:
            await db.execute(
                """INSERT INTO notification_logs
                   (notification_type, recipient_telegram_id, message_text,
                    success, error_message, message_id, metadata)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    log.notification_type.value,
                    log.recipient_telegram_id,
                    log.message_text,
                    1 if log.success else 0,
                    log.error_message,
                    log.message_id,
                    log.metadata,
                ),
            )
            await db.commit()

    async def get_recent_logs(self, limit: int = 50) -> list[NotificationLog]:
        async with self._db.connection() as db:
            cursor = await db.execute(
                "SELECT * FROM notification_logs ORDER BY sent_at DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
        return [self._row_to_log(r) for r in rows]

    def _row_to_setting(self, row) -> NotificationSetting:
        return NotificationSetting(
            notification_type=NotificationType(row["notification_type"]),
            user_telegram_id=row["user_telegram_id"],
            enabled=bool(row["enabled"]),
            cooldown_seconds=row["cooldown_seconds"],
            last_sent_at=parse_utc(row["last_sent_at"]),
        )

    def _row_to_log(self, row) -> NotificationLog:
        return NotificationLog(
            id=row["id"],
            notification_type=NotificationType(row["notification_type"]),
            recipient_telegram_id=row["recipient_telegram_id"],
            message_text=row["message_text"],
            sent_at=parse_utc(row["sent_at"]) or utc_now(),
            success=bool(row["success"]),
            error_message=row["error_message"],
            message_id=row["message_id"],
            metadata=row["metadata"],
        )

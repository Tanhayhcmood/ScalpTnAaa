"""
Audit Repository — immutable audit trail.
"""

import logging
from datetime import timedelta
from typing import Optional
from ..database import Database
from ...models.audit import AuditLog
from ...utils.time import parse_utc, utc_now

logger = logging.getLogger(__name__)


class AuditRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def log(self, entry: AuditLog) -> None:
        async with self._db.connection() as db:
            await db.execute(
                """INSERT INTO audit_logs
                   (telegram_id, username, action, description, target,
                    old_value, new_value, ip_address, success, error_message)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    entry.telegram_id, entry.username, entry.action,
                    entry.description, entry.target, entry.old_value,
                    entry.new_value, entry.ip_address,
                    1 if entry.success else 0, entry.error_message,
                ),
            )
            await db.commit()

    async def get_recent(self, limit: int = 50, telegram_id: Optional[int] = None) -> list[AuditLog]:
        async with self._db.connection() as db:
            if telegram_id is not None:
                cursor = await db.execute(
                    "SELECT * FROM audit_logs WHERE telegram_id=? ORDER BY created_at DESC LIMIT ?",
                    (telegram_id, limit),
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            rows = await cursor.fetchall()
        return [self._row_to_log(r) for r in rows]

    async def purge_old(self, retention_days: int = 90) -> int:
        cutoff = (utc_now() - timedelta(days=retention_days)).isoformat()
        async with self._db.connection() as db:
            cursor = await db.execute(
                "DELETE FROM audit_logs WHERE created_at < ?", (cutoff,)
            )
            await db.commit()
            return cursor.rowcount

    def _row_to_log(self, row) -> AuditLog:
        return AuditLog(
            id=row["id"],
            telegram_id=row["telegram_id"],
            username=row["username"],
            action=row["action"],
            description=row["description"],
            target=row["target"],
            old_value=row["old_value"],
            new_value=row["new_value"],
            ip_address=row["ip_address"],
            success=bool(row["success"]),
            error_message=row["error_message"],
            created_at=parse_utc(row["created_at"]) or utc_now(),
        )

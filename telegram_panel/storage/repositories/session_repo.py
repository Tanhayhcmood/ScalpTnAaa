"""
Session Repository — user session persistence.
"""

import json
import logging
from datetime import datetime
from typing import Optional
from ..database import Database
from ...models.session import UserSession
from ...utils.time import parse_utc, utc_now

logger = logging.getLogger(__name__)


class SessionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_session(self, telegram_id: int) -> Optional[UserSession]:
        async with self._db.connection() as db:
            cursor = await db.execute(
                """SELECT * FROM user_sessions
                   WHERE telegram_id=? AND is_active=1
                   ORDER BY last_activity_at DESC LIMIT 1""",
                (telegram_id,),
            )
            row = await cursor.fetchone()
        if not row:
            return None
        session = self._row_to_session(row)
        if session.is_expired():
            await self.close_session(telegram_id)
            return None
        return session

    async def save_session(self, session: UserSession) -> None:
        async with self._db.connection() as db:
            await db.execute(
                """INSERT INTO user_sessions
                   (telegram_id, current_page, breadcrumb, context, is_active,
                    started_at, last_activity_at, expires_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT DO NOTHING""",
                (
                    session.telegram_id, session.current_page,
                    json.dumps(session.breadcrumb), json.dumps(session.context),
                    1, session.started_at.isoformat(),
                    session.last_activity_at.isoformat(),
                    session.expires_at.isoformat(),
                ),
            )
            await db.commit()

    async def update_session(self, session: UserSession) -> None:
        async with self._db.connection() as db:
            await db.execute(
                """UPDATE user_sessions SET
                   current_page=?, breadcrumb=?, context=?,
                   last_activity_at=?, expires_at=?
                   WHERE telegram_id=? AND is_active=1""",
                (
                    session.current_page,
                    json.dumps(session.breadcrumb),
                    json.dumps(session.context),
                    session.last_activity_at.isoformat(),
                    session.expires_at.isoformat(),
                    session.telegram_id,
                ),
            )
            await db.commit()

    async def close_session(self, telegram_id: int) -> None:
        async with self._db.connection() as db:
            await db.execute(
                "UPDATE user_sessions SET is_active=0 WHERE telegram_id=?",
                (telegram_id,),
            )
            await db.commit()

    async def purge_expired(self) -> int:
        cutoff = utc_now().isoformat()
        async with self._db.connection() as db:
            cursor = await db.execute(
                "DELETE FROM user_sessions WHERE expires_at < ? OR is_active=0",
                (cutoff,),
            )
            await db.commit()
            return cursor.rowcount

    def _row_to_session(self, row) -> UserSession:
        breadcrumb = json.loads(row["breadcrumb"]) if row["breadcrumb"] else []
        context = json.loads(row["context"]) if row["context"] else {}
        return UserSession(
            id=row["id"],
            telegram_id=row["telegram_id"],
            current_page=row["current_page"],
            breadcrumb=breadcrumb,
            context=context,
            is_active=bool(row["is_active"]),
            started_at=parse_utc(row["started_at"]) or utc_now(),
            last_activity_at=parse_utc(row["last_activity_at"]) or utc_now(),
            expires_at=parse_utc(row["expires_at"]) or utc_now(),
        )

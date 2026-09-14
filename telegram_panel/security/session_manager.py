"""
Session Manager — wraps the session repository with lifecycle helpers.
Provides breadcrumb navigation state, multi-step conversation state,
and automatic expiry enforcement.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Any
from ..storage.repositories.session_repo import SessionRepository
from ..models.session import UserSession
from ..config.constants import SESSION_TIMEOUT_MINUTES

logger = logging.getLogger(__name__)


class SessionManager:
    """
    High-level session operations used by handlers and router.
    All state is persisted to SQLite so it survives bot restarts.
    """

    def __init__(
        self,
        session_repo: SessionRepository,
        timeout_minutes: int = SESSION_TIMEOUT_MINUTES,
    ) -> None:
        self._repo = session_repo
        self._timeout = timeout_minutes

    async def get_or_create(self, telegram_id: int) -> UserSession:
        session = await self._repo.get(telegram_id)
        if session is None or session.is_expired():
            session = UserSession(
                telegram_id=telegram_id,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=self._timeout),
            )
            await self._repo.upsert(session)
        return session

    async def set_context(
        self, telegram_id: int, key: str, value: Any
    ) -> None:
        """Store a key-value pair in the user's session context dict."""
        session = await self.get_or_create(telegram_id)
        session.context[key] = value
        await self._repo.upsert(session)

    async def get_context(self, telegram_id: int, key: str, default: Any = None) -> Any:
        session = await self._repo.get(telegram_id)
        if session is None:
            return default
        return session.context.get(key, default)

    async def clear_context(self, telegram_id: int, key: Optional[str] = None) -> None:
        session = await self._repo.get(telegram_id)
        if session is None:
            return
        if key:
            session.context.pop(key, None)
        else:
            session.context = {}
        await self._repo.upsert(session)

    async def set_breadcrumb(self, telegram_id: int, page: str) -> None:
        """Update the current navigation breadcrumb."""
        session = await self.get_or_create(telegram_id)
        session.current_page = page
        await self._repo.upsert(session)

    async def expire(self, telegram_id: int) -> None:
        """Manually expire a session (e.g. on /logout or role change)."""
        await self._repo.expire(telegram_id)

    async def purge_expired(self) -> int:
        """Remove all expired sessions. Call periodically."""
        return await self._repo.purge_expired()

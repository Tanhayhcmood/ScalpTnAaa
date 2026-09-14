"""
Auth Middleware — checks every incoming update against user roles.
Unauthorized users are rejected with a clear message.
Audit log entries created for every access attempt.
"""

import logging
from datetime import datetime
from typing import Optional, Callable, Awaitable, Any
from telegram import Update
from telegram.ext import BaseHandler
from ...config.constants import BotRole
from ...models.user import User, UserPermission
from ...storage.repositories.user_repo import UserRepository
from ...storage.repositories.audit_repo import AuditRepository
from ...models.audit import AuditLog

logger = logging.getLogger(__name__)

# Max failed attempts before blocking
MAX_FAILED_ATTEMPTS = 10


class AuthMiddleware:
    """
    Permission enforcement for all Telegram interactions.
    Owner ID is always granted — cannot be removed.
    """

    def __init__(
        self,
        user_repo: UserRepository,
        audit_repo: AuditRepository,
        owner_id: int,
        admin_ids: Optional[list[int]] = None,
    ) -> None:
        self._user_repo = user_repo
        self._audit_repo = audit_repo
        self._owner_id = owner_id
        self._admin_ids = set(admin_ids or [])
        # In-memory cache for session performance (short TTL)
        self._cache: dict[int, tuple[User, float]] = {}
        self._cache_ttl = 30.0  # seconds

    async def get_user(self, telegram_id: int, tg_user=None) -> Optional[User]:
        """
        Get or create a user record. Auto-promotes owner and pre-configured admins.
        """
        import asyncio
        now = asyncio.get_running_loop().time()
        if telegram_id in self._cache:
            user, ts = self._cache[telegram_id]
            if now - ts < self._cache_ttl:
                # Refresh last_seen in background without awaiting
                return user

        user = await self._user_repo.get_by_telegram_id(telegram_id)

        if user is None and tg_user:
            # Auto-create new user
            role = self._auto_role(telegram_id)
            user = User(
                telegram_id=telegram_id,
                username=tg_user.username,
                first_name=tg_user.first_name or str(telegram_id),
                last_name=tg_user.last_name,
                role=role,
            )
            user = await self._user_repo.upsert(user)
            logger.info(f"Created user {telegram_id} with role {role.value}")
        elif user and tg_user:
            # Update display info
            user.username = tg_user.username
            user.first_name = tg_user.first_name or str(telegram_id)
            user.last_name = tg_user.last_name
            await self._user_repo.upsert(user)

        if user:
            # Ensure owner is always owner
            if telegram_id == self._owner_id and user.role != BotRole.OWNER:
                await self._user_repo.set_role(telegram_id, BotRole.OWNER)
                user.role = BotRole.OWNER
                # Assigning .role directly (instead of reconstructing the
                # User dataclass) does NOT re-run __post_init__, so
                # user.permissions would otherwise stay frozen at whatever
                # role this row had before (e.g. VIEWER) — the owner would
                # then look like an owner everywhere (badge, menus) while
                # every can_* permission check silently returns False for
                # them. Recompute permissions to match the corrected role.
                user.permissions = UserPermission.for_role(BotRole.OWNER)

            # Update cache
            self._cache[telegram_id] = (user, now)
            await self._user_repo.update_last_seen(telegram_id)

        return user

    async def check_permission(
        self,
        update: Update,
        permission_attr: str = "can_view_dashboard",
    ) -> tuple[bool, Optional[User]]:
        """
        Check if the sender has the required permission.
        Returns (allowed, user).
        """
        tg_user = update.effective_user
        if not tg_user:
            return False, None

        user = await self.get_user(tg_user.id, tg_user)
        if user is None:
            return False, None

        if user.is_blocked():
            await self._handle_unauthorized(update, user, "User is blocked")
            return False, user

        allowed = getattr(user.permissions, permission_attr, False)
        if not allowed:
            await self._handle_unauthorized(
                update, user, f"Missing permission: {permission_attr}"
            )
        return allowed, user

    async def is_authorized(self, update: Update) -> tuple[bool, Optional[User]]:
        """Basic check — user exists and is not blocked."""
        return await self.check_permission(update, "can_view_dashboard")

    async def record_action(
        self,
        user: User,
        action: str,
        description: str,
        target: Optional[str] = None,
        old_value: Optional[str] = None,
        new_value: Optional[str] = None,
        success: bool = True,
        error: Optional[str] = None,
    ) -> None:
        log = AuditLog(
            id=None,
            telegram_id=user.telegram_id,
            username=user.username,
            action=action,
            description=description,
            target=target,
            old_value=old_value,
            new_value=new_value,
            success=success,
            error_message=error,
        )
        try:
            await self._audit_repo.log(log)
        except Exception as e:
            logger.error(f"Failed to write audit log: {e}")

    def invalidate_cache(self, telegram_id: int) -> None:
        self._cache.pop(telegram_id, None)

    def _auto_role(self, telegram_id: int) -> BotRole:
        if telegram_id == self._owner_id:
            return BotRole.OWNER
        if telegram_id in self._admin_ids:
            return BotRole.ADMIN
        return BotRole.VIEWER

    async def _handle_unauthorized(
        self, update: Update, user: Optional[User], reason: str
    ) -> None:
        tid = update.effective_user.id if update.effective_user else "unknown"
        logger.warning(f"Unauthorized access: {tid} — {reason}")

        # Audit log
        if user:
            await self.record_action(
                user, "UNAUTHORIZED_ACCESS", reason, success=False, error=reason
            )

        try:
            if update.callback_query:
                await update.callback_query.answer(
                    "⛔ Access denied.", show_alert=True
                )
            elif update.message:
                await update.message.reply_text(
                    "⛔ <b>Access Denied</b>\n"
                    "You do not have permission to perform this action.\n"
                    "Contact the bot owner if you believe this is an error.",
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.debug(f"Could not send unauthorized message: {e}")

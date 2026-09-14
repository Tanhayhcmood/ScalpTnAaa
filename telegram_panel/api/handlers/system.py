"""
System Handler — system stats, logs, users, and audit trail.
"""

import logging
from telegram import Update
from telegram.ext import ContextTypes
from .base import BaseHandler
from ..keyboards.inline import Keyboards
from ..formatters.messages import MessageFormatter
from ...config.constants import BotRole

logger = logging.getLogger(__name__)


class SystemHandler(BaseHandler):
    def __init__(
        self,
        system_service,
        robot_service,
        user_repo,
        audit_repo,
        auth_middleware,
        formatter: MessageFormatter,
        robot_log_path: str = "logs/robot.log",
    ) -> None:
        self._system = system_service
        self._robot = robot_service
        self._user_repo = user_repo
        self._audit_repo = audit_repo
        self._auth = auth_middleware
        self._fmt = formatter
        self._log_path = robot_log_path

    async def show_system(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _ = await self._auth.check_permission(update, "can_access_system")
        if not ok:
            return
        await self.edit_or_reply(
            update, context,
            "💻 <b>SYSTEM PANEL</b>\n\nSelect a category:",
            Keyboards.system_menu(),
        )

    async def show_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _ = await self._auth.check_permission(update, "can_access_system")
        if not ok:
            return
        stats = await self._system.get_full_report()
        text = self._fmt.system_stats(stats)
        await self.edit_or_reply(update, context, text, Keyboards.back_only("nav:system"))

    async def show_logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _ = await self._auth.check_permission(update, "can_view_logs")
        if not ok:
            return
        lines = await self._system.read_logs(self._log_path, lines=50)
        text = self._fmt.logs(lines)
        # Telegram message limit: 4096 chars
        if len(text) > 4000:
            text = text[:3990] + "\n<i>... (truncated)</i>"
        await self.edit_or_reply(update, context, text, Keyboards.back_only("nav:system"))

    async def show_uptime(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _ = await self._auth.check_permission(update, "can_access_system")
        if not ok:
            return
        uptime = await self._system.get_uptime()
        robot_uptime = await self._robot.get_uptime()
        text = (
            f"⏱️ <b>UPTIME</b>\n"
            f"{'─' * 30}\n"
            f"🤖 <b>Panel uptime:</b>  {uptime.get('formatted', '—')}\n"
            f"⚙️ <b>Robot uptime:</b>  {self._fmt_seconds(robot_uptime)}\n"
        )
        await self.edit_or_reply(update, context, text, Keyboards.back_only("nav:system"))

    async def show_network(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _ = await self._auth.check_permission(update, "can_access_system")
        if not ok:
            return
        await self.answer_callback(update, "Pinging...")
        internet = await self._system.get_internet_status()
        latency = await self._system.get_latency()
        lat_str = f"{latency:.1f}ms" if latency else "timeout"
        net_icon = "🟢" if internet else "🔴"
        text = (
            f"🌐 <b>NETWORK STATUS</b>\n"
            f"{'─' * 30}\n"
            f"{net_icon} <b>Internet:</b>  {'Online' if internet else 'Offline'}\n"
            f"⚡ <b>Latency:</b>   {lat_str}\n"
        )
        await self.edit_or_reply(update, context, text, Keyboards.back_only("nav:system"))

    async def show_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _ = await self._auth.check_permission(update, "can_manage_users")
        if not ok:
            return
        users = await self._user_repo.get_all()
        lines = [f"👥 <b>REGISTERED USERS ({len(users)})</b>"]
        for u in users:
            lines.append(f"\n{u.role_icon} <b>{u.display_name}</b>\n  Role: {u.role.value}")
        text = "\n".join(lines)
        await self.edit_or_reply(update, context, text, Keyboards.users_menu(users))

    async def show_user_detail(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, telegram_id: int
    ) -> None:
        ok, _ = await self._auth.check_permission(update, "can_manage_users")
        if not ok:
            return
        user = await self._user_repo.get_by_telegram_id(telegram_id)
        if not user:
            await self.answer_callback(update, "User not found", show_alert=True)
            return
        text = (
            f"👤 <b>{user.display_name}</b>\n"
            f"{'─' * 30}\n"
            f"{user.role_icon} Role: <b>{user.role.value}</b>\n"
            f"✅ Active: {'Yes' if user.is_active else 'No'}\n"
            f"📅 Last seen: {user.last_seen_at.strftime('%Y-%m-%d %H:%M') if user.last_seen_at else '—'}\n"
            f"📅 Created: {user.created_at.strftime('%Y-%m-%d')}\n"
        )
        await self.edit_or_reply(update, context, text, Keyboards.user_role_select(telegram_id))

    async def set_user_role(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        telegram_id: int,
        role_str: str,
    ) -> None:
        ok, actor = await self._auth.check_permission(update, "can_manage_users")
        if not ok:
            return
        try:
            role = BotRole(role_str)
        except ValueError:
            await self.answer_callback(update, "Invalid role", show_alert=True)
            return

        # Owner cannot be demoted via the bot (security)
        if telegram_id == actor.telegram_id and role != BotRole.OWNER:
            await self.answer_callback(update, "Cannot change your own role", show_alert=True)
            return

        success = await self._user_repo.set_role(telegram_id, role)
        self._auth.invalidate_cache(telegram_id)

        await self._auth.record_action(
            actor,
            "USER_ROLE_CHANGE",
            f"Changed user {telegram_id} role to {role.value}",
            success=success,
        )
        await self.answer_callback(update, f"✅ Role set to {role.value}", show_alert=True)
        await self.show_users(update, context)

    async def show_audit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _ = await self._auth.check_permission(update, "can_view_logs")
        if not ok:
            return
        logs = await self._audit_repo.get_recent(limit=20)
        if not logs:
            text = "📋 <b>AUDIT LOG</b>\n\nNo audit entries found."
        else:
            lines = [f"📋 <b>AUDIT LOG (last {len(logs)})</b>"]
            for log in logs:
                lines.append(
                    f"\n{log.result_icon} <code>{log.created_at.strftime('%m-%d %H:%M')}</code> "
                    f"<b>{log.action}</b>\n  {log.description[:80]}"
                )
            text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:3990] + "\n<i>... (truncated)</i>"
        await self.edit_or_reply(update, context, text, Keyboards.back_only("nav:system"))

    def _fmt_seconds(self, seconds: int) -> str:
        d = seconds // 86400
        h = (seconds % 86400) // 3600
        m = (seconds % 3600) // 60
        return f"{d}d {h:02d}h {m:02d}m"

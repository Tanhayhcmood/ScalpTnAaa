"""
Notifications Handler — configure per-type notification settings.
"""

import logging
from telegram import Update
from telegram.ext import ContextTypes
from .base import BaseHandler
from ..keyboards.inline import Keyboards
from ..formatters.messages import MessageFormatter
from ...config.constants import NotificationType

logger = logging.getLogger(__name__)


class NotificationsHandler(BaseHandler):
    def __init__(
        self, notification_service, auth_middleware, formatter: MessageFormatter
    ) -> None:
        self._notif = notification_service
        self._auth = auth_middleware
        self._fmt = formatter

    async def show_notifications(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _ = await self._auth.check_permission(update, "can_view_dashboard")
        if not ok:
            return
        settings = await self._notif.get_settings()
        await self.edit_or_reply(
            update, context,
            "🔔 <b>NOTIFICATION SETTINGS</b>\n\n"
            "Toggle each notification type on or off.\n"
            "Changes take effect immediately.",
            Keyboards.notifications_menu(settings),
        )

    async def toggle_notification(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, ntype_str: str
    ) -> None:
        ok, user = await self._auth.check_permission(update, "can_manage_notifications")
        if not ok:
            return
        try:
            ntype = NotificationType(ntype_str)
        except ValueError:
            await self.answer_callback(update, "Unknown notification type", show_alert=True)
            return

        # Get current setting and toggle
        settings = await self._notif.get_settings()
        settings_map = {s.notification_type: s for s in settings}
        current = settings_map.get(ntype)
        new_state = not current.enabled if current else False

        await self._notif.update_setting(ntype, new_state)
        await self._auth.record_action(
            user, "NOTIF_TOGGLE",
            f"Toggled {ntype.display_name} → {'ON' if new_state else 'OFF'}",
        )
        await self.answer_callback(
            update,
            f"{'🔔' if new_state else '🔕'} {ntype.display_name} {'enabled' if new_state else 'disabled'}",
        )
        await self.show_notifications(update, context)

    async def toggle_all(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, enable: bool
    ) -> None:
        ok, user = await self._auth.check_permission(update, "can_manage_notifications")
        if not ok:
            return
        for ntype in NotificationType:
            await self._notif.update_setting(ntype, enable)
        await self._auth.record_action(
            user, "NOTIF_ALL_TOGGLE",
            f"All notifications {'enabled' if enable else 'disabled'}",
        )
        await self.answer_callback(update, "✅ Done")
        await self.show_notifications(update, context)

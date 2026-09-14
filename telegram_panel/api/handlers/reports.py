"""
Reports Handler — daily, weekly, monthly reports and CSV export.
"""

import logging
from telegram import Update
from telegram.ext import ContextTypes
from .base import BaseHandler
from ..keyboards.inline import Keyboards
from ..formatters.messages import MessageFormatter

logger = logging.getLogger(__name__)


class ReportsHandler(BaseHandler):
    def __init__(
        self, report_service, account_service, auth_middleware, formatter: MessageFormatter
    ) -> None:
        self._reports = report_service
        self._accounts = account_service
        self._auth = auth_middleware
        self._fmt = formatter

    async def show_reports_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _ = await self._auth.check_permission(update, "can_view_reports")
        if not ok:
            return
        await self.edit_or_reply(
            update, context,
            "📋 <b>REPORTS</b>\n\nSelect a report period:",
            Keyboards.reports_menu(),
        )

    async def show_daily(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _ = await self._auth.check_permission(update, "can_view_reports")
        if not ok:
            return
        account = await self._accounts.get_active_account()
        if not account:
            await self.answer_callback(update, "No active account", show_alert=True)
            return
        report = await self._reports.get_daily_report(account.id)
        text = self._fmt.daily_report(report, "Daily")
        await self.edit_or_reply(update, context, text, Keyboards.back_only("nav:reports"))

    async def show_weekly(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _ = await self._auth.check_permission(update, "can_view_reports")
        if not ok:
            return
        account = await self._accounts.get_active_account()
        if not account:
            await self.answer_callback(update, "No active account", show_alert=True)
            return
        report = await self._reports.get_weekly_report(account.id)
        text = self._fmt.daily_report(report, "Weekly")
        await self.edit_or_reply(update, context, text, Keyboards.back_only("nav:reports"))

    async def show_monthly(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _ = await self._auth.check_permission(update, "can_view_reports")
        if not ok:
            return
        account = await self._accounts.get_active_account()
        if not account:
            await self.answer_callback(update, "No active account", show_alert=True)
            return
        report = await self._reports.get_monthly_report(account.id)
        text = self._fmt.daily_report(report, "Monthly")
        await self.edit_or_reply(update, context, text, Keyboards.back_only("nav:reports"))

    async def export_csv(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, period: str
    ) -> None:
        ok, user = await self._auth.check_permission(update, "can_export_reports")
        if not ok:
            return
        await self.answer_callback(update, "📥 Generating CSV...", show_alert=False)
        account = await self._accounts.get_active_account()
        if not account:
            await self.answer_callback(update, "No active account", show_alert=True)
            return
        try:
            csv_bytes, filename = await self._reports.export_to_csv(account.id, period)
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=csv_bytes,
                filename=filename,
                caption=f"📊 {period.capitalize()} trade report — {filename}",
            )
            await self._auth.record_action(
                user, "REPORT_EXPORT", f"Exported {period} CSV: {filename}"
            )
        except Exception as e:
            logger.error(f"Failed to export CSV: {e}")
            await self.send_message(
                update, context, self._fmt.error(f"Export failed: {e}")
            )

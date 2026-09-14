"""
Risk Handler — view and update risk management parameters.
"""

import logging
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler
from .base import BaseHandler
from ..keyboards.inline import Keyboards
from ..formatters.messages import MessageFormatter
from ...config.constants import RiskParameter

logger = logging.getLogger(__name__)

# State for entering new value
ASK_VALUE = 0


class RiskHandler(BaseHandler):
    def __init__(self, risk_service, auth_middleware, formatter: MessageFormatter) -> None:
        self._risk = risk_service
        self._auth = auth_middleware
        self._fmt = formatter

    async def show_risk(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _ = await self._auth.check_permission(update, "can_view_dashboard")
        if not ok:
            return
        config = await self._risk.get_config()
        text = self._fmt.risk_config(config)
        await self.edit_or_reply(update, context, text, Keyboards.risk_menu())

    async def view_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _ = await self._auth.check_permission(update, "can_view_dashboard")
        if not ok:
            return
        config = await self._risk.get_config()
        text = self._fmt.risk_config(config)
        await self.edit_or_reply(update, context, text, Keyboards.back_only("nav:risk"))

    async def start_edit_parameter(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, param: str
    ) -> int:
        ok, _ = await self._auth.check_permission(update, "can_change_risk")
        if not ok:
            return ConversationHandler.END

        try:
            risk_param = RiskParameter(param)
        except ValueError:
            await self.answer_callback(update, "Unknown parameter", show_alert=True)
            return ConversationHandler.END

        config = await self._risk.get_config()
        current = getattr(config, risk_param.value.replace("auto_", "") if "auto_" in risk_param.value else risk_param.value, "?")

        context.user_data["editing_risk"] = risk_param
        await self.edit_or_reply(
            update, context,
            f"✏️ <b>Edit: {risk_param.value.replace('_', ' ').title()}</b>\n\n"
            f"Current value: <code>{current}</code>\n\n"
            f"Enter the new value:",
            None,
        )
        return ASK_VALUE

    async def receive_value(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        ok, user = await self._auth.check_permission(update, "can_change_risk")
        if not ok:
            return ConversationHandler.END

        risk_param: RiskParameter = context.user_data.get("editing_risk")
        if not risk_param:
            return ConversationHandler.END

        raw = self.get_text(update).strip()
        try:
            # Booleans
            if risk_param in (RiskParameter.AUTO_BE, RiskParameter.AUTO_TRAIL):
                value = raw.lower() in ("1", "true", "yes", "on")
            else:
                value = float(raw)
        except ValueError:
            await update.message.reply_text("❌ Invalid value. Please enter a number.")
            return ASK_VALUE

        config_before = await self._risk.get_config()
        old_val = getattr(config_before, risk_param.value, None)

        success, message = await self._risk.update_parameter(risk_param, value)

        await self._auth.record_action(
            user,
            "RISK_UPDATE",
            f"Updated {risk_param.value} to {value}",
            target=risk_param.value,
            old_value=str(old_val),
            new_value=str(value),
            success=success,
        )

        await update.message.reply_text(message, parse_mode="HTML")
        context.user_data.pop("editing_risk", None)

        # Return to risk menu
        config = await self._risk.get_config()
        await self.send_message(
            update, context, self._fmt.risk_config(config), Keyboards.risk_menu()
        )
        return ConversationHandler.END

    async def toggle_bool(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, param: str
    ) -> None:
        ok, user = await self._auth.check_permission(update, "can_change_risk")
        if not ok:
            return
        try:
            risk_param = RiskParameter(param)
        except ValueError:
            return
        config = await self._risk.get_config()
        current = getattr(config, risk_param.value, False)
        new_val = not bool(current)
        success, message = await self._risk.update_parameter(risk_param, new_val)
        await self._auth.record_action(
            user, "RISK_TOGGLE", f"Toggled {param} → {new_val}", success=success
        )
        await self.answer_callback(update, message[:100])
        await self.show_risk(update, context)

"""
Strategy Handler — enable/disable SMC strategy components.
"""

import logging
from telegram import Update
from telegram.ext import ContextTypes
from .base import BaseHandler
from ..keyboards.inline import Keyboards
from ..formatters.messages import MessageFormatter
from ...config.constants import StrategyComponent

logger = logging.getLogger(__name__)


class StrategyHandler(BaseHandler):
    def __init__(self, strategy_service, auth_middleware, formatter: MessageFormatter) -> None:
        self._strategy = strategy_service
        self._auth = auth_middleware
        self._fmt = formatter

    async def show_strategy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _ = await self._auth.check_permission(update, "can_view_dashboard")
        if not ok:
            return
        config = await self._strategy.get_config()
        text = self._fmt.strategy_config(config)
        await self.edit_or_reply(update, context, text, Keyboards.strategy_menu(config))

    async def toggle_component(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, component_str: str
    ) -> None:
        ok, user = await self._auth.check_permission(update, "can_change_strategy")
        if not ok:
            return
        try:
            component = StrategyComponent(component_str)
        except ValueError:
            await self.answer_callback(update, "Unknown component", show_alert=True)
            return

        config = await self._strategy.get_config()
        current = config.is_component_enabled(component)
        new_state = not current

        success, message = await self._strategy.toggle_component(component, new_state)
        await self._auth.record_action(
            user,
            "STRATEGY_TOGGLE",
            f"Toggled {component.display_name} → {'ON' if new_state else 'OFF'}",
            success=success,
        )
        await self.answer_callback(update, message[:100])
        await self.show_strategy(update, context)

    async def toggle_all(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, enable: bool
    ) -> None:
        ok, user = await self._auth.check_permission(update, "can_change_strategy")
        if not ok:
            return
        success, message = await self._strategy.toggle_all(enable)
        await self._auth.record_action(
            user, "STRATEGY_ALL_TOGGLE",
            f"All strategy components {'enabled' if enable else 'disabled'}",
            success=success,
        )
        await self.answer_callback(update, message[:100])
        await self.show_strategy(update, context)

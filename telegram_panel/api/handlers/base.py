"""
Base Handler — shared utilities for all Telegram handlers.
Provides safe message editing, navigation helpers, and context injection.
"""

import logging
from typing import Optional, Any
from telegram import Update, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import TelegramError, BadRequest

logger = logging.getLogger(__name__)


class BaseHandler:
    """
    Base class for all panel handlers.
    Provides common methods for safe message editing and navigation.
    """

    async def edit_or_reply(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
        keyboard: Optional[InlineKeyboardMarkup] = None,
        parse_mode: str = "HTML",
    ) -> None:
        """Edit the current message (callback) or send a new one (message)."""
        try:
            if update.callback_query:
                await update.callback_query.answer()
                try:
                    await update.callback_query.edit_message_text(
                        text=text,
                        reply_markup=keyboard,
                        parse_mode=parse_mode,
                    )
                except BadRequest as e:
                    if "Message is not modified" in str(e):
                        pass  # Ignore unchanged messages
                    else:
                        raise
            elif update.message:
                await update.message.reply_text(
                    text=text,
                    reply_markup=keyboard,
                    parse_mode=parse_mode,
                )
        except TelegramError as e:
            logger.error(f"Failed to edit/reply message: {e}")

    async def answer_callback(
        self,
        update: Update,
        text: str = "",
        show_alert: bool = False,
    ) -> None:
        """Answer callback query silently or with an alert."""
        try:
            if update.callback_query:
                await update.callback_query.answer(text=text, show_alert=show_alert)
        except TelegramError as e:
            logger.debug(f"Failed to answer callback: {e}")

    async def send_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
        keyboard: Optional[InlineKeyboardMarkup] = None,
        parse_mode: str = "HTML",
    ) -> None:
        """Always send a new message, regardless of update type."""
        try:
            chat_id = update.effective_chat.id
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=keyboard,
                parse_mode=parse_mode,
            )
        except TelegramError as e:
            logger.error(f"Failed to send message: {e}")

    def get_callback_data(self, update: Update) -> str:
        """Extract callback data safely."""
        if update.callback_query:
            return update.callback_query.data or ""
        return ""

    def get_text(self, update: Update) -> str:
        """Extract text from message or callback."""
        if update.message and update.message.text:
            return update.message.text.strip()
        return ""

    def get_user_id(self, update: Update) -> int:
        if update.effective_user:
            return update.effective_user.id
        return 0

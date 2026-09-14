"""
Accounts Handler — add, remove, switch, and monitor broker accounts.
Multi-step conversation for adding new accounts.
"""

import logging
from telegram import Update, ForceReply
from telegram.ext import ContextTypes, ConversationHandler
from .base import BaseHandler
from ..keyboards.inline import Keyboards
from ..formatters.messages import MessageFormatter
from ...config.constants import AccountType

logger = logging.getLogger(__name__)

# Conversation states
(
    ASK_NAME, ASK_TYPE, ASK_BROKER, ASK_SERVER,
    ASK_LOGIN, ASK_PASSWORD, CONFIRM_ADD,
) = range(7)


class AccountsHandler(BaseHandler):
    def __init__(self, account_service, auth_middleware, formatter: MessageFormatter) -> None:
        self._accounts = account_service
        self._auth = auth_middleware
        self._fmt = formatter

    async def show_accounts(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, user = await self._auth.check_permission(update, "can_view_accounts")
        if not ok:
            if update.callback_query:
                try:
                    await update.callback_query.answer()
                except Exception:
                    pass
            return
        accounts = await self._accounts.get_all_accounts()
        text = self._fmt.account_list(accounts)
        await self.edit_or_reply(update, context, text, Keyboards.account_list(accounts))

    async def show_account_detail(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, account_id: int
    ) -> None:
        ok, user = await self._auth.check_permission(update, "can_view_accounts")
        if not ok:
            return
        account = await self._accounts.get_account(account_id)
        if not account:
            await self.answer_callback(update, "Account not found", show_alert=True)
            return
        text = self._fmt.account_detail(account)
        await self.edit_or_reply(update, context, text, Keyboards.account_detail(account))

    async def start_add_account(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        ok, user = await self._auth.check_permission(update, "can_manage_accounts")
        if not ok:
            return ConversationHandler.END
        context.user_data["new_account"] = {}
        await self.edit_or_reply(
            update, context,
            "➕ <b>Add New Account</b>\n\n"
            "Step 1/6: Enter a <b>display name</b> for this account.\n"
            "Example: <code>ICMarkets Real #1</code>",
            None,
        )
        return ASK_NAME

    async def receive_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        name = self.get_text(update).strip()
        if not name:
            await update.message.reply_text("Please enter a valid account name.")
            return ASK_NAME
        context.user_data["new_account"]["name"] = name
        await update.message.reply_text(
            f"✅ Name: <b>{name}</b>\n\n"
            "Step 2/6: Select <b>account type</b>:",
            reply_markup=Keyboards.account_type_select(),
            parse_mode="HTML",
        )
        return ASK_TYPE

    async def receive_type(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, acc_type: str
    ) -> int:
        try:
            context.user_data["new_account"]["account_type"] = AccountType(acc_type)
        except ValueError:
            await self.answer_callback(update, "Invalid account type", show_alert=True)
            return ASK_TYPE

        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            f"✅ Type: <b>{acc_type.upper()}</b>\n\n"
            "Step 3/6: Enter the <b>broker name</b>.\n"
            "Example: <code>ICMarkets</code>",
            parse_mode="HTML",
        )
        return ASK_BROKER

    async def receive_broker(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        broker = self.get_text(update).strip()
        if not broker:
            await update.message.reply_text("Please enter a valid broker name.")
            return ASK_BROKER
        context.user_data["new_account"]["broker"] = broker
        await update.message.reply_text(
            f"✅ Broker: <b>{broker}</b>\n\n"
            "Step 4/6: Enter the MT5 <b>server name</b>.\n"
            "Example: <code>ICMarkets-Demo02</code>",
            parse_mode="HTML",
        )
        return ASK_SERVER

    async def receive_server(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        server = self.get_text(update).strip()
        if not server:
            await update.message.reply_text("Please enter a valid server name.")
            return ASK_SERVER
        context.user_data["new_account"]["server"] = server
        await update.message.reply_text(
            f"✅ Server: <b>{server}</b>\n\n"
            "Step 5/6: Enter your MT5 <b>account number (login)</b>.",
            parse_mode="HTML",
        )
        return ASK_LOGIN

    async def receive_login(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        login = self.get_text(update).strip()
        if not login:
            await update.message.reply_text("Please enter a valid login number.")
            return ASK_LOGIN
        context.user_data["new_account"]["login"] = login
        await update.message.reply_text(
            f"✅ Login: <code>{login}</code>\n\n"
            "Step 6/6: Enter your MT5 <b>password</b>.\n\n"
            "⚠️ <i>This will be stored encrypted. Delete your message after sending.</i>",
            parse_mode="HTML",
        )
        return ASK_PASSWORD

    async def receive_password(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        password = self.get_text(update).strip()
        if not password:
            await update.message.reply_text("Please enter a valid password.")
            return ASK_PASSWORD

        # Delete the password message for security
        try:
            await update.message.delete()
        except Exception:
            pass

        data = context.user_data.get("new_account", {})
        summary = (
            f"📋 <b>Confirm New Account</b>\n\n"
            f"📛 Name: <b>{data.get('name')}</b>\n"
            f"📋 Type: <b>{data.get('account_type', '').value if data.get('account_type') else ''}</b>\n"
            f"🏦 Broker: <b>{data.get('broker')}</b>\n"
            f"📡 Server: <b>{data.get('server')}</b>\n"
            f"👤 Login: <code>{data.get('login')}</code>\n"
            f"🔒 Password: <b>[encrypted]</b>\n\n"
            f"Is this correct?"
        )
        context.user_data["new_account"]["password"] = password
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=summary,
            parse_mode="HTML",
            reply_markup=Keyboards.confirm_cancel(
                "accounts:confirm_add", "accounts:cancel_add"
            ),
        )
        return CONFIRM_ADD

    async def confirm_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        ok, user = await self._auth.check_permission(update, "can_manage_accounts")
        if not ok:
            return ConversationHandler.END

        data = context.user_data.get("new_account", {})
        try:
            account = await self._accounts.add_account(
                name=data["name"],
                account_type=data["account_type"],
                broker=data["broker"],
                server=data["server"],
                login=data["login"],
                password=data["password"],
            )
            await self._auth.record_action(
                user, "ACCOUNT_ADD", f"Added account: {account.name}"
            )
            await self.answer_callback(update, "✅ Account added!", show_alert=False)
        except Exception as e:
            logger.error(f"Failed to add account: {e}")
            await self.answer_callback(update, f"❌ Failed: {e}", show_alert=True)

        context.user_data.pop("new_account", None)
        await self.show_accounts(update, context)
        return ConversationHandler.END

    async def cancel_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data.pop("new_account", None)
        await self.answer_callback(update, "Cancelled", show_alert=False)
        await self.show_accounts(update, context)
        return ConversationHandler.END

    async def delete_account(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, account_id: int
    ) -> None:
        ok, user = await self._auth.check_permission(update, "can_manage_accounts")
        if not ok:
            return
        account = await self._accounts.get_account(account_id)
        if account:
            await self.edit_or_reply(
                update, context,
                f"🗑️ <b>Delete Account</b>\n\nDelete <b>{account.name}</b>?\n"
                "This cannot be undone.",
                Keyboards.confirm_cancel(
                    f"accounts:delete_confirmed:{account_id}",
                    f"accounts:detail:{account_id}",
                ),
            )

    async def delete_account_confirmed(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, account_id: int
    ) -> None:
        ok, user = await self._auth.check_permission(update, "can_manage_accounts")
        if not ok:
            return
        account = await self._accounts.get_account(account_id)
        success = await self._accounts.delete_account(account_id)
        if success and account:
            await self._auth.record_action(
                user, "ACCOUNT_DELETE", f"Deleted account: {account.name}"
            )
        await self.answer_callback(update, "✅ Deleted" if success else "❌ Failed", show_alert=True)
        await self.show_accounts(update, context)

    async def switch_account(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, account_id: int
    ) -> None:
        ok, user = await self._auth.check_permission(update, "can_manage_accounts")
        if not ok:
            return
        success = await self._accounts.switch_account(account_id)
        await self._auth.record_action(
            user, "ACCOUNT_SWITCH", f"Switched to account #{account_id}", success=success
        )
        await self.answer_callback(update, "✅ Switched" if success else "❌ Failed")
        await self.show_accounts(update, context)

    async def toggle_account(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        account_id: int, enable: bool
    ) -> None:
        ok, user = await self._auth.check_permission(update, "can_manage_accounts")
        if not ok:
            return
        if enable:
            success = await self._accounts.enable_account(account_id)
        else:
            success = await self._accounts.disable_account(account_id)
        action = "ENABLE" if enable else "DISABLE"
        await self._auth.record_action(
            user, f"ACCOUNT_{action}", f"Account #{account_id}", success=success
        )
        await self.answer_callback(update, "✅ Done" if success else "❌ Failed")
        await self.show_account_detail(update, context, account_id)

    async def reconnect_account(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, account_id: int
    ) -> None:
        """Send RECONNECT command via the account service → robot."""
        ok, _ = await self._auth.check_permission(update, "can_manage_accounts")
        if not ok:
            return
        result = await self._accounts.reconnect(account_id)
        await self.answer_callback(
            update,
            "✅ Reconnect command sent" if result else "❌ Failed to send reconnect",
            show_alert=True,
        )

    async def test_connection(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, account_id: int
    ) -> None:
        ok, _ = await self._auth.check_permission(update, "can_view_accounts")
        if not ok:
            return
        await self.answer_callback(update, "🔍 Testing live connection...")
        result = await self._accounts.test_connection(account_id)
        checked = result.get("checked_at", "")
        hb = result.get("last_heartbeat", "—")
        age = result.get("data_age_seconds", -1)

        if result.get("success"):
            bal = result.get("balance", 0.0)
            cur = result.get("currency", "USD")
            robot_st = result.get("robot_status", "running").upper()
            msg = (
                f"✅ Connected\n"
                f"Robot: {robot_st}\n"
                f"MT5: CONNECTED\n"
                f"Balance: {cur} {bal:,.2f}\n"
                f"Heartbeat: {hb}\n"
                f"Checked: {checked}"
            )
        else:
            robot_alive = result.get("robot_alive", False)
            robot_st = result.get("robot_status", "unknown").upper()
            mt5_st = result.get("mt5_status", "unknown").upper()
            err = result.get("error", "Unknown error")
            age_str = f"{age}s ago" if age >= 0 else "never"
            if robot_alive:
                msg = (
                    f"⚠️ Robot Online but MT5 Disconnected\n"
                    f"Robot: {robot_st}\n"
                    f"MT5: {mt5_st}\n"
                    f"Heartbeat: {hb}\n"
                    f"Error: {err}\n"
                    f"Checked: {checked}"
                )
            else:
                msg = (
                    f"❌ Robot Offline\n"
                    f"Last seen: {age_str}\n"
                    f"Status: {robot_st}\n"
                    f"Error: {err}\n"
                    f"Checked: {checked}"
                )
        await self.answer_callback(update, msg, show_alert=True)

"""
Router — registers all Telegram handlers with the Application.
Central dispatcher that maps callback_data patterns to handler methods.
Uses a single CallbackQueryHandler with pattern routing (no handler sprawl).
"""

import logging
import re
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
from .handlers.dashboard import DashboardHandler
from .handlers.accounts import AccountsHandler, ASK_NAME, ASK_TYPE, ASK_BROKER, ASK_SERVER, ASK_LOGIN, ASK_PASSWORD, CONFIRM_ADD
from .handlers.trading import TradingHandler
from .handlers.risk import RiskHandler, ASK_VALUE
from .handlers.strategy import StrategyHandler
from .handlers.reports import ReportsHandler
from .handlers.notifications_handler import NotificationsHandler
from .handlers.system import SystemHandler
from .middleware.auth import AuthMiddleware
from .middleware.rate_limiter import RateLimiter
from .formatters.messages import MessageFormatter
from .keyboards.inline import Keyboards

logger = logging.getLogger(__name__)


class Router:
    """
    Central message and callback router.
    All routing decisions live here — handlers have zero routing logic.
    """

    def __init__(
        self,
        dashboard: DashboardHandler,
        accounts: AccountsHandler,
        trading: TradingHandler,
        risk: RiskHandler,
        strategy: StrategyHandler,
        reports: ReportsHandler,
        notifications: NotificationsHandler,
        system: SystemHandler,
        auth: AuthMiddleware,
        rate_limiter: RateLimiter,
        formatter: MessageFormatter,
    ) -> None:
        self._dashboard = dashboard
        self._accounts = accounts
        self._trading = trading
        self._risk = risk
        self._strategy = strategy
        self._reports = reports
        self._notifications = notifications
        self._system = system
        self._auth = auth
        self._rl = rate_limiter
        self._fmt = formatter

    def register(self, app: Application) -> None:
        """Register all handlers with the Telegram Application."""

        # /start command
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("menu", self._cmd_start))
        app.add_handler(CommandHandler("dashboard", self._cmd_dashboard))
        app.add_handler(CommandHandler("status", self._cmd_dashboard))
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CommandHandler("history", self._cmd_history))

        # Add account conversation
        add_account_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self._start_add_account, pattern="^accounts:add$")],
            states={
                ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._accounts.receive_name)],
                ASK_TYPE: [CallbackQueryHandler(self._recv_account_type, pattern="^accounts:type:")],
                ASK_BROKER: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._accounts.receive_broker)],
                ASK_SERVER: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._accounts.receive_server)],
                ASK_LOGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._accounts.receive_login)],
                ASK_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._accounts.receive_password)],
                CONFIRM_ADD: [
                    CallbackQueryHandler(self._accounts.confirm_add, pattern="^accounts:confirm_add$"),
                    CallbackQueryHandler(self._accounts.cancel_add, pattern="^accounts:cancel_add$"),
                ],
            },
            fallbacks=[CallbackQueryHandler(self._accounts.cancel_add, pattern="^accounts:cancel_add$")],
            per_user=True,
            per_chat=True,
        )
        app.add_handler(add_account_conv)

        # Risk edit conversation
        risk_edit_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self._start_risk_edit, pattern="^risk:edit:")],
            states={
                ASK_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._risk.receive_value)],
            },
            fallbacks=[CommandHandler("cancel", self._cancel_conversation)],
            per_user=True,
            per_chat=True,
        )
        app.add_handler(risk_edit_conv)

        # Main callback router (catch-all)
        app.add_handler(CallbackQueryHandler(self._route_callback))

        # Unknown command fallback
        app.add_handler(MessageHandler(filters.COMMAND, self._unknown_command))

        # Error handler
        app.add_error_handler(self._error_handler)

        logger.info("All handlers registered successfully")

    # ─── Commands ────────────────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._rate_check(update):
            return
        await self._dashboard.show_home(update, context)

    async def _cmd_dashboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._rate_check(update):
            return
        await self._dashboard.show_dashboard(update, context)

    async def _cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show recent trade history via /history command."""
        if not await self._rate_check(update):
            return
        await self._trading.show_trade_history(update, context)

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._rate_check(update):
            return
        _, user = await self._auth.is_authorized(update)
        if not user:
            return
        text = (
            "🤖 <b>GoldScalperPro Control Panel</b>\n\n"
            "<b>Commands:</b>\n"
            "/start — Main menu\n"
            "/dashboard — Dashboard view\n"
            "/menu — Main menu\n"
            "/status — Quick status\n"
            "/history — Recent closed trades\n"
            "/help — This message\n\n"
            f"Your role: <b>{user.role_icon} {user.role.value}</b>"
        )
        if update.message:
            await update.message.reply_text(text, parse_mode="HTML")

    async def _unknown_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message:
            await update.message.reply_text(
                "Unknown command. Use /start for the main menu."
            )

    # ─── Main Callback Router ────────────────────────────────────────────────

    async def _route_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._rate_check(update):
            return

        data = update.callback_query.data if update.callback_query else ""
        if not data:
            return

        user_id = update.effective_user.id if update.effective_user else "?"
        logger.info(f"[BTN] user={user_id} data={data!r}")
        parts = data.split(":")

        try:
            section = parts[0]

            # ── Navigation ──────────────────────────────────────────────
            if section == "nav":
                dest = parts[1] if len(parts) > 1 else "home"
                if dest in ("home", "refresh_home"):
                    await self._dashboard.show_home(update, context)
                elif dest == "dashboard":
                    await self._dashboard.show_dashboard(update, context)
                elif dest == "accounts":
                    await self._accounts.show_accounts(update, context)
                elif dest == "trading":
                    await self._trading.show_trading(update, context)
                elif dest == "risk":
                    await self._risk.show_risk(update, context)
                elif dest == "strategy":
                    await self._strategy.show_strategy(update, context)
                elif dest == "reports":
                    await self._reports.show_reports_menu(update, context)
                elif dest == "notifications":
                    await self._notifications.show_notifications(update, context)
                elif dest == "settings":
                    await self._show_settings(update, context)
                elif dest == "system":
                    await self._system.show_system(update, context)
                elif dest == "news":
                    await update.callback_query.edit_message_text(
                        "📰 <b>NEWS</b>\n\nNews feed is not yet available.",
                        reply_markup=Keyboards.back_only("nav:home"),
                        parse_mode="HTML",
                    )
                    await update.callback_query.answer()

            # ── Dashboard / Robot Control ────────────────────────────────
            elif section == "dashboard":
                action = parts[1] if len(parts) > 1 else "refresh"
                if action == "refresh":
                    await self._dashboard.show_dashboard(update, context)

            elif section == "robot":
                action = parts[1] if len(parts) > 1 else ""
                await self._dashboard.handle_robot_control(update, context, action)

            # ── Accounts ────────────────────────────────────────────────
            elif section == "accounts":
                action = parts[1] if len(parts) > 1 else ""
                param = parts[2] if len(parts) > 2 else ""

                if action == "list":
                    await self._accounts.show_accounts(update, context)
                elif action == "detail" and param:
                    await self._accounts.show_account_detail(update, context, int(param))
                elif action == "enable" and param:
                    await self._accounts.toggle_account(update, context, int(param), True)
                elif action == "disable" and param:
                    await self._accounts.toggle_account(update, context, int(param), False)
                elif action == "switch" and param:
                    await self._accounts.switch_account(update, context, int(param))
                elif action == "delete_confirm" and param:
                    await self._accounts.delete_account(update, context, int(param))
                elif action == "delete_confirmed" and param:
                    await self._accounts.delete_account_confirmed(update, context, int(param))
                elif action == "reconnect" and param:
                    await self._accounts.reconnect_account(update, context, int(param))
                elif action == "test" and param:
                    await self._accounts.test_connection(update, context, int(param))

            # ── Trading ─────────────────────────────────────────────────
            elif section == "trading":
                action = parts[1] if len(parts) > 1 else ""
                param = parts[2] if len(parts) > 2 else ""

                if action == "menu":
                    await self._trading.show_trading(update, context)
                elif action == "positions":
                    await self._trading.show_positions(update, context)
                elif action == "position_detail" and param:
                    await self._trading.show_position_detail(update, context, int(param))
                elif action == "pending":
                    await self._trading.show_pending(update, context)
                elif action == "history":
                    limit = int(param) if param else 20
                    await self._trading.show_trade_history(update, context, limit)
                elif action == "close_confirm" and param:
                    await self._trading.close_position_confirm(update, context, int(param))
                elif action == "close_confirmed" and param:
                    await self._trading.close_position(update, context, int(param))
                elif action == "breakeven" and param:
                    await self._trading.set_breakeven(update, context, int(param))
                elif action in ("close_all_confirm", "close_buy_confirm",
                                "close_sell_confirm", "close_profit_confirm",
                                "close_loss_confirm"):
                    close_type = action.replace("close_", "").replace("_confirm", "")
                    await self._trading.close_all_confirm(update, context, close_type)
                elif action in ("close_all_confirmed", "close_buy_confirmed",
                                "close_sell_confirmed", "close_profit_confirmed",
                                "close_loss_confirmed"):
                    close_type = action.replace("close_", "").replace("_confirmed", "")
                    await self._trading.close_bulk(update, context, close_type)

                elif action == "partial_close" and param:
                    await self._trading.handle_partial_close(update, context, int(param))
                elif action == "partial_exec" and param:
                    pct = float(parts[3]) if len(parts) > 3 else 50.0
                    await self._trading.execute_partial_close(update, context, int(param), pct)
                elif action == "move_sl" and param:
                    await self._trading.handle_move_sl(update, context, int(param))
                elif action == "sl_exec" and param:
                    pts = float(parts[3]) if len(parts) > 3 else 0.0
                    await self._trading.execute_move_sl(update, context, int(param), pts)
                elif action == "move_tp" and param:
                    await self._trading.handle_move_tp(update, context, int(param))
                elif action == "tp_exec" and param:
                    pts = float(parts[3]) if len(parts) > 3 else 0.0
                    await self._trading.execute_move_tp(update, context, int(param), pts)
                elif action == "trail" and param:
                    await self._trading.handle_trail(update, context, int(param))
                elif action == "trail_exec" and param:
                    pts = float(parts[3]) if len(parts) > 3 else 50.0
                    await self._trading.execute_trail(update, context, int(param), pts)

            elif section == "risk":
                action = parts[1] if len(parts) > 1 else ""
                param = parts[2] if len(parts) > 2 else ""

                if action == "view":
                    await self._risk.view_config(update, context)
                elif action == "toggle" and param:
                    await self._risk.toggle_bool(update, context, param)

            # ── Strategy ────────────────────────────────────────────────
            elif section == "strategy":
                action = parts[1] if len(parts) > 1 else ""
                param = parts[2] if len(parts) > 2 else ""

                if action == "toggle" and param:
                    await self._strategy.toggle_component(update, context, param)
                elif action == "all_on":
                    await self._strategy.toggle_all(update, context, True)
                elif action == "all_off":
                    await self._strategy.toggle_all(update, context, False)

            # ── Reports ─────────────────────────────────────────────────
            elif section == "reports":
                action = parts[1] if len(parts) > 1 else ""
                param = parts[2] if len(parts) > 2 else ""

                if action == "daily":
                    await self._reports.show_daily(update, context)
                elif action == "weekly":
                    await self._reports.show_weekly(update, context)
                elif action == "monthly":
                    await self._reports.show_monthly(update, context)
                elif action == "history":
                    await self._trading.show_trade_history(update, context, 20)
                elif action == "export" and param:
                    await self._reports.export_csv(update, context, param)

            # ── Notifications ───────────────────────────────────────────
            elif section == "notif":
                action = parts[1] if len(parts) > 1 else ""
                param = parts[2] if len(parts) > 2 else ""

                if action == "toggle" and param:
                    await self._notifications.toggle_notification(update, context, param)
                elif action == "all_on":
                    await self._notifications.toggle_all(update, context, True)
                elif action == "all_off":
                    await self._notifications.toggle_all(update, context, False)

            # ── System ──────────────────────────────────────────────────
            elif section == "system":
                action = parts[1] if len(parts) > 1 else ""
                p1 = parts[2] if len(parts) > 2 else ""
                p2 = parts[3] if len(parts) > 3 else ""

                if action == "stats" or action == "refresh":
                    await self._system.show_stats(update, context)
                elif action == "logs":
                    await self._system.show_logs(update, context)
                elif action == "uptime":
                    await self._system.show_uptime(update, context)
                elif action == "network":
                    await self._system.show_network(update, context)
                elif action == "users":
                    await self._system.show_users(update, context)
                elif action == "user_detail" and p1:
                    await self._system.show_user_detail(update, context, int(p1))
                elif action == "set_role" and p1 and p2:
                    await self._system.set_user_role(update, context, int(p1), p2)
                elif action == "audit":
                    await self._system.show_audit(update, context)
                elif action == "menu":
                    await self._system.show_system(update, context)

            # ── Settings ────────────────────────────────────────────────
            elif section == "settings":
                action = parts[1] if len(parts) > 1 else ""
                if action == "token":
                    await update.callback_query.edit_message_text(
                        "🔑 <b>CHANGE BOT TOKEN</b>\n\n"
                        "Set <code>TELEGRAM_BOT_TOKEN</code> in your Render environment variables, "
                        "then redeploy the panel service.",
                        reply_markup=Keyboards.back_only("nav:settings"),
                        parse_mode="HTML",
                    )
                    await update.callback_query.answer()
                elif action == "admins":
                    await update.callback_query.edit_message_text(
                        "👥 <b>MANAGE ADMINS</b>\n\n"
                        "Add admin Telegram IDs to <code>TELEGRAM_ADMIN_IDS</code> "
                        "(comma-separated) in your Render environment variables, "
                        "then redeploy the panel service.",
                        reply_markup=Keyboards.back_only("nav:settings"),
                        parse_mode="HTML",
                    )
                    await update.callback_query.answer()
                elif action == "session_timeout":
                    await update.callback_query.edit_message_text(
                        "⏱️ <b>SESSION TIMEOUT</b>\n\n"
                        "Configure session timeout in "
                        "<code>telegram_panel/config/panel.json</code> under "
                        "<code>security.session_timeout_minutes</code>.",
                        reply_markup=Keyboards.back_only("nav:settings"),
                        parse_mode="HTML",
                    )
                    await update.callback_query.answer()
                elif action == "gen_key":
                    from cryptography.fernet import Fernet
                    key = Fernet.generate_key().decode()
                    await update.callback_query.edit_message_text(
                        f"🔒 <b>NEW ENCRYPTION KEY</b>\n\n"
                        f"<code>{key}</code>\n\n"
                        f"⚠️ Set this as <code>PANEL_ENCRYPTION_KEY</code> in Render and redeploy. "
                        f"<b>Store it securely — losing it means losing access to encrypted credentials.</b>",
                        reply_markup=Keyboards.back_only("nav:settings"),
                        parse_mode="HTML",
                    )
                    await update.callback_query.answer()
                else:
                    await self._show_settings(update, context)

            else:
                logger.debug(f"Unhandled callback: {data}")
                await update.callback_query.answer()

        except (ValueError, IndexError) as e:
            logger.warning(f"[BTN ERR] user={user_id} data={data!r} malformed: {e}")
            await update.callback_query.answer("Invalid action", show_alert=True)
        except Exception as e:
            logger.error(
                f"[BTN EXCEPTION] user={user_id} data={data!r} error={e}",
                exc_info=True,
            )
            await update.callback_query.answer("An error occurred", show_alert=True)
        else:
            logger.info(f"[BTN OK] user={user_id} data={data!r}")

    # ─── Conversation Entry Points ───────────────────────────────────────────

    async def _start_add_account(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        if not await self._rate_check(update):
            return ConversationHandler.END
        return await self._accounts.start_add_account(update, context)

    async def _recv_account_type(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        data = update.callback_query.data
        acc_type = data.split(":")[-1]
        return await self._accounts.receive_type(update, context, acc_type)

    async def _start_risk_edit(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        if not await self._rate_check(update):
            return ConversationHandler.END
        param = update.callback_query.data.split(":")[-1]
        return await self._risk.start_edit_parameter(update, context, param)

    async def _cancel_conversation(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        await self._dashboard.show_home(update, context)
        return ConversationHandler.END

    # ─── Settings (simple placeholder panel) ────────────────────────────────

    async def _show_settings(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        from .keyboards.inline import Keyboards
        _, user = await self._auth.is_authorized(update)
        if not user or not user.permissions.can_manage_users:
            await update.callback_query.answer("⛔ Access denied", show_alert=True)
            return
        text = (
            "⚙️ <b>SETTINGS</b>\n\n"
            "Panel configuration is managed via environment variables\n"
            "and <code>telegram_panel/config/panel.json</code>.\n\n"
            "<b>Current session:</b>\n"
            f"👤 User: {user.display_name}\n"
            f"{user.role_icon} Role: {user.role.value}\n\n"
            "<i>See README.md for full configuration guide.</i>"
        )
        await update.callback_query.edit_message_text(
            text, reply_markup=Keyboards.settings_menu(), parse_mode="HTML"
        )
        await update.callback_query.answer()

    # ─── Helpers ─────────────────────────────────────────────────────────────

    async def _rate_check(self, update: Update) -> bool:
        """Returns False and warns user if rate limited."""
        user_id = update.effective_user.id if update.effective_user else 0
        if not await self._rl.check(user_id):
            try:
                if update.callback_query:
                    await update.callback_query.answer(
                        "⏱️ Too many requests. Please slow down.", show_alert=True
                    )
                elif update.message:
                    await update.message.reply_text("⏱️ Too many requests. Please slow down.")
            except Exception:
                pass
            return False
        return True

    async def _error_handler(
        self, update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        logger.error(f"Telegram error: {context.error}", exc_info=context.error)

    def _get_account_service(self):
        """Extract account service from accounts handler."""
        return self._accounts._accounts

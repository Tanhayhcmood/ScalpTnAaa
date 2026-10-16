"""
Dashboard Handler — main home screen and robot control.
"""

import asyncio
import html
import logging
from typing import Optional
from uuid import uuid4
from telegram import Update
from telegram.ext import ContextTypes
from .base import BaseHandler
from ..keyboards.inline import Keyboards
from ..formatters.messages import MessageFormatter
from ...models.account import Account
from ...config.constants import AccountType, ConnectionStatus


def _format_guardian_reset_receipt(receipt: dict) -> str:
    """Format the robot's applied/no-op reset receipt for Telegram."""
    status = str(receipt.get("status") or "failed")
    if status == "ignored":
        reason = html.escape(str(receipt.get("reason") or "Guardian was not halted."))
        return (
            "🛡️ <b>No reset applied.</b> "
            + reason
            + " Existing baselines and limits were left unchanged."
        )
    if status != "applied":
        reason = html.escape(str(receipt.get("reason") or "The robot could not apply the reset."))
        return "⚠️ <b>Guardian reset not applied.</b> " + reason

    baseline = receipt.get("baseline") or {}
    currency = html.escape(str(receipt.get("currency") or ""))

    def amount(value, signed: bool = False) -> str:
        try:
            number = float(value)
            return f"{number:+,.2f}" if signed else f"{number:,.2f}"
        except (TypeError, ValueError):
            return "n/a"

    reset_at = html.escape(str(baseline.get("reset_at") or "unknown"))
    trading_day = html.escape(str(baseline.get("trading_day") or "unknown"))
    daily_limit = amount(receipt.get("daily_loss_limit_pct"))
    drawdown_limit = amount(receipt.get("max_drawdown_pct"))
    return (
        "🛡️ <b>Guardian reset applied.</b>\n"
        "Daily-loss and drawdown limits now count from the reset point.\n"
        f"Reset (UTC): <code>{reset_at}</code> · trading day: <code>{trading_day}</code>\n"
        f"Day PnL at reset: <code>{amount(baseline.get('realized_day_pnl'), True)} {currency}</code>\n"
        f"Balance at reset: <code>{amount(baseline.get('balance'))} {currency}</code>\n"
        f"Equity / peak at reset: <code>{amount(baseline.get('equity'))} / {amount(baseline.get('equity_peak'))} {currency}</code>\n"
        f"Configured limits unchanged: daily loss {daily_limit}% · max drawdown {drawdown_limit}%"
    )


async def _wait_for_guardian_reset_receipt(
    robot, request_id: str, timeout_seconds: float = 7.5
) -> Optional[dict]:
    """Wait briefly for the live robot to acknowledge a queued reset command."""
    get_fresh_state = getattr(robot, "get_fresh_state", None)
    if not callable(get_fresh_state):
        return None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        try:
            state = await asyncio.wait_for(
                get_fresh_state(), timeout=min(1.5, remaining)
            )
            receipt = state.get("guardian_reset_receipt") if isinstance(state, dict) else None
            if (isinstance(receipt, dict)
                    and str(receipt.get("request_id") or "") == request_id):
                return receipt
        except Exception:
            # A transient state-read failure must not turn a queued command into
            # a false success; keep polling until the bounded wait expires.
            pass
        remaining = deadline - loop.time()
        if remaining > 0:
            await asyncio.sleep(min(0.25, remaining))


def _account_from_robot_state(robot_state: dict) -> Optional[Account]:
    """Synthesize a transient Account from robot_state when no DB account exists.

    This is a display-only fallback so the dashboard account block is never
    blank just because panel.db was wiped on a Render restart.  The object is
    NOT persisted — it is only passed to the formatter.
    """
    # Prefer the richer account_info dict (written by state_writer after our fix)
    info = robot_state.get("account_info") or robot_state.get("account") or {}
    if not info:
        return None

    balance = float(info.get("balance", 0.0))
    equity  = float(info.get("equity",  0.0))
    # Only skip if we have no identifying info at all.
    # Previously we returned None whenever balance=0 — this caused the entire
    # account block to vanish while the robot was starting up or reconnecting,
    # even though broker/server/login were already available from the state.
    broker_check = str(info.get("broker") or "").strip()
    server_check = str(info.get("server") or "").strip()
    login_check  = str(info.get("login")  or info.get("name") or "").strip()
    if balance == 0.0 and equity == 0.0 and not broker_check and not server_check and not login_check:
        return None  # truly no data yet

    broker = str(info.get("broker") or "")
    server = str(info.get("server") or "")
    login  = str(info.get("login")  or info.get("name") or "")
    currency = str(info.get("currency", "USD"))
    leverage = int(info.get("leverage") or 100)

    raw_conn = info.get("connection_status", robot_state.get("connection_status", "disconnected"))
    try:
        conn_status = ConnectionStatus(str(raw_conn).lower())
    except ValueError:
        conn_status = ConnectionStatus.DISCONNECTED

    try:
        acct_type = AccountType("demo")
    except ValueError:
        acct_type = AccountType.DEMO

    return Account(
        id=None,
        name=f"{broker} – {login}" if broker and login else (broker or login or "MT5 Account"),
        account_type=acct_type,
        broker=broker or "—",
        server=server or "—",
        login=login or "—",
        balance=balance,
        equity=equity,
        margin=float(info.get("margin", 0.0)),
        free_margin=float(info.get("free_margin", info.get("freeMargin", 0.0))),
        floating_profit=float(info.get("floating_profit", equity - balance)),
        currency=currency,
        leverage=leverage,
        connection_status=conn_status,
    )

logger = logging.getLogger(__name__)


class DashboardHandler(BaseHandler):
    def __init__(
        self,
        robot_service,
        mt5_service,
        account_service,
        system_service,
        auth_middleware,
        formatter: MessageFormatter,
    ) -> None:
        self._robot = robot_service
        self._mt5 = mt5_service
        self._accounts = account_service
        self._system = system_service
        self._auth = auth_middleware
        self._fmt = formatter

    async def show_home(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, user = await self._auth.is_authorized(update)
        if not ok:
            # Answer the callback so Telegram doesn't show "An error occurred"
            if update.callback_query:
                try:
                    await update.callback_query.answer()
                except Exception:
                    pass
            return
        formatter = MessageFormatter()
        text = formatter.welcome(user.display_name if user else "User")
        await self.edit_or_reply(update, context, text, Keyboards.main_menu())

    async def show_dashboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Answer callback immediately — prevents Telegram timeout / "An error occurred"
        if update.callback_query:
            try:
                await update.callback_query.answer()
            except Exception:
                pass

        ok, user = await self._auth.is_authorized(update)
        if not ok:
            return

        # Gather all data concurrently; each service is individually guarded
        try:
            robot_state, account, today_profit, floating, drawdown, sys_stats = await asyncio.gather(
                self._robot.get_state(),
                self._accounts.get_active_account(),
                self._mt5.get_today_profit(),
                self._mt5.get_floating_profit(),
                self._mt5.get_drawdown(),
                self._system.get_system_stats(),
            )
        except Exception as exc:
            logger.error(f"show_dashboard gather failed: {exc}", exc_info=True)
            # Fallback: show minimal dashboard rather than crashing
            robot_state = {"status": "unknown", "active_trades": 0, "pending_orders": 0}
            account = None
            today_profit = 0.0
            floating = 0.0
            drawdown = {"current_percent": 0.0, "max_percent": 0.0}
            sys_stats = {"cpu_percent": 0.0, "ram_percent": 0.0}

        active_count = robot_state.get("active_trades", 0)
        pending_count = robot_state.get("pending_orders", 0)

        # ── Synthesize account from robot_state when DB account is missing ────
        # This handles the case where /tmp/panel.db was wiped on Render restart
        # and auto_seed didn't recreate it (e.g. MT5_USER env not set in panel).
        # The robot writes full account_info to its state; we use that as fallback.
        if account is None:
            account = _account_from_robot_state(robot_state)

        text = self._fmt.dashboard(
            robot_state=robot_state,
            account=account,
            active_trades=active_count,
            pending_orders=pending_count,
            today_profit=today_profit,
            floating_profit=floating,
            drawdown=drawdown,
            system_stats=sys_stats,
        )
        await self.edit_or_reply(update, context, text, Keyboards.dashboard())

    async def handle_robot_control(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, action: str
    ) -> None:
        ok, user = await self._auth.check_permission(update, "can_control_robot")
        if not ok:
            return

        # Keyboard buttons send "stop_confirm", "emergency_confirm" etc.
        # Normalise to base action so confirm_needed lookup works and
        # Keyboards.confirm_action(action, label) produces "robot:<action>_confirmed"
        # which matches the action_map keys below.
        if action.endswith("_confirm"):
            action = action[: -len("_confirm")]

        # Actions that require explicit confirmation
        confirm_needed = {
            "stop": ("stop_confirmed", "Stop Robot"),
            "emergency": ("emergency_confirmed", "EMERGENCY STOP"),
            "reset_guardian": ("reset_guardian_confirmed", "Reset Cooldown"),
            "restart_engine": ("restart_engine_confirmed", "Restart Engine"),
            "restart_mt5": ("restart_mt5_confirmed", "Restart MT5"),
            "restart_telegram": ("restart_telegram_confirmed", "Restart Telegram Bot"),
            "shutdown": ("shutdown_confirmed", "Safe Shutdown"),
        }

        if action in confirm_needed:
            confirmed_action, label = confirm_needed[action]
            if action == "reset_guardian":
                confirmation_text = (
                    "⚠️ <b>Reset Cooldown?</b>\n\n"
                    "This clears an active RiskGuardian halt and resumes the robot. "
                    "Daily-loss and drawdown checks will count from the live account "
                    "snapshot at reset; configured limits stay unchanged. The separate "
                    "post-SL entry cooldown still applies. If the Guardian is not halted, "
                    "the command does nothing.\n\n"
                    "Continue?"
                )
            else:
                confirmation_text = (
                    f"⚠️ <b>Confirm Action</b>\n\nAre you sure you want to: <b>{label}</b>?\n\n"
                    f"This action will take effect immediately."
                )
            await self.edit_or_reply(
                update, context,
                confirmation_text,
                Keyboards.confirm_action(action, label),
            )
            return

        # Execute confirmed actions
        result = False
        action_map = {
            "start": self._robot.start,
            "pause": self._robot.pause,
            "resume": self._robot.resume,
            "reset_guardian_confirmed": self._robot.reset_guardian,
            "stop_confirmed": self._robot.emergency_stop,
            "emergency_confirmed": self._robot.emergency_stop,
            "restart_engine_confirmed": self._robot.restart_engine,
            "restart_mt5_confirmed": self._robot.restart_mt5,
            "restart_telegram_confirmed": self._robot.restart_telegram,
            "shutdown_confirmed": self._robot.safe_shutdown,
        }

        fn = action_map.get(action)
        reset_request_id = uuid4().hex if action == "reset_guardian_confirmed" else ""
        if fn:
            if reset_request_id:
                result = await fn(request_id=reset_request_id)
            else:
                result = await fn()
            await self._auth.record_action(
                user,
                f"ROBOT_{action.upper()}",
                f"Robot control: {action}",
                success=result,
            )

        if result:
            if action == "reset_guardian_confirmed":
                request_id = reset_request_id
                await self.answer_callback(
                    update, "✅ Reset command sent; waiting for robot confirmation.",
                    show_alert=False,
                )
                receipt = await _wait_for_guardian_reset_receipt(
                    self._robot, request_id
                )
                if receipt is not None:
                    await self.send_message(
                        update, context, _format_guardian_reset_receipt(receipt)
                    )
                else:
                    await self.send_message(
                        update, context,
                        "⏳ Reset command was queued, but the robot has not confirmed "
                        "it yet. This does not mean the reset was applied.",
                    )
                await self.show_dashboard(update, context)
            else:
                await self.answer_callback(
                    update, f"✅ Command sent: {action}", show_alert=False
                )
                await self.show_dashboard(update, context)
        else:
            await self.answer_callback(
                update, f"⚠️ Command may not have reached the robot.", show_alert=True
            )

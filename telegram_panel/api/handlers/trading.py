"""
Trading Handler — position and order management.
"""

import logging
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler
from .base import BaseHandler
from ..keyboards.inline import Keyboards
from ..formatters.messages import MessageFormatter

logger = logging.getLogger(__name__)

# States for entering values
ASK_LOTS, ASK_PRICE, ASK_TRAIL_DIST = range(3)


class TradingHandler(BaseHandler):
    def __init__(
        self,
        trade_service,
        auth_middleware,
        formatter: MessageFormatter,
    ) -> None:
        self._trades = trade_service
        self._auth = auth_middleware
        self._fmt = formatter

    async def show_trading(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, user = await self._auth.check_permission(update, "can_view_dashboard")
        if not ok:
            return
        await self.edit_or_reply(
            update, context,
            "💹 <b>TRADE MANAGEMENT</b>\n\n"
            "Select an option below to manage open positions and orders.",
            Keyboards.trading_menu(),
        )

    async def show_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _ = await self._auth.check_permission(update, "can_view_dashboard")
        if not ok:
            return
        positions = await self._trades.get_open_positions()
        text = self._fmt.positions_list(positions)
        await self.edit_or_reply(update, context, text, Keyboards.positions_list(positions))

    async def show_position_detail(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, ticket: int
    ) -> None:
        ok, _ = await self._auth.check_permission(update, "can_view_dashboard")
        if not ok:
            return
        positions = await self._trades.get_open_positions()
        pos = next((p for p in positions if p.ticket == ticket), None)
        if not pos:
            await self.answer_callback(update, "Position not found", show_alert=True)
            return
        text = self._fmt.position_detail(pos)
        await self.edit_or_reply(update, context, text, Keyboards.position_detail(ticket))

    async def close_position_confirm(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, ticket: int
    ) -> None:
        ok, _ = await self._auth.check_permission(update, "can_manage_trades")
        if not ok:
            return
        await self.edit_or_reply(
            update, context,
            f"⚠️ Close position <b>#{ticket}</b>?\n\nThis will immediately close the trade at market price.",
            Keyboards.confirm_cancel(
                f"trading:close_confirmed:{ticket}",
                f"trading:position_detail:{ticket}",
            ),
        )

    async def close_position(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, ticket: int
    ) -> None:
        ok, user = await self._auth.check_permission(update, "can_manage_trades")
        if not ok:
            return
        result = await self._trades.close_position(ticket)
        success = result.get("success", False)
        await self._auth.record_action(
            user, "TRADE_CLOSE", f"Closed position #{ticket}", success=success
        )
        await self.answer_callback(
            update, "✅ Close order sent" if success else "❌ Failed to close", show_alert=True
        )
        await self.show_positions(update, context)

    async def set_breakeven(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, ticket: int
    ) -> None:
        ok, user = await self._auth.check_permission(update, "can_manage_trades")
        if not ok:
            return
        result = await self._trades.set_breakeven(ticket)
        success = result.get("success", False)
        await self._auth.record_action(
            user, "TRADE_BREAKEVEN", f"Set BE on #{ticket}", success=success
        )
        await self.answer_callback(
            update, "✅ Break even set" if success else "❌ Failed", show_alert=True
        )
        await self.show_position_detail(update, context, ticket)

    async def close_all_confirm(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, close_type: str = "all"
    ) -> None:
        ok, _ = await self._auth.check_permission(update, "can_manage_trades")
        if not ok:
            return
        labels = {
            "all": "ALL positions",
            "buy": "all BUY positions",
            "sell": "all SELL positions",
            "profit": "all PROFITABLE positions",
            "loss": "all LOSING positions",
        }
        label = labels.get(close_type, "positions")
        await self.edit_or_reply(
            update, context,
            f"🚨 <b>Close {label}?</b>\n\nThis will close all matching trades at market price.",
            Keyboards.confirm_cancel(
                f"trading:close_{close_type}_confirmed",
                "trading:menu",
            ),
        )

    async def close_bulk(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, close_type: str
    ) -> None:
        ok, user = await self._auth.check_permission(update, "can_manage_trades")
        if not ok:
            return
        fn_map = {
            "all": self._trades.close_all,
            "buy": self._trades.close_buy,
            "sell": self._trades.close_sell,
            "profit": self._trades.close_profitable,
            "loss": self._trades.close_losing,
        }
        fn = fn_map.get(close_type)
        result = await fn() if fn else {"success": False}
        success = result.get("success", False)
        await self._auth.record_action(
            user, f"TRADE_CLOSE_{close_type.upper()}", f"Bulk close: {close_type}", success=success
        )
        await self.answer_callback(
            update, "✅ Command sent" if success else "❌ Failed", show_alert=True
        )
        await self.show_positions(update, context)

    async def show_pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, _ = await self._auth.check_permission(update, "can_view_dashboard")
        if not ok:
            return
        orders = await self._trades.get_pending_orders()
        if not orders:
            text = "⏳ <b>PENDING ORDERS</b>\n\nNo pending orders."
        else:
            lines = [f"⏳ <b>PENDING ORDERS ({len(orders)})</b>"]
            for o in orders:
                lines.append(
                    f"\n{o.type_icon} <b>#{o.ticket}</b> {o.symbol}\n"
                    f"  📦 {o.volume}L @ {o.open_price:.5f}\n"
                    f"  🛑 SL: {o.stop_loss or '—'}  🎯 TP: {o.take_profit or '—'}"
                )
            text = "\n".join(lines)
        await self.edit_or_reply(
            update, context, text, Keyboards.back_only("trading:menu")
        )

    # ── Partial Close ─────────────────────────────────────────────────────────

    async def handle_partial_close(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, ticket: int
    ) -> None:
        ok, _ = await self._auth.check_permission(update, "can_manage_trades")
        if not ok:
            return
        from telegram import InlineKeyboardMarkup, InlineKeyboardButton
        positions = await self._trades.get_open_positions()
        pos = next((p for p in positions if p.ticket == ticket), None)
        if not pos:
            await self.answer_callback(update, "⚠️ Position not found", show_alert=True)
            return
        lots = pos.volume
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"25%  ({round(lots*0.25,2):.2f}L)", callback_data=f"trading:partial_exec:{ticket}:25"),
                InlineKeyboardButton(f"50%  ({round(lots*0.50,2):.2f}L)", callback_data=f"trading:partial_exec:{ticket}:50"),
            ],
            [InlineKeyboardButton(f"75%  ({round(lots*0.75,2):.2f}L)", callback_data=f"trading:partial_exec:{ticket}:75")],
            [InlineKeyboardButton("← Back", callback_data=f"trading:position_detail:{ticket}")],
        ])
        await self.edit_or_reply(
            update, context,
            f"📦 <b>PARTIAL CLOSE — #{ticket}</b>\n\nPosition: <b>{lots:.2f} lots</b>\nSelect how much to close:",
            kb,
        )

    async def execute_partial_close(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, ticket: int, pct: float
    ) -> None:
        ok, user = await self._auth.check_permission(update, "can_manage_trades")
        if not ok:
            return
        positions = await self._trades.get_open_positions()
        pos = next((p for p in positions if p.ticket == ticket), None)
        if not pos:
            await self.answer_callback(update, "⚠️ Position not found", show_alert=True)
            return
        lots = round(pos.volume * (pct / 100.0), 2)
        if lots < 0.01:
            lots = 0.01
        result = await self._trades.partial_close(ticket, lots)
        success = result.get("success", False)
        await self._auth.record_action(
            user, "TRADE_PARTIAL_CLOSE",
            f"Partial close #{ticket} {pct:.0f}% ({lots:.2f}L)", success=success,
        )
        await self.answer_callback(
            update, "✅ Partial close sent" if success else "❌ Command failed", show_alert=True
        )
        await self.show_positions(update, context)

    # ── Move Stop Loss ────────────────────────────────────────────────────────

    async def handle_move_sl(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, ticket: int
    ) -> None:
        ok, _ = await self._auth.check_permission(update, "can_manage_trades")
        if not ok:
            return
        from telegram import InlineKeyboardMarkup, InlineKeyboardButton
        positions = await self._trades.get_open_positions()
        pos = next((p for p in positions if p.ticket == ticket), None)
        if not pos:
            await self.answer_callback(update, "⚠️ Position not found", show_alert=True)
            return
        sl_text = f"{pos.stop_loss:.2f}" if pos.stop_loss else "None"
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("−50 pts", callback_data=f"trading:sl_exec:{ticket}:-50"),
                InlineKeyboardButton("−20 pts", callback_data=f"trading:sl_exec:{ticket}:-20"),
                InlineKeyboardButton("−10 pts", callback_data=f"trading:sl_exec:{ticket}:-10"),
            ],
            [
                InlineKeyboardButton("+10 pts", callback_data=f"trading:sl_exec:{ticket}:10"),
                InlineKeyboardButton("+20 pts", callback_data=f"trading:sl_exec:{ticket}:20"),
                InlineKeyboardButton("+50 pts", callback_data=f"trading:sl_exec:{ticket}:50"),
            ],
            [InlineKeyboardButton("← Back", callback_data=f"trading:position_detail:{ticket}")],
        ])
        await self.edit_or_reply(
            update, context,
            f"🛑 <b>MOVE STOP LOSS — #{ticket}</b>\n\nCurrent SL: <b>{sl_text}</b>\n1 pt = $0.01 (XAUUSD):",
            kb,
        )

    async def execute_move_sl(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, ticket: int, pts: float
    ) -> None:
        ok, user = await self._auth.check_permission(update, "can_manage_trades")
        if not ok:
            return
        positions = await self._trades.get_open_positions()
        pos = next((p for p in positions if p.ticket == ticket), None)
        if not pos:
            await self.answer_callback(update, "⚠️ Position not found", show_alert=True)
            return
        if not pos.stop_loss:
            await self.answer_callback(update, "⚠️ No SL set on this position", show_alert=True)
            return
        new_sl = round(pos.stop_loss + pts * 0.01, 2)
        result = await self._trades.modify_sl(ticket, new_sl)
        success = result.get("success", False)
        await self._auth.record_action(
            user, "TRADE_MODIFY_SL",
            f"Move SL #{ticket} {pts:+.0f}pts → {new_sl:.2f}", success=success,
        )
        await self.answer_callback(
            update, f"✅ SL → {new_sl:.2f}" if success else "❌ Command failed", show_alert=True
        )
        await self.show_position_detail(update, context, ticket)

    # ── Move Take Profit ──────────────────────────────────────────────────────

    async def handle_move_tp(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, ticket: int
    ) -> None:
        ok, _ = await self._auth.check_permission(update, "can_manage_trades")
        if not ok:
            return
        from telegram import InlineKeyboardMarkup, InlineKeyboardButton
        positions = await self._trades.get_open_positions()
        pos = next((p for p in positions if p.ticket == ticket), None)
        if not pos:
            await self.answer_callback(update, "⚠️ Position not found", show_alert=True)
            return
        tp_text = f"{pos.take_profit:.2f}" if pos.take_profit else "None"
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("−50 pts", callback_data=f"trading:tp_exec:{ticket}:-50"),
                InlineKeyboardButton("−20 pts", callback_data=f"trading:tp_exec:{ticket}:-20"),
                InlineKeyboardButton("−10 pts", callback_data=f"trading:tp_exec:{ticket}:-10"),
            ],
            [
                InlineKeyboardButton("+10 pts", callback_data=f"trading:tp_exec:{ticket}:10"),
                InlineKeyboardButton("+20 pts", callback_data=f"trading:tp_exec:{ticket}:20"),
                InlineKeyboardButton("+50 pts", callback_data=f"trading:tp_exec:{ticket}:50"),
            ],
            [InlineKeyboardButton("← Back", callback_data=f"trading:position_detail:{ticket}")],
        ])
        await self.edit_or_reply(
            update, context,
            f"🎯 <b>MOVE TAKE PROFIT — #{ticket}</b>\n\nCurrent TP: <b>{tp_text}</b>\n1 pt = $0.01 (XAUUSD):",
            kb,
        )

    async def execute_move_tp(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, ticket: int, pts: float
    ) -> None:
        ok, user = await self._auth.check_permission(update, "can_manage_trades")
        if not ok:
            return
        positions = await self._trades.get_open_positions()
        pos = next((p for p in positions if p.ticket == ticket), None)
        if not pos:
            await self.answer_callback(update, "⚠️ Position not found", show_alert=True)
            return
        if not pos.take_profit:
            await self.answer_callback(update, "⚠️ No TP set on this position", show_alert=True)
            return
        new_tp = round(pos.take_profit + pts * 0.01, 2)
        result = await self._trades.modify_tp(ticket, new_tp)
        success = result.get("success", False)
        await self._auth.record_action(
            user, "TRADE_MODIFY_TP",
            f"Move TP #{ticket} {pts:+.0f}pts → {new_tp:.2f}", success=success,
        )
        await self.answer_callback(
            update, f"✅ TP → {new_tp:.2f}" if success else "❌ Command failed", show_alert=True
        )
        await self.show_position_detail(update, context, ticket)

    # ── Trailing Stop ─────────────────────────────────────────────────────────

    async def handle_trail(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, ticket: int
    ) -> None:
        ok, _ = await self._auth.check_permission(update, "can_manage_trades")
        if not ok:
            return
        from telegram import InlineKeyboardMarkup, InlineKeyboardButton
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("30 pts",  callback_data=f"trading:trail_exec:{ticket}:30"),
                InlineKeyboardButton("50 pts",  callback_data=f"trading:trail_exec:{ticket}:50"),
                InlineKeyboardButton("80 pts",  callback_data=f"trading:trail_exec:{ticket}:80"),
            ],
            [
                InlineKeyboardButton("100 pts", callback_data=f"trading:trail_exec:{ticket}:100"),
                InlineKeyboardButton("150 pts", callback_data=f"trading:trail_exec:{ticket}:150"),
            ],
            [InlineKeyboardButton("← Back", callback_data=f"trading:position_detail:{ticket}")],
        ])
        await self.edit_or_reply(
            update, context,
            f"📐 <b>TRAILING STOP — #{ticket}</b>\n\nSelect trailing distance (1 pt = $0.01):",
            kb,
        )

    async def execute_trail(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, ticket: int, pts: float
    ) -> None:
        ok, user = await self._auth.check_permission(update, "can_manage_trades")
        if not ok:
            return
        result = await self._trades.set_trailing(ticket, pts)
        success = result.get("success", False)
        await self._auth.record_action(
            user, "TRADE_SET_TRAIL",
            f"Trailing stop #{ticket} distance={pts:.0f}pts", success=success,
        )
        await self.answer_callback(
            update,
            f"✅ Trailing stop set ({pts:.0f} pts)" if success else "❌ Command failed",
            show_alert=True,
        )
        await self.show_position_detail(update, context, ticket)

    # ── Trade History ─────────────────────────────────────────────────────────

    async def show_trade_history(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, limit: int = 20
    ) -> None:
        """
        Display recent closed trades via /history or trading:history callback.
        ROOT-CAUSE FIX: no handler existed to show completed trade history.
        """
        ok, _ = await self._auth.check_permission(update, "can_view_dashboard")
        if not ok:
            return
        from ..formatters.messages import MessageFormatter
        from telegram import InlineKeyboardMarkup, InlineKeyboardButton
        try:
            trades = await self._trades.get_recent_trades(limit)
        except Exception as exc:
            await self.edit_or_reply(
                update, context,
                MessageFormatter.error(f"Could not fetch trade history: {exc}"),
                InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="nav:trading")]]),
            )
            return
        text = MessageFormatter.trade_history(trades)
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 Refresh",  callback_data="trading:history"),
                InlineKeyboardButton("📋 Last 50",  callback_data="trading:history:50"),
            ],
            [InlineKeyboardButton("← Back", callback_data="nav:trading")],
        ])
        await self.edit_or_reply(update, context, text, kb)
        if update.callback_query:
            await update.callback_query.answer()
    

"""
Trade Service — position and order management via MT5 service interface.
"""

import logging
from typing import Optional
from ..models.trade import Position, PendingOrder
from ..services.mt5_service import MT5Service

logger = logging.getLogger(__name__)


class TradeService:
    def __init__(self, mt5: MT5Service) -> None:
        self._mt5 = mt5

    async def get_open_positions(self) -> list[Position]:
        return await self._mt5.get_open_positions()

    async def get_pending_orders(self) -> list[PendingOrder]:
        return await self._mt5.get_pending_orders()

    async def get_today_profit(self) -> float:
        return await self._mt5.get_today_profit()

    async def get_floating_profit(self) -> float:
        return await self._mt5.get_floating_profit()

    async def get_drawdown(self) -> dict[str, float]:
        return await self._mt5.get_drawdown()

    async def close_position(self, ticket: int) -> dict[str, object]:
        return await self._mt5.send_trade_command("CLOSE", {"ticket": ticket})

    async def partial_close(self, ticket: int, lots: float) -> dict[str, object]:
        return await self._mt5.send_trade_command(
            "PARTIAL_CLOSE", {"ticket": ticket, "lots": lots}
        )

    async def modify_sl(self, ticket: int, new_sl: float) -> dict[str, object]:
        return await self._mt5.send_trade_command(
            "MODIFY_SL", {"ticket": ticket, "sl": new_sl}
        )

    async def modify_tp(self, ticket: int, new_tp: float) -> dict[str, object]:
        return await self._mt5.send_trade_command(
            "MODIFY_TP", {"ticket": ticket, "tp": new_tp}
        )

    async def set_breakeven(self, ticket: int) -> dict[str, object]:
        return await self._mt5.send_trade_command("SET_BREAKEVEN", {"ticket": ticket})

    async def set_trailing(self, ticket: int, distance_pips: float) -> dict[str, object]:
        return await self._mt5.send_trade_command(
            "SET_TRAILING", {"ticket": ticket, "distance_pips": distance_pips}
        )

    async def close_all(self) -> dict[str, object]:
        return await self._mt5.send_trade_command("CLOSE_ALL", {})

    async def close_buy(self) -> dict[str, object]:
        return await self._mt5.send_trade_command("CLOSE_ALL_BUY", {})

    async def close_sell(self) -> dict[str, object]:
        return await self._mt5.send_trade_command("CLOSE_ALL_SELL", {})

    async def close_profitable(self) -> dict[str, object]:
        return await self._mt5.send_trade_command("CLOSE_PROFITABLE", {})

    async def close_losing(self) -> dict[str, object]:
        return await self._mt5.send_trade_command("CLOSE_LOSING", {})

    async def get_recent_trades(self, limit: int = 20) -> list:
        """
        Return the most recent closed trades from the robot snapshot.

        ROOT-CAUSE FIX: TradeService had no get_recent_trades() method, so
        the Telegram panel had no way to fetch completed trade history from
        the MT5 snapshot (which IS populated by the live robot).
        """
        from ..models.trade import Trade
        trades = await self._mt5.get_recent_trades(limit)
        return trades

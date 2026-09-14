"""
Trade, Position, and PendingOrder models.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from ..config.constants import TradeDirection, TradeStatus


@dataclass
class Trade:
    """Represents a completed or active trade."""
    ticket: int
    symbol: str
    direction: TradeDirection
    volume: float           # lots
    open_price: float
    current_price: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    open_time: datetime
    close_time: Optional[datetime] = None
    close_price: Optional[float] = None
    profit: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    status: TradeStatus = TradeStatus.OPEN
    comment: Optional[str] = None
    close_reason: Optional[str] = None
    magic: int = 0
    account_id: Optional[int] = None
    # Derived
    pips: float = 0.0
    rr_ratio: Optional[float] = None
    sl_distance_pips: Optional[float] = None
    tp_distance_pips: Optional[float] = None
    duration_minutes: Optional[int] = None

    @property
    def net_profit(self) -> float:
        return self.profit + self.commission + self.swap

    @property
    def direction_icon(self) -> str:
        return "🟢" if self.direction == TradeDirection.BUY else "🔴"

    @property
    def profit_icon(self) -> str:
        return "💰" if self.net_profit >= 0 else "🔻"

    @property
    def status_icon(self) -> str:
        icons = {
            TradeStatus.OPEN: "📈",
            TradeStatus.CLOSED: "✅",
            TradeStatus.PENDING: "⏳",
            TradeStatus.CANCELLED: "❌",
            TradeStatus.PARTIALLY_CLOSED: "🔄",
        }
        return icons.get(self.status, "📊")


@dataclass
class Position(Trade):
    """Active open position with live metrics."""
    floating_profit: float = 0.0
    margin_used: float = 0.0
    breakeven_activated: bool = False
    trailing_stop_active: bool = False
    partial_close_done: bool = False
    # Why the robot opened this trade (regime, confirming engines, top
    # signals) — published by the robot at trade-open time and looked up by
    # ticket. None when unavailable (older trade, Redis down, etc.); callers
    # must degrade gracefully rather than assume it is always present.
    strategy: Optional[dict] = None


@dataclass
class PendingOrder:
    """Pending limit/stop order."""
    ticket: int
    symbol: str
    order_type: str          # "BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"
    volume: float
    open_price: float        # trigger price
    stop_loss: Optional[float]
    take_profit: Optional[float]
    expiry: Optional[datetime] = None
    placed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    comment: Optional[str] = None
    magic: int = 0
    account_id: Optional[int] = None

    @property
    def type_icon(self) -> str:
        icons = {
            "BUY_LIMIT": "🟢⬇️",
            "SELL_LIMIT": "🔴⬆️",
            "BUY_STOP": "🟢⬆️",
            "SELL_STOP": "🔴⬇️",
        }
        return icons.get(self.order_type, "⏳")

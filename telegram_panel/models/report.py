"""
Report models — daily, weekly, monthly trading performance.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional


@dataclass
class TradeRecord:
    """Persisted trade record for reporting."""
    id: Optional[int]
    ticket: int
    account_id: int
    symbol: str
    direction: str
    volume: float
    open_price: float
    close_price: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    open_time: datetime
    close_time: datetime
    profit: float
    commission: float
    swap: float
    pips: float
    rr_ratio: Optional[float]
    duration_minutes: int
    close_reason: Optional[str] = None   # "TP", "SL", "Manual", "Trailing", "BE"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def net_profit(self) -> float:
        return self.profit + self.commission + self.swap

    @property
    def is_winner(self) -> bool:
        return self.net_profit > 0


@dataclass
class DailyReport:
    """Aggregated daily trading statistics."""
    id: Optional[int]
    account_id: int
    report_date: date
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    break_even_trades: int = 0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    total_commission: float = 0.0
    total_swap: float = 0.0
    net_profit: float = 0.0
    win_rate: float = 0.0
    average_rr: float = 0.0
    average_trade_profit: float = 0.0
    average_winner: float = 0.0
    average_loser: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_percent: float = 0.0
    best_trade_profit: float = 0.0
    worst_trade_profit: float = 0.0
    total_pips: float = 0.0
    starting_balance: float = 0.0
    ending_balance: float = 0.0
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def loss_rate(self) -> float:
        return 100.0 - self.win_rate

    @property
    def result_icon(self) -> str:
        if self.net_profit > 0:
            return "💰"
        elif self.net_profit < 0:
            return "🔻"
        return "➖"

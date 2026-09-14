"""
Risk configuration model — persisted risk management settings.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class RiskConfig:
    """All risk management parameters."""
    id: Optional[int] = None
    account_id: Optional[int] = None   # None = global default

    risk_percent: float = 1.0          # % of balance per trade
    lot_size_override: Optional[float] = None  # If set, overrides risk %
    daily_loss_limit: float = 3.0      # % of balance
    max_concurrent_trades: int = 3
    max_spread_pips: float = 30.0      # XAUUSD pips
    max_drawdown_percent: float = 10.0
    rr_ratio: float = 2.0
    default_sl_pips: float = 150.0
    default_tp_pips: float = 300.0
    auto_breakeven: bool = True
    be_trigger_pips: float = 100.0     # Move SL to entry when profit >= this
    auto_trailing: bool = True
    trail_distance_pips: float = 80.0
    trail_activation_pips: float = 120.0

    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def validate(self) -> list[str]:
        errors = []
        if not 0.01 <= self.risk_percent <= 10.0:
            errors.append("risk_percent must be between 0.01 and 10.0")
        if self.daily_loss_limit <= 0 or self.daily_loss_limit > 50:
            errors.append("daily_loss_limit must be between 0 and 50%")
        if self.max_concurrent_trades < 1 or self.max_concurrent_trades > 100:
            errors.append("max_concurrent_trades must be between 1 and 100")
        if self.rr_ratio < 0.5 or self.rr_ratio > 20:
            errors.append("rr_ratio must be between 0.5 and 20")
        if self.max_spread_pips < 1 or self.max_spread_pips > 500:
            errors.append("max_spread_pips must be between 1 and 500")
        return errors

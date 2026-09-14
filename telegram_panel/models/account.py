"""
Account model — represents a broker trading account.
Supports Real, Demo, and Prop Firm account types.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from ..config.constants import AccountType, ConnectionStatus


@dataclass
class Account:
    id: Optional[int]
    name: str                         # Display name, e.g. "ICMarkets Real #1"
    account_type: AccountType
    broker: str                       # e.g. "ICMarkets", "Pepperstone"
    server: str                       # MT5 server name
    login: str                        # MT5 login (stored as str, may be large)
    # NOTE: password is stored encrypted in DB — never stored plain
    password_encrypted: Optional[str] = None
    is_active: bool = True
    is_enabled: bool = True
    # Live data (populated by MT5 service, not persisted)
    balance: float = 0.0
    equity: float = 0.0
    margin: float = 0.0
    free_margin: float = 0.0
    margin_level: float = 0.0
    floating_profit: float = 0.0
    currency: str = "USD"
    leverage: int = 100
    connection_status: ConnectionStatus = ConnectionStatus.DISCONNECTED
    last_connected_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Prop firm specific
    prop_firm_name: Optional[str] = None
    prop_challenge_phase: Optional[str] = None   # e.g. "Phase 1", "Funded"
    prop_max_daily_loss: Optional[float] = None
    prop_max_total_loss: Optional[float] = None
    prop_profit_target: Optional[float] = None
    # Metadata
    notes: Optional[str] = None

    @property
    def type_icon(self) -> str:
        icons = {
            AccountType.REAL: "💰",
            AccountType.DEMO: "🎓",
            AccountType.PROP_FIRM: "🏆",
        }
        return icons.get(self.account_type, "👤")

    @property
    def connection_icon(self) -> str:
        icons = {
            ConnectionStatus.CONNECTED: "🟢",
            ConnectionStatus.DISCONNECTED: "🔴",
            ConnectionStatus.RECONNECTING: "🟡",
            ConnectionStatus.FAILED: "❌",
        }
        return icons.get(self.connection_status, "⚪")

    @property
    def short_display(self) -> str:
        return f"{self.connection_icon} {self.type_icon} {self.name} [{self.broker}]"

    @property
    def profit_loss_today(self) -> float:
        """Populated from reports service."""
        return 0.0

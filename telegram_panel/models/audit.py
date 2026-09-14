"""
Audit log model — every admin action is recorded.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class AuditLog:
    """Immutable audit trail entry for every significant action."""
    id: Optional[int]
    telegram_id: int
    username: Optional[str]
    action: str               # e.g. "ROBOT_STOP", "RISK_CHANGE", "TRADE_CLOSE"
    description: str          # Human-readable description
    target: Optional[str]     # What was acted on (e.g. "Trade #12345")
    old_value: Optional[str]  # JSON string of before state
    new_value: Optional[str]  # JSON string of after state
    ip_address: Optional[str] = None
    success: bool = True
    error_message: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def result_icon(self) -> str:
        return "✅" if self.success else "❌"

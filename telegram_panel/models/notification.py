"""
Notification settings and log models.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from ..config.constants import NotificationType


@dataclass
class NotificationSetting:
    """Per-notification-type enable/disable configuration."""
    notification_type: NotificationType
    enabled: bool = True
    user_telegram_id: Optional[int] = None    # None = global default
    cooldown_seconds: int = 0                 # Avoid spam for repeated events
    last_sent_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def status_icon(self) -> str:
        return "🔔" if self.enabled else "🔕"


@dataclass
class NotificationLog:
    """Audit trail of sent notifications."""
    id: Optional[int]
    notification_type: NotificationType
    recipient_telegram_id: int
    message_text: str
    sent_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    success: bool = True
    error_message: Optional[str] = None
    message_id: Optional[int] = None   # Telegram message_id for reference
    metadata: Optional[str] = None     # JSON string for extra data

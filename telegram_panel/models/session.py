"""
User session model — tracks active Telegram sessions.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional


@dataclass
class UserSession:
    """Tracks an active user session with the bot."""
    id: Optional[int]
    telegram_id: int
    current_page: str = "home"
    breadcrumb: list[str] = field(default_factory=list)
    context: dict = field(default_factory=dict)    # Page-specific state
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc) + timedelta(hours=1))
    is_active: bool = True

    def touch(self, timeout_minutes: int = 60) -> None:
        """Refresh session expiry."""
        now = datetime.now(timezone.utc)
        self.last_activity_at = now
        self.expires_at = now + timedelta(minutes=timeout_minutes)

    def is_expired(self) -> bool:
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > expires_at or not self.is_active

    def navigate_to(self, page: str) -> None:
        """Navigate to a page and update breadcrumb."""
        if page == "home":
            self.breadcrumb = []
        else:
            if self.current_page != page:
                self.breadcrumb.append(self.current_page)
        self.current_page = page

    def go_back(self) -> str:
        """Navigate back. Returns previous page name."""
        if self.breadcrumb:
            prev = self.breadcrumb.pop()
            self.current_page = prev
            return prev
        self.current_page = "home"
        return "home"

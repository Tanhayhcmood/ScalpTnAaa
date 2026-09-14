"""
User and permission models for the Telegram Control Panel.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from ..config.constants import BotRole


@dataclass
class UserPermission:
    can_view_dashboard: bool = True
    can_view_accounts: bool = True
    can_manage_accounts: bool = False
    can_control_robot: bool = False
    can_manage_trades: bool = False
    can_change_risk: bool = False
    can_change_strategy: bool = False
    can_view_reports: bool = True
    can_export_reports: bool = False
    can_manage_notifications: bool = False
    can_access_system: bool = False
    can_manage_users: bool = False
    can_view_logs: bool = False

    @classmethod
    def for_role(cls, role: BotRole) -> "UserPermission":
        if role == BotRole.OWNER:
            return cls(
                can_view_dashboard=True,
                can_view_accounts=True,
                can_manage_accounts=True,
                can_control_robot=True,
                can_manage_trades=True,
                can_change_risk=True,
                can_change_strategy=True,
                can_view_reports=True,
                can_export_reports=True,
                can_manage_notifications=True,
                can_access_system=True,
                can_manage_users=True,
                can_view_logs=True,
            )
        elif role == BotRole.ADMIN:
            return cls(
                can_view_dashboard=True,
                can_view_accounts=True,
                can_manage_accounts=True,
                can_control_robot=True,
                can_manage_trades=True,
                can_change_risk=True,
                can_change_strategy=True,
                can_view_reports=True,
                can_export_reports=True,
                can_manage_notifications=True,
                can_access_system=True,
                can_manage_users=False,
                can_view_logs=True,
            )
        elif role == BotRole.VIEWER:
            return cls(
                can_view_dashboard=True,
                can_view_accounts=True,
                can_manage_accounts=False,
                can_control_robot=False,
                can_manage_trades=False,
                can_change_risk=False,
                can_change_strategy=False,
                can_view_reports=True,
                can_export_reports=False,
                can_manage_notifications=False,
                can_access_system=False,
                can_manage_users=False,
                can_view_logs=False,
            )
        else:  # BLOCKED
            return cls(
                can_view_dashboard=False,
                can_view_accounts=False,
                can_manage_accounts=False,
                can_control_robot=False,
                can_manage_trades=False,
                can_change_risk=False,
                can_change_strategy=False,
                can_view_reports=False,
                can_export_reports=False,
                can_manage_notifications=False,
                can_access_system=False,
                can_manage_users=False,
                can_view_logs=False,
            )


@dataclass
class User:
    telegram_id: int
    username: Optional[str]
    first_name: str
    last_name: Optional[str]
    role: BotRole
    is_active: bool = True
    permissions: UserPermission = field(default_factory=UserPermission)
    last_seen_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    failed_auth_attempts: int = 0
    last_failed_auth_at: Optional[datetime] = None
    notes: Optional[str] = None

    def __post_init__(self) -> None:
        if isinstance(self.role, str):
            self.role = BotRole(self.role)
        self.permissions = UserPermission.for_role(self.role)

    @property
    def display_name(self) -> str:
        name = self.first_name
        if self.last_name:
            name += f" {self.last_name}"
        if self.username:
            name += f" (@{self.username})"
        return name

    @property
    def role_icon(self) -> str:
        icons = {
            BotRole.OWNER: "👑",
            BotRole.ADMIN: "🛡️",
            BotRole.VIEWER: "👁️",
            BotRole.BLOCKED: "🚫",
        }
        return icons.get(self.role, "👤")

    def is_blocked(self) -> bool:
        return self.role == BotRole.BLOCKED or not self.is_active

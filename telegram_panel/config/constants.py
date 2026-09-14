"""
Constants and Enumerations for GoldScalperPro Telegram Panel
All constants in one place — never hardcoded elsewhere.
"""

from enum import Enum, auto


# ─────────────────────────────────────────────
# User & Security
# ─────────────────────────────────────────────

class BotRole(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    VIEWER = "viewer"
    BLOCKED = "blocked"


class SessionStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    TERMINATED = "terminated"


# ─────────────────────────────────────────────
# Robot & Trading
# ─────────────────────────────────────────────

class RobotStatus(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"
    STARTING = "starting"
    STOPPING = "stopping"
    RESTARTING = "restarting"
    WAITING = "waiting"
    SCANNING = "scanning"
    HOLDING = "holding"
    DISCONNECTED = "disconnected"


class TradeDirection(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TradeStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    PENDING = "pending"
    CANCELLED = "cancelled"
    PARTIALLY_CLOSED = "partially_closed"


class AccountType(str, Enum):
    REAL = "real"
    DEMO = "demo"
    PROP_FIRM = "prop_firm"


class ConnectionStatus(str, Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


# ─────────────────────────────────────────────
# Strategy
# ─────────────────────────────────────────────

class StrategyComponent(str, Enum):
    SMC = "smc"
    BOS = "bos"
    CHOCH = "choch"
    ORDER_BLOCKS = "order_blocks"
    LIQUIDITY = "liquidity"
    FVG = "fvg"
    MITIGATION = "mitigation"
    SESSIONS = "sessions"
    TREND_FILTER = "trend_filter"
    VOLUME_FILTER = "volume_filter"
    NEWS_FILTER = "news_filter"
    TIME_FILTER = "time_filter"
    SPREAD_FILTER = "spread_filter"

    @property
    def display_name(self) -> str:
        names = {
            "smc": "Smart Money Concepts",
            "bos": "Break of Structure",
            "choch": "Change of Character",
            "order_blocks": "Order Blocks",
            "liquidity": "Liquidity Zones",
            "fvg": "Fair Value Gaps",
            "mitigation": "OB Mitigation",
            "sessions": "Session Filter",
            "trend_filter": "Trend Filter",
            "volume_filter": "Volume Filter",
            "news_filter": "News Filter",
            "time_filter": "Time Filter",
            "spread_filter": "Spread Filter",
        }
        return names.get(self.value, self.value)


# ─────────────────────────────────────────────
# Risk Management
# ─────────────────────────────────────────────

class RiskParameter(str, Enum):
    RISK_PERCENT = "risk_percent"
    LOT_SIZE = "lot_size"
    DAILY_LOSS = "daily_loss_limit"
    MAX_TRADES = "max_concurrent_trades"
    MAX_SPREAD = "max_spread_pips"
    MAX_DRAWDOWN = "max_drawdown_percent"
    RR_RATIO = "rr_ratio"
    STOP_LOSS = "default_sl_pips"
    TAKE_PROFIT = "default_tp_pips"
    AUTO_BE = "auto_breakeven"
    AUTO_TRAIL = "auto_trailing"


# ─────────────────────────────────────────────
# Notifications
# ─────────────────────────────────────────────

class NotificationType(str, Enum):
    TRADE_OPEN = "trade_open"
    TRADE_CLOSE = "trade_close"
    SL_HIT = "sl_hit"
    TP_HIT = "tp_hit"
    DAILY_TARGET = "daily_target"
    DAILY_LOSS = "daily_loss"
    CONNECTION_LOST = "connection_lost"
    CONNECTION_RESTORED = "connection_restored"
    NEWS_PAUSE = "news_pause"
    ERROR = "error"
    WARNING = "warning"
    SYSTEM_RESTART = "system_restart"
    HEARTBEAT = "heartbeat"

    @property
    def icon(self) -> str:
        icons = {
            "trade_open": "📈",
            "trade_close": "📉",
            "sl_hit": "🛑",
            "tp_hit": "✅",
            "daily_target": "🎯",
            "daily_loss": "⚠️",
            "connection_lost": "🔴",
            "connection_restored": "🟢",
            "news_pause": "📰",
            "error": "❌",
            "warning": "⚠️",
            "system_restart": "🔄",
            "heartbeat": "💓",
        }
        return icons.get(self.value, "📣")

    @property
    def display_name(self) -> str:
        names = {
            "trade_open": "Trade Opened",
            "trade_close": "Trade Closed",
            "sl_hit": "Stop Loss Hit",
            "tp_hit": "Take Profit Hit",
            "daily_target": "Daily Target Reached",
            "daily_loss": "Daily Loss Limit",
            "connection_lost": "Connection Lost",
            "connection_restored": "Connection Restored",
            "news_pause": "News Pause",
            "error": "Error Alert",
            "warning": "Warning Alert",
            "system_restart": "System Restart",
            "heartbeat": "Heartbeat",
        }
        return names.get(self.value, self.value)


# ─────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────

class MenuPage(str, Enum):
    HOME = "home"
    DASHBOARD = "dashboard"
    ACCOUNTS = "accounts"
    TRADING = "trading"
    RISK = "risk"
    STRATEGY = "strategy"
    NEWS = "news"
    REPORTS = "reports"
    NOTIFICATIONS = "notifications"
    SETTINGS = "settings"
    SYSTEM = "system"


# ─────────────────────────────────────────────
# Timing
# ─────────────────────────────────────────────

SESSION_TIMEOUT_MINUTES = 60
HEARTBEAT_INTERVAL_SECONDS = 30
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_REQUESTS = 30
AUDIT_LOG_RETENTION_DAYS = 90
REPORT_CACHE_TTL_SECONDS = 300

# ─────────────────────────────────────────────
# Icons & Symbols
# ─────────────────────────────────────────────

ICONS = {
    "robot": "🤖",
    "dashboard": "📊",
    "account": "👤",
    "trading": "💹",
    "risk": "🛡️",
    "strategy": "🧠",
    "news": "📰",
    "reports": "📋",
    "notifications": "🔔",
    "settings": "⚙️",
    "system": "💻",
    "online": "🟢",
    "offline": "🔴",
    "warning": "🟡",
    "profit": "💰",
    "loss": "🔻",
    "balance": "💳",
    "equity": "📈",
    "margin": "📊",
    "cpu": "🖥️",
    "ram": "🧮",
    "disk": "💾",
    "latency": "⚡",
    "internet": "🌐",
    "broker": "🏦",
    "vps": "🖧",
    "mt5": "📡",
    "lock": "🔒",
    "key": "🔑",
    "check": "✅",
    "cross": "❌",
    "arrow_right": "▶️",
    "arrow_back": "◀️",
    "refresh": "🔄",
    "stop": "⏹️",
    "play": "▶️",
    "pause": "⏸️",
    "emergency": "🚨",
    "crown": "👑",
    "shield": "🛡️",
    "chart": "📉",
    "money": "💵",
    "target": "🎯",
    "fire": "🔥",
    "thunder": "⚡",
    "calendar": "📅",
    "clock": "🕐",
    "upload": "⬆️",
    "download": "⬇️",
    "trash": "🗑️",
    "edit": "✏️",
    "add": "➕",
    "save": "💾",
    "heart": "❤️",
    "star": "⭐",
    "flag": "🚩",
}

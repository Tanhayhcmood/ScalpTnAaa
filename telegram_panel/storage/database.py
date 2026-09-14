"""
SQLite database setup using SQLAlchemy Core.
Repository pattern — all DB access goes through repositories.
"""

import os
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional
from functools import lru_cache

logger = logging.getLogger(__name__)

# DDL for all tables
_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    account_type TEXT NOT NULL,
    broker TEXT NOT NULL,
    server TEXT NOT NULL,
    login TEXT NOT NULL,
    password_encrypted TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    is_enabled INTEGER NOT NULL DEFAULT 1,
    currency TEXT NOT NULL DEFAULT 'USD',
    leverage INTEGER NOT NULL DEFAULT 100,
    prop_firm_name TEXT,
    prop_challenge_phase TEXT,
    prop_max_daily_loss REAL,
    prop_max_total_loss REAL,
    prop_profit_target REAL,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT NOT NULL,
    last_name TEXT,
    role TEXT NOT NULL DEFAULT 'viewer',
    is_active INTEGER NOT NULL DEFAULT 1,
    failed_auth_attempts INTEGER NOT NULL DEFAULT 0,
    last_failed_auth_at TEXT,
    last_seen_at TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    current_page TEXT NOT NULL DEFAULT 'home',
    breadcrumb TEXT NOT NULL DEFAULT '[]',
    context TEXT NOT NULL DEFAULT '{}',
    is_active INTEGER NOT NULL DEFAULT 1,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_activity_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    risk_percent REAL NOT NULL DEFAULT 1.0,
    lot_size_override REAL,
    daily_loss_limit REAL NOT NULL DEFAULT 3.0,
    max_concurrent_trades INTEGER NOT NULL DEFAULT 3,
    max_spread_pips REAL NOT NULL DEFAULT 30.0,
    max_drawdown_percent REAL NOT NULL DEFAULT 10.0,
    rr_ratio REAL NOT NULL DEFAULT 2.0,
    default_sl_pips REAL NOT NULL DEFAULT 150.0,
    default_tp_pips REAL NOT NULL DEFAULT 300.0,
    auto_breakeven INTEGER NOT NULL DEFAULT 1,
    be_trigger_pips REAL NOT NULL DEFAULT 100.0,
    auto_trailing INTEGER NOT NULL DEFAULT 1,
    trail_distance_pips REAL NOT NULL DEFAULT 80.0,
    trail_activation_pips REAL NOT NULL DEFAULT 120.0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS strategy_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    smc_enabled INTEGER NOT NULL DEFAULT 1,
    bos_enabled INTEGER NOT NULL DEFAULT 1,
    choch_enabled INTEGER NOT NULL DEFAULT 1,
    order_blocks_enabled INTEGER NOT NULL DEFAULT 1,
    liquidity_enabled INTEGER NOT NULL DEFAULT 1,
    fvg_enabled INTEGER NOT NULL DEFAULT 1,
    mitigation_enabled INTEGER NOT NULL DEFAULT 1,
    sessions_enabled INTEGER NOT NULL DEFAULT 1,
    trend_filter_enabled INTEGER NOT NULL DEFAULT 1,
    volume_filter_enabled INTEGER NOT NULL DEFAULT 1,
    news_filter_enabled INTEGER NOT NULL DEFAULT 1,
    time_filter_enabled INTEGER NOT NULL DEFAULT 1,
    spread_filter_enabled INTEGER NOT NULL DEFAULT 1,
    min_confidence_score REAL NOT NULL DEFAULT 49.0,
    min_rr_ratio REAL NOT NULL DEFAULT 2.0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS notification_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_type TEXT NOT NULL,
    user_telegram_id INTEGER REFERENCES users(telegram_id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 1,
    cooldown_seconds INTEGER NOT NULL DEFAULT 0,
    last_sent_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(notification_type, user_telegram_id)
);

CREATE TABLE IF NOT EXISTS notification_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_type TEXT NOT NULL,
    recipient_telegram_id INTEGER NOT NULL,
    message_text TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT (datetime('now')),
    success INTEGER NOT NULL DEFAULT 1,
    error_message TEXT,
    message_id INTEGER,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS trade_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket INTEGER NOT NULL,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    volume REAL NOT NULL,
    open_price REAL NOT NULL,
    close_price REAL NOT NULL,
    stop_loss REAL,
    take_profit REAL,
    open_time TEXT NOT NULL,
    close_time TEXT NOT NULL,
    profit REAL NOT NULL DEFAULT 0.0,
    commission REAL NOT NULL DEFAULT 0.0,
    swap REAL NOT NULL DEFAULT 0.0,
    pips REAL NOT NULL DEFAULT 0.0,
    rr_ratio REAL,
    duration_minutes INTEGER NOT NULL DEFAULT 0,
    close_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(ticket, account_id)
);

CREATE TABLE IF NOT EXISTS daily_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    report_date TEXT NOT NULL,
    total_trades INTEGER NOT NULL DEFAULT 0,
    winning_trades INTEGER NOT NULL DEFAULT 0,
    losing_trades INTEGER NOT NULL DEFAULT 0,
    break_even_trades INTEGER NOT NULL DEFAULT 0,
    gross_profit REAL NOT NULL DEFAULT 0.0,
    gross_loss REAL NOT NULL DEFAULT 0.0,
    total_commission REAL NOT NULL DEFAULT 0.0,
    total_swap REAL NOT NULL DEFAULT 0.0,
    net_profit REAL NOT NULL DEFAULT 0.0,
    win_rate REAL NOT NULL DEFAULT 0.0,
    average_rr REAL NOT NULL DEFAULT 0.0,
    average_trade_profit REAL NOT NULL DEFAULT 0.0,
    average_winner REAL NOT NULL DEFAULT 0.0,
    average_loser REAL NOT NULL DEFAULT 0.0,
    max_drawdown REAL NOT NULL DEFAULT 0.0,
    max_drawdown_percent REAL NOT NULL DEFAULT 0.0,
    best_trade_profit REAL NOT NULL DEFAULT 0.0,
    worst_trade_profit REAL NOT NULL DEFAULT 0.0,
    total_pips REAL NOT NULL DEFAULT 0.0,
    starting_balance REAL NOT NULL DEFAULT 0.0,
    ending_balance REAL NOT NULL DEFAULT 0.0,
    profit_factor REAL NOT NULL DEFAULT 0.0,
    sharpe_ratio REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(account_id, report_date)
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    username TEXT,
    action TEXT NOT NULL,
    description TEXT NOT NULL,
    target TEXT,
    old_value TEXT,
    new_value TEXT,
    ip_address TEXT,
    success INTEGER NOT NULL DEFAULT 1,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_telegram_id ON audit_logs(telegram_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_trade_records_account_open ON trade_records(account_id, open_time);
CREATE INDEX IF NOT EXISTS idx_daily_reports_account_date ON daily_reports(account_id, report_date);
CREATE INDEX IF NOT EXISTS idx_notification_logs_type ON notification_logs(notification_type, sent_at);
CREATE INDEX IF NOT EXISTS idx_user_sessions_telegram ON user_sessions(telegram_id, is_active);
"""


class Database:
    """
    Async SQLite database wrapper.
    All writes are serialized through the connection pool.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._connection = None
        self._initialized = False

    async def initialize(self) -> None:
        """Create tables and indexes if they don't exist."""
        import aiosqlite
        # Ensure directory exists — guard against paths with no directory component
        # (e.g. just "panel.db") where dirname returns "" and makedirs would raise.
        dir_path = os.path.dirname(self._path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            await db.executescript(_SCHEMA_SQL)
            # Migrate only the old built-in default. Any operator-selected
            # threshold (including values other than 60) is preserved.
            cursor = await db.execute(
                """
                UPDATE strategy_configs
                   SET min_confidence_score = 49.0
                 WHERE min_confidence_score = 60.0
                """
            )
            migrated_rows = cursor.rowcount
            await db.commit()

        self._initialized = True
        if migrated_rows:
            logger.info(
                "Migrated %s legacy strategy confidence default(s) from 60.0 to 49.0",
                migrated_rows,
            )
        logger.info(f"Database initialized at: {self._path}")

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator:
        """Get a database connection context manager."""
        import aiosqlite
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            try:
                yield db
            except Exception:
                await db.rollback()
                raise

    async def close(self) -> None:
        logger.info("Database connection closed")


@lru_cache(maxsize=1)
def get_database() -> Database:
    from ..config.settings import get_settings
    settings = get_settings()
    return Database(settings.database.path)

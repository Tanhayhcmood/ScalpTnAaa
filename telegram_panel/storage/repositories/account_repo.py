"""
Account Repository — all DB operations for accounts.
Repository pattern: no SQL outside this file.
"""

import json
import logging
from typing import Optional
from ..database import Database
from ...models.account import Account
from ...config.constants import AccountType, ConnectionStatus
from ...utils.time import parse_utc, utc_now

logger = logging.getLogger(__name__)


class AccountRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_all(self, active_only: bool = False) -> list[Account]:
        query = "SELECT * FROM accounts"
        if active_only:
            query += " WHERE is_active = 1"
        query += " ORDER BY name"
        async with self._db.connection() as db:
            cursor = await db.execute(query)
            rows = await cursor.fetchall()
        return [self._row_to_account(r) for r in rows]

    async def get_by_id(self, account_id: int) -> Optional[Account]:
        async with self._db.connection() as db:
            cursor = await db.execute(
                "SELECT * FROM accounts WHERE id = ?", (account_id,)
            )
            row = await cursor.fetchone()
        return self._row_to_account(row) if row else None

    async def get_active(self) -> Optional[Account]:
        """Get the currently active (enabled) account."""
        async with self._db.connection() as db:
            cursor = await db.execute(
                "SELECT * FROM accounts WHERE is_active = 1 AND is_enabled = 1 "
                "ORDER BY updated_at DESC LIMIT 1"
            )
            row = await cursor.fetchone()
        return self._row_to_account(row) if row else None

    async def create(self, account: Account) -> Account:
        async with self._db.connection() as db:
            cursor = await db.execute(
                """INSERT INTO accounts
                   (name, account_type, broker, server, login, password_encrypted,
                    is_active, is_enabled, currency, leverage,
                    prop_firm_name, prop_challenge_phase, prop_max_daily_loss,
                    prop_max_total_loss, prop_profit_target, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    account.name, account.account_type.value, account.broker,
                    account.server, account.login, account.password_encrypted,
                    1 if account.is_active else 0,
                    1 if account.is_enabled else 0,
                    account.currency, account.leverage,
                    account.prop_firm_name, account.prop_challenge_phase,
                    account.prop_max_daily_loss, account.prop_max_total_loss,
                    account.prop_profit_target, account.notes,
                ),
            )
            await db.commit()
            account.id = cursor.lastrowid
        logger.info(f"Created account id={account.id} name={account.name}")
        return account

    async def update(self, account: Account) -> bool:
        account.updated_at = utc_now()
        async with self._db.connection() as db:
            await db.execute(
                """UPDATE accounts SET
                   name=?, account_type=?, broker=?, server=?, login=?,
                   is_active=?, is_enabled=?, currency=?, leverage=?,
                   prop_firm_name=?, prop_challenge_phase=?, prop_max_daily_loss=?,
                   prop_max_total_loss=?, prop_profit_target=?, notes=?,
                   updated_at=?
                   WHERE id=?""",
                (
                    account.name, account.account_type.value, account.broker,
                    account.server, account.login,
                    1 if account.is_active else 0,
                    1 if account.is_enabled else 0,
                    account.currency, account.leverage,
                    account.prop_firm_name, account.prop_challenge_phase,
                    account.prop_max_daily_loss, account.prop_max_total_loss,
                    account.prop_profit_target, account.notes,
                    account.updated_at.isoformat(), account.id,
                ),
            )
            await db.commit()
        return True

    async def update_password(self, account_id: int, encrypted_password: str) -> bool:
        async with self._db.connection() as db:
            await db.execute(
                "UPDATE accounts SET password_encrypted=?, updated_at=? WHERE id=?",
                (encrypted_password, utc_now().isoformat(), account_id),
            )
            await db.commit()
        return True

    async def delete(self, account_id: int) -> bool:
        async with self._db.connection() as db:
            await db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            await db.commit()
        logger.info(f"Deleted account id={account_id}")
        return True

    async def set_enabled(self, account_id: int, enabled: bool) -> bool:
        async with self._db.connection() as db:
            await db.execute(
                "UPDATE accounts SET is_enabled=?, updated_at=? WHERE id=?",
                (1 if enabled else 0, utc_now().isoformat(), account_id),
            )
            await db.commit()
        return True

    async def switch_active(self, account_id: int) -> bool:
        """Make one account the primary active account."""
        async with self._db.connection() as db:
            await db.execute(
                "UPDATE accounts SET is_active=0, updated_at=?",
                (utc_now().isoformat(),),
            )
            await db.execute(
                "UPDATE accounts SET is_active=1, updated_at=? WHERE id=?",
                (utc_now().isoformat(), account_id),
            )
            await db.commit()
        return True

    def _row_to_account(self, row) -> Account:
        return Account(
            id=row["id"],
            name=row["name"],
            account_type=AccountType(row["account_type"]),
            broker=row["broker"],
            server=row["server"],
            login=row["login"],
            password_encrypted=row["password_encrypted"],
            is_active=bool(row["is_active"]),
            is_enabled=bool(row["is_enabled"]),
            currency=row["currency"],
            leverage=row["leverage"],
            prop_firm_name=row["prop_firm_name"],
            prop_challenge_phase=row["prop_challenge_phase"],
            prop_max_daily_loss=row["prop_max_daily_loss"],
            prop_max_total_loss=row["prop_max_total_loss"],
            prop_profit_target=row["prop_profit_target"],
            notes=row["notes"],
            created_at=parse_utc(row["created_at"]) or utc_now(),
            updated_at=parse_utc(row["updated_at"]) or utc_now(),
        )

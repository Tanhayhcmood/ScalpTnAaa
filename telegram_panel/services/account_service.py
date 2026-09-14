"""
Account Service — business logic for account management.

FIX: test_connection() now performs a real live check:
  1. Bypasses the in-memory MT5 snapshot cache
  2. Calls robot's /status endpoint directly via HTTP (or Redis) for fresh data
  3. Checks last_heartbeat freshness to detect a crashed/stopped robot
  4. Returns actual connection status, balance, and data age
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from ..models.account import Account
from ..config.constants import AccountType, ConnectionStatus
from ..storage.repositories.account_repo import AccountRepository
from ..storage.encryption import EncryptionService

logger = logging.getLogger(__name__)

# If last_heartbeat is older than this, report robot as disconnected.
_STALE_THRESHOLD_SECONDS = 180  # 3 minutes


class AccountService:
    def __init__(
        self,
        account_repo: AccountRepository,
        encryption: EncryptionService,
        mt5_service=None,
        robot_service=None,
    ) -> None:
        self._repo = account_repo
        self._encryption = encryption
        self._mt5 = mt5_service
        self._robot = robot_service  # Optional; used for live connection test

    async def get_all_accounts(self) -> list[Account]:
        accounts = await self._repo.get_all()
        # Enrich with live MT5 data if available
        for account in accounts:
            if account.is_enabled and self._mt5:
                try:
                    info = await self._mt5.get_account_info(account)
                    account.balance = info.get("balance", account.balance)
                    account.equity = info.get("equity", account.equity)
                    account.margin = info.get("margin", account.margin)
                    account.free_margin = info.get("free_margin", account.free_margin)
                    account.margin_level = info.get("margin_level", account.margin_level)
                    account.floating_profit = info.get("floating_profit", account.floating_profit)
                    account.leverage         = info.get("leverage",         account.leverage)
                    raw_status = info.get("connection_status", "disconnected")
                    try:
                        account.connection_status = ConnectionStatus(raw_status)
                    except ValueError:
                        account.connection_status = ConnectionStatus.DISCONNECTED
                except Exception as e:
                    logger.warning(f"Failed to enrich account {account.id}: {e}")
        return accounts

    async def get_account(self, account_id: int) -> Optional[Account]:
        account = await self._repo.get_by_id(account_id)
        if account and self._mt5:
            try:
                info = await self._mt5.get_account_info(account)
                account.balance          = info.get("balance",          account.balance)
                account.equity           = info.get("equity",           account.equity)
                account.margin           = info.get("margin",           account.margin)
                account.free_margin      = info.get("free_margin",      account.free_margin)
                account.margin_level     = info.get("margin_level",     account.margin_level)
                account.floating_profit  = info.get("floating_profit",  account.floating_profit)
                account.leverage         = info.get("leverage",         account.leverage)
                raw_status = info.get("connection_status", "disconnected")
                try:
                    account.connection_status = ConnectionStatus(raw_status)
                except ValueError:
                    account.connection_status = ConnectionStatus.DISCONNECTED
            except Exception as e:
                logger.warning(f"Failed to enrich account {account_id}: {e}")
        return account

    async def get_active_account(self) -> Optional[Account]:
        account = await self._repo.get_active()
        if account and self._mt5:
            try:
                info = await self._mt5.get_account_info(account)
                account.balance          = info.get("balance",          account.balance)
                account.equity           = info.get("equity",           account.equity)
                account.margin           = info.get("margin",           account.margin)
                account.free_margin      = info.get("free_margin",      account.free_margin)
                account.margin_level     = info.get("margin_level",     account.margin_level)
                account.floating_profit  = info.get("floating_profit",  account.floating_profit)
                account.leverage         = info.get("leverage",         account.leverage)
                # Update identity fields from live MT5 state when DB values are blank.
                # This fixes the case where auto_seed used truncated/placeholder names.
                if info.get("broker"):
                    account.broker = info["broker"]
                if info.get("login"):
                    account.login = info["login"]
                if info.get("server"):
                    account.server = info["server"]
                if info.get("currency"):
                    account.currency = info["currency"]
                raw_status = info.get("connection_status", "disconnected")
                try:
                    account.connection_status = ConnectionStatus(raw_status)
                except ValueError:
                    account.connection_status = ConnectionStatus.DISCONNECTED
            except Exception as e:
                logger.warning(f"Failed to enrich active account: {e}")
        return account

    async def add_account(
        self,
        name: str,
        account_type: AccountType,
        broker: str,
        server: str,
        login: str,
        password: str,
        **kwargs,
    ) -> Account:
        encrypted_pw = self._encryption.encrypt(password)
        account = Account(
            id=None,
            name=name,
            account_type=account_type,
            broker=broker,
            server=server,
            login=login,
            password_encrypted=encrypted_pw,
            **kwargs,
        )
        return await self._repo.create(account)

    async def delete_account(self, account_id: int) -> bool:
        return await self._repo.delete(account_id)

    async def switch_account(self, account_id: int) -> bool:
        return await self._repo.switch_active(account_id)

    async def enable_account(self, account_id: int) -> bool:
        return await self._repo.set_enabled(account_id, True)

    async def disable_account(self, account_id: int) -> bool:
        return await self._repo.set_enabled(account_id, False)

    async def test_connection(self, account_id: int) -> dict:
        """
        Perform a REAL live connection test.

        FIX: Previously this read from a potentially stale cache and always
        reported "connected" if the robot had recently been running.

        Now:
          1. Force-bypasses MT5 snapshot cache for fresh data
          2. Uses robot_service.get_fresh_state() to get non-cached robot state
          3. Checks heartbeat age — if robot is stale/down, reports disconnected
          4. Returns detailed status: robot alive, MT5 connected, balance, data age

        The result accurately reflects the CURRENT state of the connection.
        """
        account = await self._repo.get_by_id(account_id)
        if not account:
            return {"success": False, "error": "Account not found"}

        checked_at = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        # ── Step 1: Get fresh robot state (bypasses cache) ────────────────────
        robot_state: dict = {}
        data_age_seconds: int = -1
        robot_alive = False
        heartbeat_str = "—"

        if self._robot:
            try:
                robot_state = await self._robot.get_fresh_state()
                last_hb = robot_state.get("last_heartbeat")
                if last_hb:
                    try:
                        hb_dt = datetime.fromisoformat(
                            str(last_hb).replace("Z", "+00:00")
                        )
                        if hb_dt.tzinfo is None:
                            hb_dt = hb_dt.replace(tzinfo=timezone.utc)
                        now = datetime.now(timezone.utc)
                        data_age_seconds = int((now - hb_dt).total_seconds())
                        robot_alive = data_age_seconds <= _STALE_THRESHOLD_SECONDS
                        heartbeat_str = f"{data_age_seconds}s ago"
                    except Exception:
                        pass
                else:
                    robot_alive = False
            except Exception as e:
                logger.warning(f"test_connection: robot state fetch failed: {e}")

        # ── Step 2: If robot is not alive, no MT5 connection is possible ──────
        if not robot_alive:
            age_msg = (
                f"{data_age_seconds}s ago" if data_age_seconds >= 0 else "never"
            )
            return {
                "success": False,
                "error": (
                    f"Robot is offline (last seen: {age_msg}). "
                    "MT5 connection cannot be verified."
                ),
                "robot_alive": False,
                "robot_status": robot_state.get("status", "unknown"),
                "data_age_seconds": data_age_seconds,
                "last_heartbeat": heartbeat_str,
                "checked_at": checked_at,
            }

        # ── Step 3: Get fresh MT5 snapshot (bypasses cache) ───────────────────
        if self._mt5:
            try:
                snapshot = await self._mt5.get_fresh_snapshot()
                info = snapshot.get("account_info", {})
                # Also check robot state for account info (state_writer writes it there too)
                if not info and robot_state:
                    raw_account = robot_state.get("account", {})
                    if raw_account:
                        info = {
                            "balance": raw_account.get("balance", 0.0),
                            "equity": raw_account.get("equity", 0.0),
                            "currency": raw_account.get("currency", "USD"),
                            "connection_status": robot_state.get("connection_status", "disconnected"),
                        }

                conn_status = info.get("connection_status", "disconnected")
                mt5_connected = conn_status.lower() == "connected"

                if not mt5_connected:
                    # Robot is alive but MT5 bridge is disconnected
                    return {
                        "success": False,
                        "error": f"Robot is running but MT5 is {conn_status}",
                        "robot_alive": True,
                        "robot_status": robot_state.get("status", "running"),
                        "mt5_status": conn_status,
                        "data_age_seconds": data_age_seconds,
                        "last_heartbeat": heartbeat_str,
                        "checked_at": checked_at,
                    }

                return {
                    "success": True,
                    "status": "connected",
                    "robot_alive": True,
                    "robot_status": robot_state.get("status", "running"),
                    "mt5_status": "connected",
                    "balance": info.get("balance", 0.0),
                    "equity": info.get("equity", 0.0),
                    "currency": info.get("currency", "USD"),
                    "data_age_seconds": data_age_seconds,
                    "last_heartbeat": heartbeat_str,
                    "checked_at": checked_at,
                }
            except Exception as e:
                logger.warning(f"test_connection: MT5 snapshot fetch failed: {e}")
                return {"success": False, "error": str(e), "checked_at": checked_at}

        # No MT5 service configured but robot is alive
        return {
            "success": True,
            "status": "robot_alive_no_mt5_service",
            "robot_alive": True,
            "robot_status": robot_state.get("status", "running"),
            "balance": 0.0,
            "data_age_seconds": data_age_seconds,
            "last_heartbeat": heartbeat_str,
            "checked_at": checked_at,
        }

    async def reconnect(self, account_id: int) -> bool:
        """Send RECONNECT command to the robot via Redis (or HTTP fallback).

        Fixed: previously called self._mt5.send_trade_command() which does not
        exist on MT5Service and always raised AttributeError.  The reconnect
        command must go through the robot service so the trading engine can
        trigger an MT5 disconnect/reconnect cycle via its command processor.
        """
        if self._robot:
            return await self._robot.send_command("RECONNECT")
        return False

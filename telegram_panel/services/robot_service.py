"""
Robot Service — interface between Telegram panel and the trading engine.

Communication strategy (configurable via ROBOT_INTERFACE_MODE):
  'file'   — reads robot state from a JSON file that the engine writes (safest,
              zero coupling to the engine process)
  'http'   — calls an HTTP status endpoint on the engine (optional)
  'redis'  — reads from shared Redis instance (primary for Render multi-service)

FIX: Added heartbeat staleness detection — if last_heartbeat is older than
_STALE_THRESHOLD_SECONDS, the state is marked as stale and status is
overridden to 'disconnected' to prevent showing fake "running" status
when the robot has crashed or been stopped.

FIX: When ROBOT_INTERFACE_MODE=redis and Redis is empty/unavailable,
falls back to HTTP if ROBOT_BASE_URL is configured instead of returning
the static _DEFAULT_STATE.
"""

import os
import json
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Any
from ..config.constants import RobotStatus, ConnectionStatus

logger = logging.getLogger(__name__)

# If last_heartbeat is older than this, mark status as DISCONNECTED regardless
# of what Redis/file says.  This prevents showing "RUNNING" for up to 5 minutes
# after the robot crashes (Redis TTL window).
_STALE_THRESHOLD_SECONDS = 180  # 3 minutes

_DEFAULT_STATE: dict[str, Any] = {
    "status": "stopped",
    "version": "unknown",
    "uptime_seconds": 0,
    "last_heartbeat": None,
    "connection_status": "disconnected",
    "mt5_status": "disconnected",
    "vps_status": "unknown",
    "active_trades": 0,
    "pending_orders": 0,
    # Signal eligibility and final trade permission are different concepts.
    # Default closed so a missing/stale field can never authorize an entry.
    "trade_permission": {
        "allowed": False,
        "stage": "NOT_EVALUATED",
        "reasons": ["No live entry gate evaluation yet"],
    },
    "last_error": None,
}


def _parse_heartbeat(value: object) -> Optional[datetime]:
    """Parse an ISO heartbeat string to a UTC-aware datetime, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def _apply_staleness_guard(state: dict[str, Any]) -> dict[str, Any]:
    """
    If last_heartbeat is missing or too old, override status/connection fields
    to reflect the real situation: robot is not alive / not connected.

    This prevents stale Redis data (written before the robot crashed) from
    being displayed as live data in the Telegram panel.
    """
    hb = _parse_heartbeat(state.get("last_heartbeat"))
    if hb is None:
        # No heartbeat at all — treat as disconnected
        out = dict(state)
        out["status"] = "disconnected"
        out["connection_status"] = "disconnected"
        out["mt5_status"] = "disconnected"
        out["_data_fresh"] = False
        out["_data_age_seconds"] = -1
        return out

    now = datetime.now(timezone.utc)
    age = (now - hb).total_seconds()
    out = dict(state)
    out["_data_age_seconds"] = int(age)
    out["_data_fresh"] = age <= _STALE_THRESHOLD_SECONDS

    if age > _STALE_THRESHOLD_SECONDS:
        # Data is stale — robot is not sending heartbeats → it is down
        out["status"] = "disconnected"
        out["connection_status"] = "disconnected"
        out["mt5_status"] = "disconnected"
        logger.debug(
            f"Robot state is stale ({age:.0f}s > {_STALE_THRESHOLD_SECONDS}s) "
            "— marking as disconnected"
        )

    return out


class RobotService:
    """
    Read-only interface to the trading robot state.
    Control commands are written to a command file (inbox).
    All reads are non-blocking — never affects trading performance.
    """

    def __init__(
        self,
        state_path: str = "robot_state.json",
        config_path: str = "robot_config.json",
        interface_mode: str = "file",
        base_url: str = "",
    ) -> None:
        self._state_path = state_path
        self._config_path = config_path
        self._cmd_path = state_path.replace("state", "commands")
        self._interface_mode = interface_mode
        # base_url: full URL of the robot service (e.g. https://goper-v4-robot.onrender.com).
        # Used by _read_state_http for cross-service communication on Render.
        # When empty, falls back to localhost:{_http_port} for single-machine deployments.
        self._base_url: str = base_url.rstrip("/") if base_url else ""
        self._http_port: int = 0  # fallback for single-machine; unused when base_url is set
        self._cached_state: dict[str, Any] = dict(_DEFAULT_STATE)
        self._cache_ts: Optional[float] = None
        self._cache_ttl: float = 5.0    # seconds
        # Persistent aiohttp session reused across all HTTP calls.
        # Created lazily on first use; closed explicitly on shutdown.
        self._http_session: "Optional[Any]" = None

    def get_base_url(self) -> str:
        """Expose robot base URL for other services (e.g. MT5Service HTTP fallback)."""
        return self._base_url

    async def close(self) -> None:
        """Close the persistent HTTP session. Call on panel shutdown."""
        if self._http_session is not None and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None

    def _get_http_session(self) -> "Any":
        """Return a shared aiohttp.ClientSession, creating it if needed."""
        import aiohttp
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    # ─── Status ──────────────────────────────────────────────────────────────

    async def get_status(self) -> RobotStatus:
        state = await self._read_state()
        raw = state.get("status", "stopped")
        try:
            return RobotStatus(raw)
        except ValueError:
            return RobotStatus.STOPPED

    async def get_state(self) -> dict[str, Any]:
        return await self._read_state()

    async def get_version(self) -> str:
        state = await self._read_state()
        return state.get("version", "v4.0.0")

    async def get_uptime(self) -> int:
        state = await self._read_state()
        return state.get("uptime_seconds", 0)

    async def get_last_heartbeat(self) -> Optional[datetime]:
        state = await self._read_state()
        raw = state.get("last_heartbeat")
        if raw:
            try:
                return datetime.fromisoformat(raw)
            except (ValueError, TypeError):
                pass
        return None

    async def get_mt5_status(self) -> ConnectionStatus:
        state = await self._read_state()
        raw = state.get("mt5_status", "disconnected")
        try:
            return ConnectionStatus(raw)
        except ValueError:
            return ConnectionStatus.DISCONNECTED

    async def get_connection_status(self) -> ConnectionStatus:
        state = await self._read_state()
        raw = state.get("connection_status", "disconnected")
        try:
            return ConnectionStatus(raw)
        except ValueError:
            return ConnectionStatus.DISCONNECTED

    async def get_trade_permission(self) -> dict[str, Any]:
        """Return final live entry permission, never the signal's ``allowed``."""
        state = await self._read_state()
        permission = state.get("trade_permission")
        if not isinstance(permission, dict):
            return dict(_DEFAULT_STATE["trade_permission"])
        return {
            "allowed": bool(permission.get("allowed", False)),
            "stage": str(permission.get("stage", "NOT_EVALUATED")),
            "reasons": [
                str(reason)
                for reason in permission.get("reasons", [])
                if str(reason).strip()
            ],
        }

    async def get_config(self) -> dict[str, Any]:
        try:
            loop = asyncio.get_running_loop()
            def _read():
                with open(self._config_path, "r") as f:
                    return json.load(f)
            return await loop.run_in_executor(None, _read)
        except Exception:
            return {}

    async def is_running(self) -> bool:
        status = await self.get_status()
        return status in (
            RobotStatus.RUNNING, RobotStatus.SCANNING,
            RobotStatus.WAITING, RobotStatus.HOLDING,
        )

    # ─── Live state read bypassing cache (for test_connection) ───────────────

    async def get_fresh_state(self) -> dict[str, Any]:
        """Force a fresh read bypassing the in-memory cache.
        Used by test_connection to avoid showing stale cached data.
        """
        old_ts = self._cache_ts
        self._cache_ts = None  # invalidate cache
        try:
            return await self._read_state()
        finally:
            # Restore old cache ts if fresh read fails (returns default)
            if self._cached_state == _DEFAULT_STATE:
                self._cache_ts = old_ts

    # ─── Control ─────────────────────────────────────────────────────────────

    async def start(self) -> bool:
        return await self._send_command("START")

    async def pause(self) -> bool:
        return await self._send_command("PAUSE")

    async def resume(self) -> bool:
        return await self._send_command("RESUME")

    async def emergency_stop(self) -> bool:
        return await self._send_command("EMERGENCY_STOP")

    async def safe_shutdown(self) -> bool:
        return await self._send_command("SAFE_SHUTDOWN")

    async def restart_engine(self) -> bool:
        return await self._send_command("RESTART_ENGINE")

    async def restart_mt5(self) -> bool:
        return await self._send_command("RESTART_MT5")

    async def restart_telegram(self) -> bool:
        return await self._send_command("RESTART_TELEGRAM")

    async def push_risk_config(self, config: dict[str, Any]) -> bool:
        return await self._send_command("UPDATE_RISK", payload=config)

    async def push_strategy_config(self, config: dict[str, Any]) -> bool:
        return await self._send_command("UPDATE_STRATEGY", payload=config)

    async def send_command(self, command: str, payload: Optional[dict] = None) -> bool:
        """Public wrapper for _send_command — used by account_service and external callers."""
        return await self._send_command(command, payload)

    # ─── Private ─────────────────────────────────────────────────────────────

    async def _read_state(self) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._cache_ts and (now - self._cache_ts) < self._cache_ttl:
            return self._cached_state

        # Try Redis first — required when robot and panel run as separate Render services.
        # Without REDIS_URL this is a no-op and falls through to file/http.
        redis_state = await self._read_state_redis()
        if redis_state is not None:
            guarded = _apply_staleness_guard(redis_state)
            self._cached_state = self._normalize_state(guarded)
            self._cache_ts = now
            return self._cached_state

        # FIX: When interface_mode == "redis" but Redis is unavailable/empty,
        # fall back to HTTP before giving up.  Previously this fell through to
        # `else: state = dict(_DEFAULT_STATE)` which returned wrong defaults.
        if self._interface_mode == "file":
            state = await self._read_state_file()
        elif self._interface_mode in ("http", "redis"):
            # "redis" mode uses Redis as primary (already tried above) and HTTP
            # as fallback instead of returning bare defaults.
            state = await self._read_state_http()
            if not state or state == _DEFAULT_STATE:
                # HTTP also failed — try file as last resort
                file_state = await self._read_state_file()
                if file_state and file_state != _DEFAULT_STATE:
                    state = file_state
        else:
            state = dict(_DEFAULT_STATE)

        guarded = _apply_staleness_guard(state)
        self._cached_state = self._normalize_state(guarded)
        self._cache_ts = now
        return self._cached_state

    @staticmethod
    def _normalize_state(state: dict[str, Any]) -> dict[str, Any]:
        """Normalize status strings to lowercase for RobotStatus/ConnectionStatus enum compat.

        The trading robot writes uppercase statuses ("RUNNING", "PAUSED", "SCANNING", etc.)
        but the RobotStatus enum uses lowercase values ("running", "paused", "scanning", etc.).
        Without this normalization get_status() always raises ValueError and returns STOPPED,
        the heartbeat monitor never fires RUNNING events, and the dashboard always shows ⚪.
        """
        out = dict(state)
        permission = out.get("trade_permission")
        if not isinstance(permission, dict):
            out["trade_permission"] = dict(_DEFAULT_STATE["trade_permission"])
        else:
            out["trade_permission"] = {
                "allowed": bool(permission.get("allowed", False)),
                "stage": str(permission.get("stage", "NOT_EVALUATED")),
                "reasons": [
                    str(reason)
                    for reason in permission.get("reasons", [])
                    if str(reason).strip()
                ],
            }
        for key in ("status", "mt5_status", "connection_status"):
            if key in out and isinstance(out[key], str):
                out[key] = out[key].lower()
        return out

    async def _read_state_file(self) -> dict[str, Any]:
        if not os.path.exists(self._state_path):
            return dict(_DEFAULT_STATE)
        try:
            loop = asyncio.get_running_loop()
            def _read():
                with open(self._state_path, "r") as f:
                    return json.load(f)
            state = await loop.run_in_executor(None, _read)
            return {**_DEFAULT_STATE, **state}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to read robot state file: {e}")
            return dict(_DEFAULT_STATE)

    async def _read_state_redis(self) -> Optional[dict[str, Any]]:
        """Read robot state from Redis (works across separate Render services)."""
        try:
            from telegram_panel.redis_ipc import redis_read_state as _redis_state, redis_available
            if redis_available():
                result = _redis_state()
                if result is not None:
                    return {**_DEFAULT_STATE, **result}
        except Exception as exc:
            logger.debug(f"Redis read_state failed: {exc}")
        return None

    async def _read_state_http(self) -> dict[str, Any]:
        try:
            import aiohttp
            if self._base_url:
                # Cross-service mode (e.g. separate Render services): use configured base URL.
                url = f"{self._base_url}/status"
            elif self._http_port:
                # Single-machine mode: localhost with explicit port.
                url = f"http://127.0.0.1:{self._http_port}/status"
            else:
                logger.debug("HTTP state read skipped: no base_url or http_port configured")
                return dict(_DEFAULT_STATE)
            # NOTE: /status endpoint no longer requires auth (read-only).
            # Token header is still sent for backward compat with old robot deploys.
            headers = {}
            token = os.environ.get("ROBOT_COMMAND_TOKEN", "")
            if token:
                headers["X-Robot-Command-Token"] = token
            session = self._get_http_session()
            async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        return {**_DEFAULT_STATE, **data}
                    logger.debug(f"HTTP /status returned {resp.status}")
        except Exception as e:
            logger.debug(f"HTTP state read failed: {e}")
        return dict(_DEFAULT_STATE)

    async def _send_command(
        self, command: str, payload: Optional[dict] = None
    ) -> bool:
        """
        Write a command to the robot.

        Delivery order:
          1. Redis     — works across separate Render services (primary)
          2. HTTP POST — robot's /command endpoint (Redis-down fallback)
          3. File IPC  — local/single-machine deployments only
        """
        # 1. Redis — primary cross-service channel
        if await self._send_command_redis(command, payload):
            return True
        # 2. HTTP POST to robot's /command endpoint — works on Render even
        #    without Redis because the robot exposes the endpoint on its own
        #    web service URL.
        if await self._send_command_http(command, payload):
            return True
        # 3. File IPC — single-machine / local deployments
        cmd_entry = {
            "command": command,
            "payload": payload or {},
            "issued_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            loop = asyncio.get_running_loop()
            def _write():
                existing = []
                if os.path.exists(self._cmd_path):
                    try:
                        with open(self._cmd_path, "r") as f:
                            existing = json.load(f)
                        if not isinstance(existing, list):
                            existing = []
                    except Exception:
                        existing = []
                existing.append(cmd_entry)
                tmp = self._cmd_path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(existing, f)
                os.replace(tmp, self._cmd_path)
            await loop.run_in_executor(None, _write)
            logger.info(f"Sent command via file: {command}")
            self._cache_ts = None  # invalidate cache so next read reflects new command
            return True
        except Exception as e:
            logger.error(f"Failed to send command {command}: {e}")
            return False

    async def _send_command_redis(self, command: str, payload: Optional[dict] = None) -> bool:
        """Write a command to Redis (works across separate Render services)."""
        try:
            from telegram_panel.redis_ipc import redis_send_command as _redis_cmd
            ok = _redis_cmd(command, payload)
            if ok:
                self._cache_ts = None
                logger.info(f"Sent command via Redis: {command}")
            return ok
        except Exception as exc:
            logger.warning(f"Redis send_command failed: {exc}")
            return False

    async def _send_command_http(self, command: str, payload: Optional[dict] = None) -> bool:
        """POST to the robot's /command HTTP endpoint (Redis-down fallback).

        Only attempted when base_url is configured (i.e. ROBOT_BASE_URL is set
        in the panel's environment — which it is in the Render deploy).
        """
        command_token = os.environ.get("ROBOT_COMMAND_TOKEN", "")
        if not self._base_url or not command_token:
            if self._base_url and not command_token:
                logger.error("HTTP command fallback is not configured: missing ROBOT_COMMAND_TOKEN")
            return False
        url = f"{self._base_url}/command"
        try:
            import aiohttp
            body = {"command": command, "payload": payload or {}}
            session = self._get_http_session()
            async with session.post(
                    url,
                    json=body,
                    headers={"X-Robot-Command-Token": command_token},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        self._cache_ts = None
                        logger.info(f"Sent command via HTTP: {command}")
                        return True
                    logger.warning(
                        f"HTTP command POST returned {resp.status} for {command}"
                    )
                    return False
        except Exception as exc:
            logger.warning(f"HTTP send_command failed: {exc}")
            return False

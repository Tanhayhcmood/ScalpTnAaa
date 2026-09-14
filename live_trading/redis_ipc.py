"""
Redis IPC — GoldScalperPro v4

Cross-service state sharing between the trading robot and the Telegram panel
when both run as separate Render services (separate filesystems).

When REDIS_URL is set:
  - Robot writes state to Redis; panel reads state from Redis.
  - Panel writes commands to Redis; robot reads commands from Redis.

When REDIS_URL is not set or Redis is unreachable:
  - All functions return None/False silently.
  - Callers automatically fall back to file-based IPC.

Redis keys
  goldscalper:state    — robot state JSON (TTL 5 min)
  goldscalper:snapshot — MT5 snapshot JSON (TTL 5 min)
  goldscalper:commands — pending commands dict in engine-key format (TTL 5 min)
  goldscalper:guardian — RiskGuardian circuit-breaker state (TTL 27 hours)
"""

import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "")

_STATE_KEY    = "goldscalper:state"
_SNAPSHOT_KEY = "goldscalper:snapshot"
_COMMANDS_KEY = "goldscalper:commands"
_GUARDIAN_KEY = "goldscalper:guardian"
_STATE_TTL    = 300   # 5 min — prevents stale reads if robot crashes
_CMD_TTL      = 300   # 5 min — stale commands become irrelevant
_GUARDIAN_TTL = 97200  # 27h — covers the 26h freshness window after a restart

_client              = None
_last_failure_time: float = 0.0   # monotonic timestamp of last connection failure
_last_ping_time:    float = 0.0   # monotonic timestamp of last successful ping
_RETRY_COOLDOWN      = 60.0        # seconds before retrying after a failure
_PING_INTERVAL       = 30.0        # seconds between keepalive pings on good client


def _get_client():
    """
    Return a live Redis client, or None if Redis is unavailable.

    Connection management:
    - If we have a cached client, we send a keepalive ping at most once every
      30 seconds (not on every call) to avoid hammering Redis with pings.
    - After any failure, we enforce a 60-second cooldown before retrying.
    - This replaces the old permanent _unavailable=True flag that prevented
      any retry once Redis failed at startup.
    """
    global _client, _last_failure_time, _last_ping_time

    if not REDIS_URL:
        return None

    # If we have a cached client, check liveness at most once per _PING_INTERVAL.
    if _client is not None:
        now = time.monotonic()
        if (now - _last_ping_time) >= _PING_INTERVAL:
            try:
                _client.ping()
                _last_ping_time = now
            except Exception:
                logger.warning("Redis IPC: keepalive ping failed — will reconnect")
                _client = None
                _last_failure_time = now
                _last_ping_time = 0.0
        if _client is not None:
            return _client

    # Enforce retry cooldown after a failure.
    if _last_failure_time > 0:
        elapsed = time.monotonic() - _last_failure_time
        if elapsed < _RETRY_COOLDOWN:
            return None

    try:
        import redis as _redis_lib
        c = _redis_lib.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
        c.ping()
        _client = c
        _last_failure_time = 0.0
        _last_ping_time = time.monotonic()
        logger.info("Redis IPC: connected")
        return _client
    except Exception as exc:
        logger.warning(
            "Redis IPC unavailable (retry in %.0fs): %s",
            _RETRY_COOLDOWN, exc,
        )
        _last_failure_time = time.monotonic()
        return None


def redis_available() -> bool:
    """True when Redis is configured and reachable."""
    return _get_client() is not None


# ─── Robot writes state to Redis ──────────────────────────────────────────────

def redis_write_state(data: dict) -> bool:
    r = _get_client()
    if r is None:
        return False
    try:
        r.set(_STATE_KEY, json.dumps(data, default=str), ex=_STATE_TTL)
        return True
    except Exception as exc:
        logger.warning("Redis write_state: %s", exc)
        _reset_client()
        return False


def redis_write_snapshot(data: dict) -> bool:
    r = _get_client()
    if r is None:
        return False
    try:
        r.set(_SNAPSHOT_KEY, json.dumps(data, default=str), ex=_STATE_TTL)
        return True
    except Exception as exc:
        logger.warning("Redis write_snapshot: %s", exc)
        _reset_client()
        return False


# ─── Robot reads commands from Redis ──────────────────────────────────────────

def redis_read_commands() -> Optional[dict]:
    """
    Return pending commands dict (engine-key format) or None if unavailable.
    Returns {} when Redis is reachable but no commands are queued.
    """
    r = _get_client()
    if r is None:
        return None
    try:
        raw = r.get(_COMMANDS_KEY)
        if not raw:
            return {}
        return json.loads(raw)
    except Exception as exc:
        logger.warning("Redis read_commands: %s", exc)
        _reset_client()
        return None


def redis_clear_command(key: str) -> bool:
    """Remove one engine-key from the Redis command dict."""
    r = _get_client()
    if r is None:
        return False
    try:
        raw = r.get(_COMMANDS_KEY)
        if raw:
            cmds = json.loads(raw)
            if key in cmds:
                cmds.pop(key)
                r.set(_COMMANDS_KEY, json.dumps(cmds), ex=_CMD_TTL)
        return True
    except Exception as exc:
        logger.warning("Redis clear_command: %s", exc)
        _reset_client()
        return False


# ─── Panel reads state from Redis ─────────────────────────────────────────────

def redis_read_state() -> Optional[dict]:
    r = _get_client()
    if r is None:
        return None
    try:
        raw = r.get(_STATE_KEY)
        return json.loads(raw) if raw else None
    except Exception as exc:
        logger.warning("Redis read_state: %s", exc)
        _reset_client()
        return None


def redis_read_snapshot() -> Optional[dict]:
    r = _get_client()
    if r is None:
        return None
    try:
        raw = r.get(_SNAPSHOT_KEY)
        return json.loads(raw) if raw else None
    except Exception as exc:
        logger.warning("Redis read_snapshot: %s", exc)
        _reset_client()
        return None


# ─── Robot persists RiskGuardian state ────────────────────────────────────────

def redis_write_guardian_state(data: dict) -> bool:
    """Persist circuit-breaker baselines and halt state across robot restarts."""
    r = _get_client()
    if r is None:
        return False
    try:
        r.set(_GUARDIAN_KEY, json.dumps(data, default=str), ex=_GUARDIAN_TTL)
        return True
    except Exception as exc:
        logger.warning("Redis write_guardian_state: %s", exc)
        _reset_client()
        return False


def redis_read_guardian_state() -> Optional[dict]:
    """Read the most recent RiskGuardian state, or None when unavailable."""
    r = _get_client()
    if r is None:
        return None
    try:
        raw = r.get(_GUARDIAN_KEY)
        return json.loads(raw) if raw else None
    except Exception as exc:
        logger.warning("Redis read_guardian_state: %s", exc)
        _reset_client()
        return None


# ─── Panel writes commands to Redis ───────────────────────────────────────────

# Translate Telegram panel command names -> engine dict keys
_PANEL_COMMAND_MAP: dict = {
    "PAUSE":          "pause",
    "RESUME":         "resume",
    "EMERGENCY_STOP": "stop",
    "SAFE_SHUTDOWN":  "stop",
    "CLOSE_ALL":      "close_all",
    "RESET_GUARDIAN": "reset_guardian",
    "START":          "start",
    "RESTART_ENGINE": "restart_engine",
    "RESTART_MT5":    "restart_mt5",
    "RESTART_TELEGRAM": "restart_telegram",
    # "RECONNECT" sent by account_service.reconnect() maps to restart_mt5
    "RECONNECT":         "restart_mt5",
}


def redis_send_command(command: str, payload: Optional[dict] = None) -> bool:
    """
    Write a command to Redis (called by Telegram panel).
    Translates panel command name -> engine key and merges into command dict.
    Returns True if written, False if Redis is unavailable.
    """
    r = _get_client()
    if r is None:
        return False
    try:
        engine_key = _PANEL_COMMAND_MAP.get(
            command.strip().upper(), command.strip().lower()
        )
        raw = r.get(_COMMANDS_KEY)
        cmds = json.loads(raw) if raw else {}
        # Store payload alongside the flag so UPDATE_RISK / UPDATE_STRATEGY
        # can carry their config dict to the robot.
        # Empty payloads are still flag commands.  In particular, the panel
        # sends RECONNECT with {}, and the robot's truthy command check would
        # otherwise silently ignore it.
        cmds[engine_key] = payload if payload else True
        r.set(_COMMANDS_KEY, json.dumps(cmds, default=str), ex=_CMD_TTL)
        logger.info("Redis sent command: %s -> %s", command, engine_key)
        return True
    except Exception as exc:
        logger.warning("Redis send_command: %s", exc)
        _reset_client()
        return False


# ─── Internal helpers ──────────────────────────────────────────────────────────

def _reset_client() -> None:
    """Mark client as failed so _get_client() will reconnect after cooldown."""
    global _client, _last_failure_time, _last_ping_time
    _client = None
    _last_failure_time = time.monotonic()
    _last_ping_time = 0.0


# ─── Per-trade strategy explanation (why this trade was opened) ───────────────
# Keyed by ticket rather than merged into goldscalper:snapshot so it survives
# independently of the position/trade-history payloads and expires on its own.
_STRATEGY_TTL = 259200  # 3 days — comfortably covers the notification path


def redis_set_trade_strategy(ticket, strategy: dict) -> bool:
    """
    Publish the decision engine's reasoning for a just-opened trade so the
    Telegram panel can show *why* the robot took it (see
    live_trading.signals.decision_engine.describe_strategy()).

    Best-effort only: called right after order placement succeeds, so a
    Redis hiccup here must never be allowed to look like a trading error —
    callers should not treat a False return as anything more than "the
    notification will be missing strategy details this time".
    """
    r = _get_client()
    if r is None:
        return False
    try:
        r.set(
            f"goldscalper:strategy:{ticket}",
            json.dumps(strategy, default=str),
            ex=_STRATEGY_TTL,
        )
        return True
    except Exception as exc:
        logger.warning("Redis set_trade_strategy: %s", exc)
        _reset_client()
        return False


def redis_update_snapshot_positions(positions: list) -> bool:
    """
    Merge updated open_positions into the live snapshot Redis key.

    Same rationale as redis_update_snapshot_trades() below: the panel's
    MT5Service._read_snapshot() reads goldscalper:snapshot (NOT
    goldscalper:state) for open_positions, but write_mt5_snapshot() only
    runs once per M5 bar (in live_loop._on_new_bar(), BEFORE that bar's
    order is placed). Without this, a newly opened trade would not appear
    as an open position in the panel until the *next* bar — up to 5
    minutes later — even though goldscalper:state already reflects it via
    open_position.

    ROOT-CAUSE FIX: newly opened trades were invisible in the panel's
    "open positions" view for up to one M5 bar because only goldscalper:state
    was updated immediately; goldscalper:snapshot (what the panel actually
    reads for open_positions) waited for the next bar.
    """
    r = _get_client()
    if r is None:
        return False
    try:
        raw = r.get(_SNAPSHOT_KEY)
        snap = json.loads(raw) if raw else {}
        snap["open_positions"] = positions
        r.set(_SNAPSHOT_KEY, json.dumps(snap, default=str), ex=_STATE_TTL)
        return True
    except Exception as exc:
        logger.warning("Redis update_snapshot_positions: %s", exc)
        _reset_client()
        return False


def redis_update_snapshot_trades(trades: list) -> bool:
    """
    Merge updated recent_trades into the live snapshot Redis key.

    Called by write_robot_state() after every trade so the panel's
    MT5Service._read_snapshot() (which reads goldscalper:snapshot, NOT
    goldscalper:state) always gets up-to-date trade history without
    waiting for the next bar when write_mt5_snapshot() would normally run.

    ROOT-CAUSE FIX: trade history was only visible after the next M5 bar
    because write_robot_state() updated goldscalper:state but the panel
    reads goldscalper:snapshot for recent_trades.
    """
    r = _get_client()
    if r is None:
        return False
    try:
        raw = r.get(_SNAPSHOT_KEY)
        snap = json.loads(raw) if raw else {}
        snap["recent_trades"] = trades
        r.set(_SNAPSHOT_KEY, json.dumps(snap, default=str), ex=_STATE_TTL)
        return True
    except Exception as exc:
        logger.warning("Redis update_snapshot_trades: %s", exc)
        _reset_client()
        return False


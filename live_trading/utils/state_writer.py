"""
State Writer — writes robot_state.json and robot_mt5_snapshot.json
so the Telegram Control Panel can display live status and take commands.
"""
import json
import os
from datetime import datetime, timezone
from typing import List, Optional
from live_trading.signals.decision_engine import DecisionResult, describe_strategy
from live_trading.logger import get_logger

# Import env-configured paths from config so that STATE_FILE, MT5_SNAPSHOT,
# and COMMANDS_FILE env-var overrides are honoured here as well as in live_loop.
# config.py has no imports that could cause circular dependency.
from live_trading.config import (
    STATE_FILE    as _CFG_STATE_FILE,
    MT5_SNAPSHOT  as _CFG_SNAPSHOT_FILE,
    COMMANDS_FILE as _CFG_COMMANDS_FILE,
)

log = get_logger()

STATE_FILE    = _CFG_STATE_FILE
SNAPSHOT_FILE = _CFG_SNAPSHOT_FILE
COMMANDS_FILE = _CFG_COMMANDS_FILE

# Track last N trade log entries in state
MAX_TRADE_HISTORY = 50

# Maximum age (in seconds) for a queued command before it is considered stale.
# Prevents commands that were issued before a crash from replaying on restart.
_COMMAND_MAX_AGE_SECONDS = int(os.getenv("COMMAND_MAX_AGE_SECONDS", "300"))

# Explicit mapping from Telegram panel command names to engine dict keys.
# Panel commands not present in this table are silently ignored.
# Keep this map in sync with _process_commands() in live_trading/trading/live_loop.py
# and _PANEL_COMMAND_MAP in live_trading/redis_ipc.py.
_PANEL_COMMAND_MAP: dict = {
    "PAUSE":            "pause",
    "RESUME":           "resume",
    "EMERGENCY_STOP":   "stop",
    "SAFE_SHUTDOWN":    "stop",
    "CLOSE_ALL":        "close_all",
    "RESET_GUARDIAN":   "reset_guardian",
    # Commands below were missing from the file-based fallback path;
    # they are now mirrored from live_trading/redis_ipc.py.
    "START":            "start",
    "RESTART_ENGINE":   "restart_engine",
    "RESTART_MT5":      "restart_mt5",
    # "RECONNECT" is sent by the panel's account_service.reconnect(); map it
    # to restart_mt5 so it actually triggers a reconnect in the live loop.
    "RECONNECT":        "restart_mt5",
    "RESTART_TELEGRAM": "restart_telegram",
}

# ── Engine version and uptime tracking ───────────────────────────────────────
# Version string surfaced to the Telegram panel's dashboard.
VERSION = "v4.0.5"

# Module-level start time — set on the first write_robot_state() call so that
# uptime_seconds is accurate from engine start regardless of when the module
# is imported.  Using None until first call avoids a spurious 0-uptime flash.
_engine_start_time: Optional[datetime] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_uptime_seconds() -> int:
    """Return seconds since the first write_robot_state() call."""
    global _engine_start_time
    now = datetime.now(timezone.utc)
    if _engine_start_time is None:
        _engine_start_time = now
        return 0
    return int((now - _engine_start_time).total_seconds())


def _safe_write(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    try:
        # Ensure parent directory exists — required when STATE_FILE / SNAPSHOT_FILE
        # is configured to a non-/tmp path such as /data/robot_state.json on a
        # Render persistent disk that may not have been pre-created.
        dir_path = os.path.dirname(path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception as exc:
        log.error(f"State write failed ({path}): {exc}")
    # Mirror to Redis for cross-service IPC on Render (no-op if REDIS_URL not set)
    try:
        from live_trading.redis_ipc import redis_write_state, redis_write_snapshot
        if path == STATE_FILE:
            redis_write_state(data)
        elif path == SNAPSHOT_FILE:
            redis_write_snapshot(data)
    except Exception as exc:
        log.debug(f"Redis mirror skipped ({path}): {exc}")


# ── Write robot_state.json ────────────────────────────────────────────────────

def write_robot_state(
    status:            str,        # "RUNNING", "PAUSED", "WAITING", "SCANNING"
    decision:          Optional[DecisionResult],
    open_position:     Optional[dict],
    account_info:      dict,
    trade_history:     List[dict],
    loop_count:        int,
    last_signal_time:  Optional[str] = None,
    extra:             Optional[dict] = None,
    trade_permission:  Optional[dict] = None,
    open_positions:    Optional[List[dict]] = None,
) -> None:

    pos_data = None
    if open_position:
        pos_data = {
            "ticket":    open_position.get("ticket"),
            "symbol":    open_position.get("symbol"),
            "direction": open_position.get("type"),
            "lot_size":  open_position.get("volume"),
            "entry":     open_position.get("open_price"),
            "sl":        open_position.get("sl"),
            "tp":        open_position.get("tp"),
            "profit":    open_position.get("profit"),
            "open_time": open_position.get("open_time"),
        }
    positions_data = []
    source_positions = (
        open_positions
        if open_positions is not None
        else ([] if not open_position else [open_position])
    )
    for position in source_positions:
        positions_data.append({
            "ticket":    position.get("ticket", position.get("id")),
            "symbol":    position.get("symbol"),
            "direction": position.get("type", position.get("direction")),
            "lot_size":  position.get("volume"),
            "entry":     position.get("open_price"),
            "sl":        position.get("sl"),
            "tp":        position.get("tp"),
            "profit":    position.get("profit"),
            "open_time": position.get("open_time"),
        })

    dec_data = None
    if decision:
        dec_data = {
            # `allowed` is the legacy strategy-signal field.  It describes
            # whether the signal engine found an entry setup; it is not the
            # final permission to send an order.
            "allowed":    decision.allowed,
            "signal_allowed": decision.allowed,
            "direction":  decision.direction,
            "confidence": decision.confidence,
            "grade":      decision.grade,
            "regime":     decision.regime,
            "regime_label": decision.regime_label,
            "reasoning":  decision.reasoning[:8],
            "blocked_reasons": decision.blocked_reasons,
            "components": {
                "smc":        decision.components.smc_score,
                "trend":      decision.components.trend_score,
                "pa":         decision.components.pa_score,
                "wyckoff":    decision.components.wyckoff_score,
                "liquidity":  decision.components.liquidity_score,
                "volatility": decision.components.volatility_score,
                "total":      decision.components.total,
            },
            "trade_params": {
                "entry":  decision.trade_params.entry_price,
                "sl":     decision.trade_params.stop_loss,
                "tp":     decision.trade_params.take_profit,
                "lot":    decision.trade_params.lot_size,
                "rr":     decision.trade_params.risk_reward_ratio,
                "risk_usd": decision.trade_params.risk_amount,
                "risk_budget_usd": decision.trade_params.risk_budget,
                "risk_budget_exceeded": (
                    decision.trade_params.min_lot_risk_exceeded
                ),
            } if decision.trade_params else None,
            # Per-candle structured telemetry for the panel and post-trade
            # analysis. This is derived from the same DecisionResult and does
            # not introduce a second signal path.
            # A few panel/unit-test callers provide a deliberately minimal
            # signal-shaped object instead of a full DecisionResult. Keep
            # those legacy payloads serializable while real decisions receive
            # the complete structured telemetry.
            "telemetry": (
                describe_strategy(decision)
                if hasattr(decision, "pa") and hasattr(decision, "quality_filter")
                else None
            ),
        }

    # Derive connection/MT5 status from the ACTUAL connector state.
    # Using the robot loop-status string (RUNNING/PAUSED/WAITING) caused the
    # panel to show "connected" even when the mt5rest bridge was unreachable
    # (e.g. Render free-tier sleep, Guardian-paused robot, or mid-reconnect).
    # We import lazily so state_writer stays importable in unit tests that
    # don't initialise the connector.
    try:
        from live_trading.mt5.connector import is_connected as _mt5_is_connected
        _connected = _mt5_is_connected()
    except Exception:
        # Fallback: derive from status string (original behaviour)
        _connected = status.upper() in ("RUNNING", "WAITING", "SCANNING", "HOLDING")
    _conn_str = "connected" if _connected else "disconnected"

    # Convenience account dict (legacy "account" key kept for backward compat)
    _balance = account_info.get("balance", 0)
    _equity  = account_info.get("equity",  0)
    _account_dict = {
        "balance":     _balance,
        "equity":      _equity,
        # connector.get_account_info() does NOT return a "profit" field.
        # Compute it as equity − balance (= floating P&L on open positions).
        # Fall back to the API's "profit" field if it IS present (e.g. future
        # mt5rest versions that expose it directly).
        "profit":      account_info.get("profit", _equity - _balance),
        "margin":      account_info.get("margin", 0),
        "margin_free": account_info.get("freeMargin", account_info.get("free_margin", 0)),
        "currency":    account_info.get("currency", "USD"),
        "leverage":    account_info.get("leverage", 0),
        # Identity fields from mt5rest AccountSummary (if available).
        # These survive even when the panel SQLite DB is wiped (e.g. /tmp reset on Render).
        "broker":      account_info.get("broker", ""),
        "server":      account_info.get("server", ""),
        "login":       account_info.get("login", ""),
        "name":        account_info.get("name", ""),
    }

    # today_profit: sum of realised profits from trades closed today.
    # Floating P&L (equity-balance) is NOT today's profit; they measure different things.
    _today = __import__("datetime").date.today().isoformat()
    _today_profit = 0.0
    for _t in trade_history:
        if not isinstance(_t, dict):
            continue
        _p = _t.get("profit")
        if _p is None:
            continue  # open-trade entry logged at entry time, no realised profit yet
        _ts = str(_t.get("logged_at") or _t.get("bar_time") or "")
        if _ts and not _ts.startswith(_today):
            continue
        try:
            _today_profit += float(_p)
        except (TypeError, ValueError):
            pass

    state = {
        "status":           status,
        "version":          VERSION,
        # Fields read by the Telegram panel's robot_service.py
        "connection_status": _conn_str,
        "mt5_status":        _conn_str,
        "last_heartbeat":    _now_iso(),
        "last_update":       _now_iso(),
        "last_signal_time":  last_signal_time,
        "loop_count":        loop_count,
        # Uptime in seconds since engine start (tracked module-level)
        "uptime_seconds":    _get_uptime_seconds(),
        # Keep the legacy singular open_position field, but report every live
        # position in the count/list so hedging incidents cannot be hidden.
        "active_trades":     len(positions_data),
        "pending_orders":    0,
        # Legacy format (used by some panel views)
        "account": _account_dict,
        # account_info: format expected by telegram_panel/services/mt5_service.py
        "account_info": {
            "balance":          _account_dict["balance"],
            "equity":           _account_dict["equity"],
            "margin":           _account_dict["margin"],
            "free_margin":      _account_dict["margin_free"],
            "floating_profit":  _account_dict["profit"],
            "currency":         _account_dict["currency"],
            "leverage":         _account_dict["leverage"],
            # Identity fields written by connector.get_account_info()
            # so the panel can show broker/login even after a /tmp DB wipe.
            "broker":           _account_dict.get("broker", ""),
            "server":           _account_dict.get("server", ""),
            "login":            _account_dict.get("login", ""),
            "name":             _account_dict.get("name", ""),
            "connection_status": _conn_str,
        },
        # today_profit: day's realised P&L from trades closed today.
        # NOT the floating P&L (equity-balance) which is a common confusion.
        "today_profit":     round(_today_profit, 2),
        "open_position":    pos_data,
        "open_positions":   positions_data,
        "last_decision":    dec_data,
        # Kept separate from last_decision.allowed so panel consumers cannot
        # confuse a signal with a fully evaluated, live entry permission.
        "trade_permission": trade_permission or {
            "allowed": False,
            "stage": "NOT_EVALUATED",
            "reasons": ["No live entry gate evaluation yet"],
        },
        "recent_trades":    trade_history[-MAX_TRADE_HISTORY:],
        "trade_count":      len(trade_history),
    }
    if extra:
        state.update(extra)

    _safe_write(STATE_FILE, state)
    # ROOT-CAUSE FIX: sync recent_trades to snapshot key so the panel's
    # MT5Service (which reads goldscalper:snapshot) always has the latest
    # trade history without waiting for the next bar.
    if trade_history:
        try:
            from live_trading.redis_ipc import redis_update_snapshot_trades
            redis_update_snapshot_trades(trade_history[-20:])
        except Exception as _sync_exc:
            log.debug(f"Snapshot trade sync skipped: {_sync_exc}")


# ── Write robot_mt5_snapshot.json ─────────────────────────────────────────────

def write_mt5_snapshot(
    candle_time:     str,
    price:           float,
    regime:          str,
    adx:             float,
    atr:             float,
    smc_signal:      str,
    trend:           str,
    # FIX: Full account snapshot added so the Redis snapshot key contains real
    # account data.  The panel's MT5Service reads this key first; without these
    # fields it always got empty account_info → balance USD 0.00.
    account_info:    Optional[dict] = None,
    open_positions:  Optional[list] = None,
    recent_trades:   Optional[list] = None,
    today_profit:    float = 0.0,
    floating_profit: float = 0.0,
    drawdown:        Optional[dict] = None,
) -> None:
    snap = {
        "timestamp":       _now_iso(),
        "candle_time":     candle_time,
        "price":           price,
        "regime":          regime,
        "adx":             round(adx, 1),
        "atr":             round(atr, 4),
        "smc_signal":      smc_signal,
        "trend":           trend,
        # Account fields — populated on every bar so panel always has live data
        "account_info":    account_info or {},
        "open_positions":  open_positions if open_positions is not None else [],
        "recent_trades":   recent_trades if recent_trades is not None else [],
        "today_profit":    round(today_profit, 2),
        "floating_profit": round(floating_profit, 4),
        "drawdown":        drawdown or {},
    }
    _safe_write(SNAPSHOT_FILE, snap)


# ── Read robot_commands.json ──────────────────────────────────────────────────

def read_commands() -> dict:
    """Read commands written by Telegram panel.  Returns {} if none.

    Tries Redis first (required for cross-service IPC on Render where robot and
    panel run in separate containers with separate filesystems).  Falls back to
    the local command file for single-machine / local deployments.

    Supports two on-disk formats:
      • Dict format (legacy / future): ``{"pause": true, ...}`` — returned as-is.
      • List format (current panel):   ``[{"command": "PAUSE", "issued_at": "..."}]``
        — translated to engine dict keys via _PANEL_COMMAND_MAP; unknown commands
        and stale entries are silently discarded.

    Invalid or corrupted files always return {} without raising.
    """
    # Try Redis first — works across separate Render services
    try:
        from live_trading.redis_ipc import redis_read_commands, redis_available
        if redis_available():
            result = redis_read_commands()
            if result is not None:
                return result
    except Exception:
        pass
    try:
        if not os.path.exists(COMMANDS_FILE):
            return {}
        with open(COMMANDS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as _cmd_err:
        log.warning(f"commands file unreadable or corrupted — commands dropped: {_cmd_err}")
        return {}

    # ── Dict format — backward-compatible pass-through ────────────────────────
    if isinstance(data, dict):
        return data

    # ── List format — translate to engine dict keys ───────────────────────────
    if not isinstance(data, list):
        return {}

    now = datetime.now(timezone.utc)
    result: dict = {}

    for item in data:
        if not isinstance(item, dict):
            continue

        # Stale-command check — discard anything older than the age threshold
        issued_at_str = item.get("issued_at")
        if issued_at_str:
            try:
                issued_at = datetime.fromisoformat(issued_at_str)
                # Normalise naive timestamps to UTC (panel writes UTC without tz)
                if issued_at.tzinfo is None:
                    issued_at = issued_at.replace(tzinfo=timezone.utc)
                age_seconds = (now - issued_at).total_seconds()
                if age_seconds > _COMMAND_MAX_AGE_SECONDS:
                    log.warning(
                        f"Discarding stale command '{item.get('command')}' "
                        f"(age={age_seconds:.0f}s > {_COMMAND_MAX_AGE_SECONDS}s)"
                    )
                    continue
            except Exception:
                # Unparseable timestamp — process the command rather than silently drop it
                pass

        # Translate panel command name → engine dict key
        cmd_name   = str(item.get("command", "")).strip().upper()
        engine_key = _PANEL_COMMAND_MAP.get(cmd_name)
        if engine_key:
            result[engine_key] = True

    return result


def clear_command(key: str) -> None:
    # Clear from Redis too (cross-service IPC on Render)
    try:
        from live_trading.redis_ipc import redis_clear_command, redis_available
        if redis_available():
            redis_clear_command(key)
    except Exception:
        pass
    # Also clear from file (local / single-machine deployments)
    cmds = read_commands()
    if key in cmds:
        del cmds[key]
        _safe_write(COMMANDS_FILE, cmds)


def log_trade(trade_history: List[dict], entry: dict) -> None:
    entry["logged_at"] = _now_iso()
    trade_history.append(entry)
    if len(trade_history) > MAX_TRADE_HISTORY:
        trade_history[:] = trade_history[-MAX_TRADE_HISTORY:]

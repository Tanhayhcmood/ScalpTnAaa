"""
Render web-service wrapper — GoldScalperPro v4 Live Trading Engine.

Design: health server runs forever (process never exits); robot loop
restarts with exponential backoff on any failure.
Self-ping keepalive task pings /health every 14 min to prevent Render
free-tier sleep (no external UptimeRobot required).
"""
import asyncio
import hmac
import json
import os
import sys
import traceback
from datetime import datetime, timezone

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from aiohttp import web

PORT = int(os.environ.get("PORT", 10000))

# Module-level lock for thread-safe atomic writes to COMMANDS_FILE.
# Must be module-level — a local lock inside the handler provides zero
# mutual exclusion between concurrent requests (each call gets its own lock).
_commands_lock = asyncio.Lock()

# Allowlist for /command endpoint — validated once at import time.
_ALLOWED_COMMANDS = frozenset({
    "PAUSE", "RESUME", "EMERGENCY_STOP", "SAFE_SHUTDOWN",
    "CLOSE_ALL", "RESET_GUARDIAN", "START",
    "RESTART_ENGINE", "RESTART_MT5", "RESTART_TELEGRAM", "RECONNECT",
    "UPDATE_RISK", "UPDATE_STRATEGY",
})

_BACKOFF_BASE = 15
_BACKOFF_MAX  = 120
_backoff      = _BACKOFF_BASE
_robot_status = "STARTING"
_current_engine = None  # live GoldScalperLive instance, set in _run_robot_once()
# Only truly fatal states (config errors, unhandled crashes) return 503.
# DISCONNECTED is intentionally excluded: the robot is alive and actively
# trying to reconnect through MetaAPI. Returning 503 for DISCONNECTED
# caused Render's health monitor to restart the service on every temporary
# broker-session loss, creating an infinite restart loop that prevented the
# robot from ever completing its reconnect backoff.
# RETRY_IN_* is also excluded for the same reason.
# STOPPED is excluded: the supervisor will restart the engine automatically.
_UNHEALTHY_STATUSES = {"CONFIG_ERROR", "ERROR"}
_HEARTBEAT_MAX_AGE_SECONDS = 180

# Self-ping keepalive: ping /health every 8 minutes so Render free-tier
# services never spin down.  10 min < Render's 15-min inactivity threshold.
# The live loop keeps the MetaAPI broker session alive through
# its RPC connection; the health endpoint remains independent of broker
# availability so Render does not restart the service during reconnects.
_KEEPALIVE_INTERVAL_SECONDS = 360  # 6 minutes — safer margin: 3 pings before Render 15-min sleep


def _parse_heartbeat(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        heartbeat = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        return heartbeat
    except (TypeError, ValueError):
        return None


def _health_response(status: str) -> web.Response:
    normalized = status.upper()
    # RETRY_IN_* removed from unhealthy check: process is alive and retrying.
    unhealthy = normalized in _UNHEALTHY_STATUSES
    return web.Response(
        status=503 if unhealthy else 200,
        text=f"OK status={status}",
        content_type="text/plain",
    )


def _heartbeat_is_fresh(value: object, now: datetime | None = None) -> bool:
    """Return whether a state heartbeat is recent enough to prove liveness."""
    heartbeat = _parse_heartbeat(value)
    if heartbeat is None:
        return False
    current = now or datetime.now(timezone.utc)
    age = (current - heartbeat).total_seconds()
    return 0 <= age <= _HEARTBEAT_MAX_AGE_SECONDS


def _command_authorized(req: web.Request) -> tuple[bool, int, str]:
    """Validate the shared secret used by the panel's HTTP command fallback."""
    configured_token = os.environ.get("ROBOT_COMMAND_TOKEN", "")
    if not configured_token:
        return False, 503, "command interface is not configured"

    supplied_token = req.headers.get("X-Robot-Command-Token", "")
    if not supplied_token:
        return False, 401, "missing command authorization"
    if not hmac.compare_digest(supplied_token, configured_token):
        return False, 403, "invalid command authorization"
    return True, 200, ""


def _read_local_state() -> dict | None:
    """Read the local state file when cross-service Redis is unavailable."""
    try:
        from live_trading.config import STATE_FILE
        with open(STATE_FILE, "r", encoding="utf-8") as state_file:
            state = json.load(state_file)
        return state if isinstance(state, dict) else None
    except (OSError, TypeError, ValueError):
        return None


def _read_local_snapshot() -> dict | None:
    """Read the local broker snapshot file."""
    try:
        from live_trading.config import MT5_SNAPSHOT
        with open(MT5_SNAPSHOT, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, TypeError, ValueError):
        return None


async def _health(_req):
    # Prefer state written by the engine. Redis is the cross-service source on
    # Render; the local file is the fallback for a Redis outage.
    try:
        from live_trading.redis_ipc import redis_read_state, redis_available
        states = []
        if redis_available():
            state = redis_read_state()
            if state:
                states.append(state)

        local_state = _read_local_state()
        if local_state:
            states.append(local_state)

        fresh_states = [
            state for state in states
            if "status" in state and _heartbeat_is_fresh(state.get("last_heartbeat"))
        ]
        if fresh_states:
            freshest = max(
                fresh_states,
                key=lambda state: _parse_heartbeat(state["last_heartbeat"])
                or datetime.min.replace(tzinfo=timezone.utc),
            )
            return _health_response(str(freshest["status"]))

        # A state exists but none is fresh: this is a real liveness failure
        # even if the Redis client itself is unavailable. When no state exists
        # yet, retain the startup status so the process can finish booting.
        if states:
            return _health_response("DISCONNECTED")
    except Exception:
        pass
    return _health_response(_robot_status)


async def _status(req: web.Request):
    """JSON status endpoint — consumed by the Telegram panel's HTTP fallback.

    FIX: Removed authentication requirement from this read-only endpoint.
    The /command endpoint (write) still requires ROBOT_COMMAND_TOKEN.
    This allows the panel to always read fresh robot state via HTTP fallback
    without depending on the token being configured in the panel environment.

    Priority:
      1. Redis (cross-service, always fresh when available)
      2. Local state file (same-process fallback when Redis is down)
      3. In-memory _robot_status (last resort: only status string, no trade data)
    """
    now = datetime.now(timezone.utc)

    # 1. Try Redis first (cross-service IPC on Render)
    # FIX: if Redis data is stale (heartbeat too old), fall through to the local
    # state file instead of returning a frozen "disconnected" snapshot.
    # The local file is written every BAR_CHECK_INTERVAL (≈15 s) by the engine
    # loop, so it is usually much fresher than Redis when Redis writes silently
    # fail (e.g. free-tier connection drops between writes).
    _redis_stale_fallback: dict | None = None   # kept for last-resort use
    try:
        from live_trading.redis_ipc import redis_read_state, redis_available
        if redis_available():
            state = redis_read_state()
            if state:
                # Annotate with staleness so panel can display a warning
                hb = _parse_heartbeat(state.get("last_heartbeat"))
                if hb:
                    age = (now - hb).total_seconds()
                    state["_data_age_seconds"] = int(age)
                    state["_data_fresh"] = age <= _HEARTBEAT_MAX_AGE_SECONDS
                    if age > _HEARTBEAT_MAX_AGE_SECONDS:
                        # Stale — annotate but keep aside; try local file first
                        state["status"] = "disconnected"
                        state["connection_status"] = "disconnected"
                        state["mt5_status"] = "disconnected"
                        _redis_stale_fallback = state
                    else:
                        # Fresh Redis data — return immediately
                        return web.Response(
                            status=200,
                            text=json.dumps(state, default=str),
                            content_type="application/json",
                        )
                else:
                    state["_data_fresh"] = False
                    state["_data_age_seconds"] = -1
                    _redis_stale_fallback = state
    except Exception:
        pass

    # 2. Fall back to local state file (same container; written by the engine)
    local_state = _read_local_state()
    if local_state:
        hb = _parse_heartbeat(local_state.get("last_heartbeat"))
        if hb:
            age = (now - hb).total_seconds()
            local_state["_data_age_seconds"] = int(age)
            local_state["_data_fresh"] = age <= _HEARTBEAT_MAX_AGE_SECONDS
            if age > _HEARTBEAT_MAX_AGE_SECONDS:
                local_state["status"] = "disconnected"
                local_state["connection_status"] = "disconnected"
                local_state["mt5_status"] = "disconnected"
        else:
            local_state["_data_fresh"] = False
            local_state["_data_age_seconds"] = -1
        return web.Response(
            status=200,
            text=json.dumps(local_state, default=str),
            content_type="application/json",
        )

    # 3. Stale Redis as fallback (better than nothing — still has account/trade data)
    if _redis_stale_fallback is not None:
        return web.Response(
            status=200,
            text=json.dumps(_redis_stale_fallback, default=str),
            content_type="application/json",
        )

    # 4. Last resort: return in-memory supervisor status (no data = truly unknown)
    return web.Response(
        status=200,
        text=json.dumps({
            "status": _robot_status.lower(),
            "connection_status": "disconnected",
            "mt5_status": "disconnected",
            "last_heartbeat": None,
            "_data_fresh": False,
            "_data_age_seconds": -1,
            "diagnostics": {
                "mtapi_url_configured": bool(os.environ.get("MTAPI_URL")),
                "mt5_host": os.environ.get("MT5_HOST", ""),
                "mt5_port": os.environ.get("MT5_PORT", ""),
                "mt5_user_configured": bool(os.environ.get("MT5_USER")),
                "mt5_password_configured": bool(os.environ.get("MT5_PASSWORD")),
                "broker_connection": "mt5.mtapi.io",
                "diagnostic_revision": "direct-mtapi",
            },
        }),
        content_type="application/json",
    )


def _build_snapshot_from_state(state: dict, signal_snap: dict | None = None) -> dict:
    """Build a full /snapshot response from robot state, optionally merging signal data.

    The robot state (goldscalper:state Redis key / robot_state.json file) is the
    authoritative source for live account data.  The MT5 snapshot key only has
    per-bar signal metrics (price, regime, adx, atr).  This helper merges both
    so the Telegram panel always gets real account + signal data in one response.
    """
    account_info = state.get("account_info", {})
    guardian = state.get("guardian", {})
    pos = state.get("open_position")
    result: dict = {
        "account_info":     account_info,
        "connection_status": state.get("connection_status", "disconnected"),
        "today_profit":     float(state.get("today_profit", 0.0)),
        "floating_profit":  float(account_info.get("floating_profit", 0.0)),
        "open_positions":   [pos] if pos else [],
        "pending_orders":   [],
        "recent_trades":    state.get("recent_trades", []),
        "drawdown": {
            "current_percent": float(guardian.get("drawdown_pct", 0.0)),
            "max_percent":     float(
                guardian.get("max_drawdown_pct",
                             guardian.get("daily_loss_limit_pct", 0.0))
            ),
        },
    }
    # Merge per-bar signal fields from the snapshot key when available
    if signal_snap:
        for k in ("price", "regime", "adx", "atr", "smc_signal", "trend",
                  "candle_time", "timestamp"):
            if k in signal_snap:
                result[k] = signal_snap[k]
    return result


async def _snapshot(req: web.Request):
    """GET /snapshot — returns the live MetaAPI account snapshot.

    FIX: Previous implementation returned the Redis snapshot key first, but that
    key only contains per-bar signal data (price/regime/adx/atr) — no account
    balance.  The account data lives in the robot state key.  This endpoint now
    builds the response from robot state (authoritative) and merges signal data.

    Read-only endpoint: no authentication required.
    Used by the Telegram panel's broker-service HTTP fallback to get live
    account balance, positions, and trade data when Redis is unavailable.

    Priority:
      1. Redis robot state (cross-service IPC on Render) — has account_info
      2. Local robot state file — same data, local filesystem fallback
      3. Empty response (robot not yet started)
    """
    now = datetime.now(timezone.utc)

    # 1. Redis: prefer state (has account data) and merge signal snapshot
    try:
        from live_trading.redis_ipc import (
            redis_read_snapshot, redis_read_state, redis_available,
        )
        if redis_available():
            state = redis_read_state()
            if state and "account_info" in state:
                signal_snap = redis_read_snapshot()
                result = _build_snapshot_from_state(state, signal_snap)
                result["_fetched_at"] = now.isoformat()
                return web.Response(
                    status=200,
                    text=json.dumps(result, default=str),
                    content_type="application/json",
                )
    except Exception:
        pass

    # 2. Local robot state file + local signal snapshot file
    local_state = _read_local_state()
    if local_state and "account_info" in local_state:
        local_snap = _read_local_snapshot()
        result = _build_snapshot_from_state(local_state, local_snap)
        result["_fetched_at"] = now.isoformat()
        return web.Response(
            status=200,
            text=json.dumps(result, default=str),
            content_type="application/json",
        )

    # 3. Robot not yet connected — return empty account shell so panel doesn't crash
    return web.Response(
        status=200,
        text=json.dumps({"account_info": {}, "_fetched_at": now.isoformat()}),
        content_type="application/json",
    )


async def _command(req: web.Request) -> web.Response:
    """POST /command — receive a control command from the Telegram panel.

    Used as an HTTP fallback when Redis is unavailable.  The panel sends
    { "command": "PAUSE"|"RESUME"|"EMERGENCY_STOP"|…, "payload": {} }
    and this handler appends it to COMMANDS_FILE on the robot's local
    filesystem, where the live loop reads it on the next iteration.
    """
    authorized, status, message = _command_authorized(req)
    if not authorized:
        return web.Response(status=status, text=message)

    try:
        data = await req.json()
    except Exception:
        return web.Response(status=400, text="invalid JSON")

    command = str(data.get("command", "")).strip().upper()
    if not command:
        return web.Response(status=400, text="missing 'command' field")

    if command not in _ALLOWED_COMMANDS:
        return web.Response(status=400, text=f"unknown command: {command}")

    try:
        from live_trading.config import COMMANDS_FILE

        cmd_entry = {
            "command":   command,
            "payload":   data.get("payload") or {},
            "issued_at": datetime.now(timezone.utc).isoformat(),
        }

        # BUGFIX: this handler previously only appended to the local
        # COMMANDS_FILE. read_commands() in state_writer.py checks Redis
        # FIRST and returns immediately whenever Redis is configured and
        # reachable (which it always is on Render, robot + panel share one
        # Redis instance for cross-service IPC) -- so the local-file write
        # below was silently ignored and this endpoint never actually worked
        # while Redis was up. Mirror the command into Redis too, exactly like
        # the panel's own redis_send_command() does, so this HTTP path is a
        # real command channel and not just a dead fallback.
        try:
            from live_trading.redis_ipc import redis_send_command
            redis_send_command(command, data.get("payload") or {})
        except Exception as _redis_exc:
            from live_trading.logger import get_logger
            get_logger().warning(
                f"/command: redis mirror failed (falling back to file only): {_redis_exc}"
            )

        # Thread-safe atomic file append using the module-level lock.
        # _commands_lock is defined at module level so ALL concurrent requests
        # share the same lock — unlike a local lock which provides no exclusion.
        async with _commands_lock:
            # Ensure the commands file directory exists (e.g. /data/ on a
            # Render persistent disk that may not have been pre-created).
            _cmd_dir = os.path.dirname(COMMANDS_FILE)
            if _cmd_dir:
                os.makedirs(_cmd_dir, exist_ok=True)
            existing: list = []
            if os.path.exists(COMMANDS_FILE):
                try:
                    with open(COMMANDS_FILE, "r", encoding="utf-8") as _f:
                        existing = json.load(_f)
                    if not isinstance(existing, list):
                        existing = []
                except Exception:
                    existing = []
            existing.append(cmd_entry)
            tmp = COMMANDS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as _f:
                json.dump(existing, _f)
            os.replace(tmp, COMMANDS_FILE)

        return web.Response(
            status=200,
            text=json.dumps({"ok": True, "command": command}),
            content_type="application/json",
        )
    except Exception as exc:
        return web.Response(status=500, text=f"server error: {exc}")


async def _force_resume(request):
    """POST /force-resume — directly manipulate the live engine object
    in-process (bypasses Redis/file command queues entirely). Same auth as
    /command.

    Body: {"reset_baseline": bool}
      - false (default): only clears the halt flag (same as RESET_GUARDIAN).
        Note: if the underlying daily-loss/drawdown condition is still true,
        guardian.check() on the very next bar will immediately re-trigger the
        halt -- this is intentional, sticky-halt-until-cause-resolved design.
      - true: ALSO re-baselines the guardian (day_open_balance, equity_peak,
        session_open_balance) to the current account balance/equity via
        guardian.initialize(), i.e. treats "now" as the new reference point
        for today's loss tracking. This is an explicit, user-authorized
        override of risk-tracking state -- it does not change any configured
        risk parameter (daily_loss_limit_pct, max_drawdown_pct, etc.), but it
        does erase the currently-tracked loss for the rest of the day.
    """
    authorized, status, message = _command_authorized(request)
    if not authorized:
        return web.Response(status=status, text=message)
    if _current_engine is None:
        return web.Response(status=503, text="engine not running yet")
    try:
        try:
            body = await request.json()
        except Exception:
            body = {}
        reset_baseline = bool(body.get("reset_baseline", False))

        was_halted = _current_engine.guardian.is_halted
        rebaselined = False
        if reset_baseline:
            acc = _current_engine._last_acc_info or {}
            balance = float(acc.get("balance", 0.0))
            equity = float(acc.get("equity", balance))
            if balance > 0:
                _current_engine.guardian.initialize(balance, equity)
                rebaselined = True

        _current_engine.guardian.reset_halt()
        _current_engine.paused = False
        _current_engine._write_state("RUNNING")
        return web.Response(
            status=200,
            text=json.dumps({
                "ok": True,
                "was_halted": was_halted,
                "rebaselined": rebaselined,
                "paused": _current_engine.paused,
            }),
            content_type="application/json",
        )
    except Exception as exc:
        return web.Response(status=500, text=f"server error: {exc}")


async def _run_health_server():
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    app.router.add_get("/status", _status)
    app.router.add_get("/crash-log", _crash_log)
    app.router.add_get("/progress", _progress_log)
    app.router.add_post("/force-resume", _force_resume)
    app.router.add_get("/snapshot", _snapshot)
    app.router.add_post("/command", _command)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"[health] Listening on 0.0.0.0:{PORT}", flush=True)
    while True:
        await asyncio.sleep(60)


async def _keepalive():
    """External-ping /health every 6 min to prevent Render free-tier sleep.

    Render spins down free-tier web services after 15 minutes of inactivity.
    The inactivity timer is reset only by EXTERNAL HTTP requests routed through
    Render's edge — localhost / 127.0.0.1 requests bypass the edge entirely and
    do NOT reset the timer.

    We read RENDER_EXTERNAL_URL (set in render.yaml) for the external URL.
    Fallback: localhost (only effective when running locally, not on Render).
    """
    await asyncio.sleep(30)
    import aiohttp
    external_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not external_url:
        external_hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip()
        if external_hostname:
            external_url = f"https://{external_hostname}"

    own_url = f"{external_url}/health" if external_url else f"http://127.0.0.1:{PORT}/health"
    print(
        f"[keepalive] robot={own_url}  "
        f"interval={_KEEPALIVE_INTERVAL_SECONDS}s",
        flush=True,
    )
    # Reuse a single persistent session for the lifetime of the keepalive task.
    # Creating a new ClientSession on every iteration wastes connection-pool
    # resources and produces ResourceWarning noise in aiohttp ≥ 3.9.
    session = aiohttp.ClientSession()
    try:
        while True:
            # 1. Ping own /health to keep robot service alive
            try:
                async with session.get(
                    own_url, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    print(f"[keepalive] robot /health → {resp.status}", flush=True)
            except Exception as exc:
                print(f"[keepalive] robot /health failed: {exc}", flush=True)

            await asyncio.sleep(_KEEPALIVE_INTERVAL_SECONDS)
    finally:
        await session.close()



async def _crash_log(request):
    try:
        with open("/tmp/crash_trace.txt", "r", encoding="utf-8") as f:
            content = f.read()[-20000:]
    except Exception as e:
        content = f"(no crash log yet: {e})"
    return web.Response(text=content, content_type="text/plain")


async def _progress_log(request):
    try:
        with open("/tmp/progress.txt", "r", encoding="utf-8") as f:
            content = f.read()[-20000:]
    except Exception as e:
        content = f"(no progress log yet: {e})"
    return web.Response(text=content, content_type="text/plain")


async def _run_robot_once():
    global _robot_status
    from live_trading.logger import get_logger
    from live_trading.trading.live_loop import GoldScalperLive
    from live_trading.mt5.connector import disconnect as _mtapi_disconnect

    log = get_logger()

    from live_trading.config import MTAPI_URL, MT5_HOST, MT5_USER, MT5_PASSWORD
    if not MTAPI_URL or not MT5_HOST or not MT5_USER or not MT5_PASSWORD:
        _robot_status = "CONFIG_ERROR"
        raise RuntimeError(
            "MTAPI_URL, MT5_HOST, MT5_USER, and MT5_PASSWORD must be set — "
            "cannot start the MTAPI trading engine."
        )

    _robot_status = "STARTING"
    global _current_engine
    engine = GoldScalperLive()
    _current_engine = engine
    try:
        _robot_status = "RUNNING"
        # NOTE: engine.start() runs indefinitely by design -- including while
        # legitimately PAUSED (e.g. a RiskGuardian halt) -- so it must NOT be
        # wrapped in a bounded asyncio.wait_for(). A timeout here previously
        # forced a full engine restart (and MetaAPI disconnect/reconnect) every
        # ~240s even during normal, healthy PAUSED operation, which is what
        # was producing the repeated "Connection Lost / Connection Restored"
        # Telegram notifications. The actual root cause (frozen heartbeat
        # while paused) is fixed directly in live_loop.py's paused branch, so
        # no artificial bound is needed here.
        ok = await engine.start()
        # engine.start() returns False when the MTAPI connection or startup fails.
        # Without this check a False return is treated as a clean exit,
        # bypassing supervisor backoff and leaving _robot_status as RUNNING.
        if not ok:
            raise RuntimeError(
                "GoldScalperLive.start() returned False — "
                "MTAPI connection or engine startup failed."
            )
    except Exception:
        # DIAGNOSTIC: persist the full traceback to an always-writable path so
        # it survives even if the in-memory log buffer / Redis / state file
        # writes are themselves failing.  Exposed via GET /crash-log.
        try:
            with open("/tmp/crash_trace.txt", "a", encoding="utf-8") as _f:
                _f.write(f"\n\n=== crash at {datetime.now(timezone.utc).isoformat()} ===\n")
                traceback.print_exc(file=_f)
        except Exception:
            pass
        raise
    finally:
        _robot_status = "STOPPED"
        try:
            await _mtapi_disconnect()
        except Exception:
            pass


async def _robot_supervisor():
    global _backoff, _robot_status
    attempt = 0
    while True:
        attempt += 1
        print(f"[supervisor] Starting robot attempt #{attempt} …", flush=True)
        try:
            await _run_robot_once()
            print("[supervisor] Robot exited cleanly — scheduling restart.", flush=True)
            _backoff = _BACKOFF_BASE
        except Exception:
            wait = min(_backoff, _BACKOFF_MAX)
            _backoff = min(_backoff * 2, _BACKOFF_MAX)
            _robot_status = f"RETRY_IN_{wait}s"
            print(
                f"[supervisor] Robot error (attempt #{attempt}), retrying in {wait}s:",
                flush=True,
            )
            traceback.print_exc()
            await asyncio.sleep(wait)
        else:
            await asyncio.sleep(_BACKOFF_BASE)


async def _main():
    print(f"[server] Python {sys.version}  PORT={PORT}", flush=True)
    health = asyncio.create_task(_run_health_server())
    # Give the health server a moment to bind before loading the trading engine.
    await asyncio.sleep(1)
    keepalive  = asyncio.create_task(_keepalive())
    supervisor = asyncio.create_task(_robot_supervisor())
    # Keep strong references so CPython cannot GC background tasks.
    _background_tasks = {health, keepalive, supervisor}

    # asyncio.gather() with return_exceptions=True collects each task result
    # (or exception) instead of cancelling remaining tasks when one raises.
    # This prevents a transient keepalive failure from killing the health
    # server and supervisor, which would leave Render with no health endpoint
    # and no trading loop — the worst possible outcome for a silent failure.
    try:
        results = await asyncio.gather(health, keepalive, supervisor, return_exceptions=True)
        for task_name, res in zip(("health", "keepalive", "supervisor"), results):
            if isinstance(res, BaseException):
                print(f"[server] task '{task_name}' exited with error: {res}", flush=True)
                traceback.print_exception(type(res), res, res.__traceback__)
    except Exception:
        traceback.print_exc()
    # Never exit — keep the process alive even if all tasks somehow die.
    print("[server] all tasks finished — entering keep-alive loop", flush=True)
    while True:
        await asyncio.sleep(60)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("[server] Keyboard interrupt — shutting down.", flush=True)
    except Exception:
        traceback.print_exc()
        # Last-resort: keep process alive even on unexpected crash.
        import time
        while True:
            time.sleep(60)

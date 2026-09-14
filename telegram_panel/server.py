"""
Render web-service wrapper — Telegram Control Panel.
Runs the Telegram bot with a supervisor loop for auto-restart on crash.
Runs a /health HTTP endpoint on $PORT for Render health checks and keepalive.

Deployed as type: web so the service gets a public URL and health checks.
The keepalive task pings RENDER_EXTERNAL_URL/health every 14 min via the
external URL (not localhost) to reset Render's inactivity timer and prevent
free-tier sleep.
"""
import asyncio
import os
import sys
import traceback

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from aiohttp import web

# Render workers don't provision PORT, but we keep a health server for
# manual debugging (e.g. curl /health from Render shell).
PORT = int(os.environ.get("PORT", 8081))

# Supervisor backoff: start at 10 s, double on each crash, cap at 5 min.
# Reset to base on a clean (non-error) exit.
_BACKOFF_BASE = 10
_BACKOFF_MAX  = 300

# Self-ping keepalive: ping /health every 8 min to prevent Render free-tier
# web services from spinning down after 15 min of inactivity.
# Render deploys the panel as a web service (not a worker) so the 15-min
# sleep timer applies.  8 min gives a 7-min safety buffer (was 1 min at 14 min).
_KEEPALIVE_INTERVAL_SECONDS = 480  # 8 minutes — matches robot keepalive interval


async def _health(_req: web.Request) -> web.Response:
    return web.Response(text="OK", content_type="text/plain")


async def _run_health_server() -> None:
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"[health] Listening on 0.0.0.0:{PORT}", flush=True)
    while True:
        await asyncio.sleep(3600)


async def _keepalive() -> None:
    """Ping panel, robot, and mtapi every 8 min to keep the entire chain alive.

    ROOT-CAUSE FIX: The previous implementation only pinged the panel itself
    every 14 minutes — dangerously close to Render's 15-minute free-tier sleep
    threshold.  A single slow or failed ping left a 14-minute window where the
    service could go to sleep.

    Fix:
    - Interval reduced from 840 s (14 min) to 480 s (8 min), matching the
      robot's own keepalive interval and giving a 7-minute safety buffer.
    - Panel now also pings the robot (/health) and mtapi (/Ping) so that even
      if the robot's own internal keepalive is in a backoff/restart cycle, the
      panel acts as a second keepalive for the whole stack.

    Render's inactivity timer is reset only by EXTERNAL HTTP requests routed
    through Render's edge — localhost/127.0.0.1 requests bypass the edge and
    do NOT reset the timer.
    """
    await asyncio.sleep(30)  # wait for health server to start
    import aiohttp
    external_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    robot_url    = os.environ.get("ROBOT_BASE_URL",      "").rstrip("/")
    mtapi_url    = os.environ.get("MTAPI_URL",           "").rstrip("/")

    own_url = f"{external_url}/health" if external_url else f"http://127.0.0.1:{PORT}/health"
    print(
        f"[keepalive] panel={own_url}  robot={robot_url or '(not set)'}  "
        f"mtapi={mtapi_url or '(not set)'}  interval={_KEEPALIVE_INTERVAL_SECONDS}s",
        flush=True,
    )
    # Reuse a single persistent session — creating a new ClientSession on every
    # iteration wastes connection-pool resources and produces ResourceWarning
    # noise in aiohttp >= 3.9.
    session = aiohttp.ClientSession()
    try:
        while True:
            # 1. Keep panel itself alive
            try:
                async with session.get(own_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    print(f"[keepalive] panel /health → {resp.status}", flush=True)
            except Exception as exc:
                print(f"[keepalive] panel /health failed: {exc}", flush=True)

            # 2. Keep robot alive (belt-and-suspenders alongside robot's own keepalive)
            if robot_url:
                try:
                    async with session.get(
                        f"{robot_url}/health", timeout=aiohttp.ClientTimeout(total=20)
                    ) as resp:
                        print(f"[keepalive] robot /health → {resp.status}", flush=True)
                except Exception as exc:
                    print(f"[keepalive] robot /health failed: {exc}", flush=True)

            # 3. Keep mtapi (mt5rest Docker bridge) alive — Wine cold-start is
            #    60-90 s, so keeping it warm avoids the 90-s reconnect delay.
            if mtapi_url:
                try:
                    async with session.get(
                        f"{mtapi_url}/Ping", timeout=aiohttp.ClientTimeout(total=30)
                    ) as resp:
                        print(f"[keepalive] mtapi /Ping → {resp.status}", flush=True)
                except Exception as exc:
                    print(f"[keepalive] mtapi /Ping failed: {exc}", flush=True)

            await asyncio.sleep(_KEEPALIVE_INTERVAL_SECONDS)
    finally:
        await session.close()


async def _run_panel() -> None:
    from telegram_panel.main import TelegramPanel
    panel = TelegramPanel()
    await panel.run()


async def _main() -> None:
    # Health server and keepalive run in the background; task references are
    # stored so CPython's reference counter cannot garbage-collect them while
    # _main() is executing.  Unreferenced tasks can disappear silently in
    # Python ≥ 3.12 (PEP 667 enforcement became stricter).
    _health_task = asyncio.create_task(_run_health_server())
    _keepalive_task = asyncio.create_task(_keepalive())
    # Keep strong references alive for the lifetime of _main().
    _background_tasks = {_health_task, _keepalive_task}
    await asyncio.sleep(1)
    print("[server] Health server ready. Starting Telegram panel...", flush=True)

    # ── Startup validation — fail fast on missing required env vars ─────────────
    _missing = []
    if not os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
        _missing.append("TELEGRAM_BOT_TOKEN")
    if not os.environ.get("TELEGRAM_OWNER_ID", "").strip():
        _missing.append("TELEGRAM_OWNER_ID")
    if not os.environ.get("PANEL_ENCRYPTION_KEY", "").strip():
        _missing.append("PANEL_ENCRYPTION_KEY")
    if _missing:
        for _var in _missing:
            print(f"[startup] CRITICAL: required env var '{_var}' is not set.", flush=True)
        print(
            "[startup] Set the missing variables in Render dashboard → Environment, "
            "then redeploy. Panel cannot start without them.",
            flush=True,
        )
        sys.exit(78)   # EX_CONFIG — Render supervisor will NOT restart on non-zero exit

    # ── Auto-seed broker account from env vars once at startup ────────────────
    # Fixes "No accounts configured" after every Render restart that wipes
    # the ephemeral /tmp/panel.db SQLite database.
    try:
        from telegram_panel.utils.auto_seed import run as _auto_seed
        _auto_seed()
        print("[server] auto_seed completed", flush=True)
    except Exception as _seed_exc:
        print(f"[server] auto_seed warning (non-fatal): {_seed_exc}", flush=True)

    # ── Supervisor loop — restart panel on any crash ──────────────────────────
    # The panel runs indefinitely; this loop ensures it restarts automatically
    # with exponential backoff rather than letting the entire worker process exit.
    _backoff = _BACKOFF_BASE
    attempt = 0
    while True:
        attempt += 1
        print(f"[supervisor] Starting panel (attempt #{attempt})...", flush=True)
        try:
            await _run_panel()
            # Panel exited cleanly (rare — normally runs forever until signal).
            print(
                f"[supervisor] Panel exited cleanly — restarting in {_BACKOFF_BASE}s...",
                flush=True,
            )
            _backoff = _BACKOFF_BASE  # reset on clean exit
        except SystemExit as exc:
            code = exc.code if exc.code is not None else 1
            if code == 0:
                print(
                    f"[supervisor] Panel sys.exit(0) — restarting in {_BACKOFF_BASE}s...",
                    flush=True,
                )
                _backoff = _BACKOFF_BASE
            else:
                print(
                    f"[supervisor] Panel sys.exit({code}) — check config/env vars above. "
                    f"Restarting in {_backoff}s...",
                    flush=True,
                )
        except Exception as exc:
            print(f"[supervisor] Panel crashed ({type(exc).__name__}): {exc}", flush=True)
            traceback.print_exc()

        await asyncio.sleep(_backoff)
        _backoff = min(_backoff * 2, _BACKOFF_MAX)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("[server] Keyboard interrupt — shutting down.", flush=True)

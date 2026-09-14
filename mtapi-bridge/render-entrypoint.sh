#!/bin/bash
# Render PORT proxy — transparent wrapper for the mt5rest Docker service.
#
# ROOT CAUSE FIXED HERE:
#   Render's reverse-proxy routes all external HTTPS traffic (including the
#   /Ping health check) to the container on $PORT (typically 10000+ on the
#   free tier).  mt5rest always binds to port 80 and cannot be reconfigured
#   without patching the application.  Without this wrapper, Render's health
#   check never reaches mt5rest, the service is marked unhealthy and restarted
#   in a continuous loop, and the trading robot can never establish an MT5
#   connection.
#
# Fix strategy:
#   socat creates a lightweight TCP forwarder that listens on $PORT and
#   forwards every connection to localhost:80 where mt5rest is running.
#   This script starts the proxy in the background, then runs the original
#   container command in a supervised restart loop so that:
#     - mt5rest starts exactly as before (no application changes)
#     - Render's health check reaches /Ping through the proxy
#     - The trading robot's MTAPI_URL works through Render's reverse proxy
#     - Wine/dotnet transient startup failures are recovered automatically
#       without the container exiting (which Render would count as earlyExit
#       and mark the deploy as failed)
#
# Wine startup behaviour on Render free tier:
#   The mt5rest image runs a Windows .NET application under Wine.  Wine can
#   fail to initialise on the first attempt due to resource contention on
#   shared infrastructure.  The original "exec" approach caused the container
#   to exit on every Wine crash, triggering a Render deploy failure and a
#   restart loop visible in the dashboard.  The supervised loop below keeps
#   the container (PID 1 = this script) alive through transient Wine crashes,
#   restarting the child process automatically until Wine initialises
#   successfully and /Ping begins responding.
#
# FIX — earlyExit on Wine code=0 startup failure:
#   Wine/dotnet sometimes exits with code 0 (not just non-zero) when it fails
#   to initialise during container startup on Render's free tier.  The previous
#   logic treated any code=0 exit as a clean intentional shutdown and broke out
#   of the restart loop — causing the container (PID 1) to exit and Render to
#   record earlyExit on every new deployment.
#   Fix: only treat code=0 as intentional if the process ran for > MIN_UPTIME_S
#   seconds.  A code=0 exit within the first MIN_UPTIME_S seconds is treated as
#   a Wine startup crash and triggers the same 5s restart as a non-zero exit.
#   MIN_UPTIME_S=300 (5 min) covers all real shutdown scenarios while catching
#   any Wine initialisation failures that exit cleanly.
#
# SELF-KEEPALIVE (FIX — critical):
#   Render free tier sleeps a service after 15 minutes of no EXTERNAL traffic.
#   The trading robot pings /Ping every ~8 minutes, but if the robot itself is
#   restarting or in a backoff cycle, no pings arrive at the mtapi and the
#   Docker service sleeps.  Once asleep, the next robot reconnect attempt waits
#   60-90 s for Wine to cold-start, causing a "Connection Lost" spike in the
#   panel on every robot restart — a continuous reconnect loop.
#
#   Fix: a background loop inside this container pings its OWN /Ping endpoint
#   through Render's external URL every 8 minutes, independently of the robot.
#   Even if the robot is down for an hour the mtapi stays warm.
#   RENDER_EXTERNAL_URL must be set in render.yaml (already done).
#
# Local / Docker Compose usage:
#   When PORT is not set or equals 80, the proxy and external keepalive are
#   skipped entirely so local development and non-Render deployments are
#   unaffected.

_PORT="${PORT:-80}"
_SELF_KEEPALIVE_INTERVAL=480  # 8 minutes — well under Render's 15-min threshold
# Minimum seconds a child must run for code=0 to be treated as intentional shutdown.
# Wine startup failures can exit with code 0 within seconds; real shutdowns always
# come after the service has been running (i.e. > 5 minutes).
_MIN_UPTIME_S=300
# Avoid a tight Wine/.NET restart loop when the host briefly exhausts a shared
# kernel resource such as inotify instances. Bounded backoff gives the child
# time to release resources before the next startup attempt.
_CRASH_BACKOFF_MIN=5
_CRASH_BACKOFF_MAX=120
_CRASH_BACKOFF=$_CRASH_BACKOFF_MIN

if [ "$_PORT" != "80" ]; then
    echo "[render-entrypoint] Render PORT=${_PORT} detected — starting socat proxy: ${_PORT} → 80"
    # fork: handle each connection in a separate child process (concurrent requests)
    # reuseaddr: allow fast restart without "address already in use" errors
    socat TCP-LISTEN:${_PORT},fork,reuseaddr TCP:127.0.0.1:80 &
    _SOCAT_PID=$!
    echo "[render-entrypoint] socat proxy started (pid=${_SOCAT_PID})"

    # ── Self-keepalive loop ───────────────────────────────────────────────────
    # Pings our OWN external /Ping URL every 8 minutes so this Docker service
    # never goes to sleep even when the trading robot is down/restarting.
    # Uses RENDER_EXTERNAL_URL which is set in render.yaml for this service.
    # Falls back to localhost (which does NOT prevent Render sleep but is harmless).
    _EXTERNAL_URL="${RENDER_EXTERNAL_URL:-}"
    if [ -n "$_EXTERNAL_URL" ]; then
        _PING_TARGET="${_EXTERNAL_URL%/}/Ping"
        echo "[render-entrypoint] Self-keepalive enabled: pinging ${_PING_TARGET} every ${_SELF_KEEPALIVE_INTERVAL}s"
    else
        _PING_TARGET="http://127.0.0.1:${_PORT}/Ping"
        echo "[render-entrypoint] RENDER_EXTERNAL_URL not set — self-keepalive via localhost (won't prevent Render sleep)"
    fi

    (
        # Wait for mt5rest to fully boot before starting pings (Wine startup ~90s)
        sleep 120
        while true; do
            _STATUS=$(wget -qO- --timeout=20 --tries=1 "${_PING_TARGET}" 2>/dev/null && echo "ok" || echo "fail")
            echo "[self-keepalive] /Ping → ${_STATUS}" >&2
            sleep ${_SELF_KEEPALIVE_INTERVAL}
        done
    ) &
    _KEEPALIVE_PID=$!
    echo "[render-entrypoint] Self-keepalive loop started (pid=${_KEEPALIVE_PID})"
else
    echo "[render-entrypoint] PORT=80 — running without proxy or keepalive (local / Docker Compose mode)"
fi

# ── Supervised restart loop ───────────────────────────────────────────────────
# Keep PID 1 (this script) alive so Render never sees an earlyExit.
# On SIGTERM/SIGINT (graceful shutdown from Render), forward the signal to the
# running child and exit cleanly.

_CHILD_PID=""

_shutdown() {
    echo "[render-entrypoint] Shutdown signal received — stopping mt5rest (pid=${_CHILD_PID})"
    if [ -n "$_CHILD_PID" ]; then
        kill "$_CHILD_PID" 2>/dev/null || true
        wait "$_CHILD_PID" 2>/dev/null || true
    fi
    # Also stop the keepalive and socat background processes gracefully
    [ -n "$_KEEPALIVE_PID" ] && kill "$_KEEPALIVE_PID" 2>/dev/null || true
    [ -n "$_SOCAT_PID" ]     && kill "$_SOCAT_PID"     2>/dev/null || true
    exit 0
}

trap _shutdown SIGTERM SIGINT

echo "[render-entrypoint] Starting mt5rest with supervised restart loop (min_uptime=${_MIN_UPTIME_S}s for clean exit)..."
while true; do
    _ITER_START=$(date +%s)

    # Run the original container command as a child (not exec) so this script
    # remains PID 1 and can catch signals and restart on failure.
    "$@" &
    _CHILD_PID=$!
    wait "$_CHILD_PID"
    _EXIT=$?
    _CHILD_PID=""

    _ITER_END=$(date +%s)
    _UPTIME=$((_ITER_END - _ITER_START))

    # After a minute the process has completed its cold start. If it later
    # exits, restart with the short delay again; repeated early crashes use
    # exponential backoff.
    if [ "$_UPTIME" -ge 60 ]; then
        _CRASH_BACKOFF=$_CRASH_BACKOFF_MIN
    fi

    if [ "$_EXIT" -eq 0 ] && [ "$_UPTIME" -ge "$_MIN_UPTIME_S" ]; then
        # Only treat code=0 as a clean intentional shutdown when the process
        # ran for at least MIN_UPTIME_S seconds.  This distinguishes a real
        # graceful shutdown from Wine initialisation failures that exit with
        # code 0 within seconds of container startup.
        echo "[render-entrypoint] mt5rest exited cleanly (code=0, uptime=${_UPTIME}s ≥ ${_MIN_UPTIME_S}s) — shutting down"
        break
    fi

    if [ "$_EXIT" -eq 0 ]; then
        echo "[render-entrypoint] mt5rest exited code=0 but uptime=${_UPTIME}s < ${_MIN_UPTIME_S}s — treating as Wine startup crash, restarting in ${_CRASH_BACKOFF}s..."
    else
        echo "[render-entrypoint] mt5rest exited (code=${_EXIT}, uptime=${_UPTIME}s) — Wine/dotnet crash detected, restarting in ${_CRASH_BACKOFF}s..."
    fi
    sleep "$_CRASH_BACKOFF"
    if [ "$_CRASH_BACKOFF" -lt "$_CRASH_BACKOFF_MAX" ]; then
        _NEXT_BACKOFF=$((_CRASH_BACKOFF * 2))
        if [ "$_NEXT_BACKOFF" -gt "$_CRASH_BACKOFF_MAX" ]; then
            _NEXT_BACKOFF=$_CRASH_BACKOFF_MAX
        fi
        _CRASH_BACKOFF=$_NEXT_BACKOFF
    fi
done

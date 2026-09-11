"""
mt5rest HTTP Connector – GoldScalperPro v4

Direct MT5 connection via the mt5rest Docker bridge (no MetaAPI cloud).
Runs fully self-hosted on Render.

Required env vars:
    MTAPI_URL     – URL of the mt5rest Docker service
                    e.g. https://ger-mtapi.onrender.com
    MT5_HOST      – broker server name  (e.g. AMarkets-Demo)
    MT5_USER      – MT5 account login number
    MT5_PASSWORD  – MT5 account password

mt5rest endpoints used:
    GET  /ConnectEx        – authenticate with broker, returns UUID conn id
    GET  /Disconnect       – close connection
    GET  /ConnectionStatus – check live connection
    GET  /AccountSummary   – balance, equity, margin
    GET  /OpenedOrders     – open positions
    GET  /HistoryPositions – completed positions by ticket
    GET  /PriceHistoryV2   – OHLCV candles (ISO datetime range)
    GET  /GetQuote         – current bid/ask price
    GET  /Ping             – liveness probe
"""

import asyncio
import math
import time as _time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import aiohttp

from live_trading.config import (
    MTAPI_URL, MT5_HOST, MT5_PORT,
    MT5_USER, MT5_PASSWORD,
    SYNC_TIMEOUT,
)
from live_trading.signals.gold_engine import OHLCV
from live_trading.logger import get_logger

log = get_logger()

# ── Module-level state ────────────────────────────────────────────────────────
_session:    Optional[aiohttp.ClientSession] = None
_connected:  bool = False
_base_url:   str  = ""
_conn_id:    str  = ""   # UUID returned by ConnectEx; passed to every call
_last_connect_time: float = 0.0   # monotonic timestamp of last successful connect()

# After a fresh ConnectEx the mt5rest bridge may take a few seconds to report
# isConnected=true on ConnectionStatus.  During this window ensure_connected()
# would wrongly declare DISCONNECTED and trigger an immediate reconnect loop.
# The grace period suppresses that false failure.
_CONNECT_GRACE_PERIOD: float = 150.0  # seconds — ROOT-CAUSE FIX: Wine cold-start on Render free tier
# takes 60-90 s, plus 30-60 s for the service to wake from sleep, totalling up
# to 150 s before isConnected=true is reliable.  90 s was too short: the grace
# period expired mid-startup, causing ensure_connected() to detect a false
# DISCONNECTED and trigger an immediate reconnect that aborted the in-progress
# ConnectEx — producing the continuous 'Connection Lost' loop in the panel.

# Prevents concurrent reconnect attempts when multiple coroutines detect a
# stale connection at the same time (e.g. fetch_candles + ensure_connected
# racing on the same event loop).  The first coroutine acquires the lock and
# reconnects; the rest wait and benefit from the result.
_reconnect_lock: asyncio.Lock | None = None

# Background watchdog task — proactively checks connection every 60 s and
# reconnects before the trading loop hits a failure.  Stored here so it can
# be cancelled cleanly on shutdown.
_watchdog_task: asyncio.Task | None = None

# MT5 broker-session keepalive: pings /ConnectionStatus with the actual conn_id
# every _MT5_KEEPALIVE_INTERVAL_S seconds so the broker socket stays open.
# Completely separate from the HTTP-bridge /Ping in server.py.
_MT5_KEEPALIVE_TASK: asyncio.Task | None = None
_MT5_KEEPALIVE_INTERVAL_S:    float = 180.0    # 3 min  — keep broker session alive
_MT5_SESSION_REFRESH_AGE_S:   float = 14400.0  # 4 hours — proactively refresh conn_id


def _connect_params(user: str, password: str, host: str) -> dict[str, object]:
    """Build query parameters accepted by mt5rest's ConnectEx endpoint.

    aiohttp encodes query parameters through ``str()`` for most values, but
    mt5rest validates the values before routing and rejects Python booleans
    with HTTP 500. Keep the flags as explicit lowercase query-string values
    so the request is valid for both the current bridge and older versions.
    """
    return {
        "user": user,
        "password": password,
        "server": host,
        "connectTimeoutSeconds": 60,
        # The panel's trade history is backed by the same MT5 session. Ask
        # mt5rest to download it during ConnectEx so closed trades are
        # available immediately after reconnects.
        "downloadOrderHistory": "true",
        "reconnectOnSymbolUpdate": "true",
    }


def _get_reconnect_lock() -> asyncio.Lock:
    """Lazily create the reconnect lock on the running event loop."""
    global _reconnect_lock
    if _reconnect_lock is None:
        _reconnect_lock = asyncio.Lock()
    return _reconnect_lock


# ── Timeframe map  (label → mt5rest integer minutes) ─────────────────────────
_TF_MAP = {
    "1m":  1,   "5m":  5,   "10m": 10,  "15m": 15,  "20m": 20,  "30m": 30,
    "1h":  60,  "4h":  240, "1d":  1440,
    "M1":  1,   "M5":  5,   "M10": 10,  "M15": 15,  "M20": 20,  "M30": 30,
    "H1":  60,  "H4":  240, "D1":  1440,
}


def _parse_candle_time(value: object) -> Optional[datetime]:
    """Parse a mt5rest candle timestamp into an aware UTC datetime."""
    raw = str(value).strip()
    if not raw:
        return None

    try:
        numeric = float(raw)
        if math.isfinite(numeric):
            if abs(numeric) > 100_000_000_000:
                numeric /= 1000.0
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        pass

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _h1_request_minutes(count: int) -> int:
    """Return a calendar lookback that can contain ``count`` traded H1 bars."""
    # PriceHistoryV2 accepts a date range, not a bar count. A one-to-one
    # count*1h range cannot contain 300 H1 bars when weekends or holidays fall
    # inside it, so include a trading-week and holiday buffer.
    active_bars = max(count + 10, math.ceil(count * 7 / 5))
    return active_bars * 60 + (3 * 24 * 60)


def _completed_candles(
    candles: List[Tuple[datetime, OHLCV]],
    *,
    timeframe_minutes: int,
    now: datetime,
) -> List[OHLCV]:
    """Keep chronologically ordered, completed candles.

    The mt5rest ``time`` field is the candle open time. For H1, checking the
    calculated close time avoids dropping a valid last candle just because the
    bridge returned only closed history, while also preventing an in-progress
    candle from entering the EMA calculation.
    """
    ordered: List[Tuple[datetime, OHLCV]] = []
    seen_times: set[datetime] = set()
    for candle_time, candle in sorted(candles, key=lambda item: item[0]):
        if candle_time in seen_times:
            continue
        seen_times.add(candle_time)
        ordered.append((candle_time, candle))

    if timeframe_minutes == 60:
        close_cutoff = now - timedelta(seconds=2)
        return [
            candle
            for candle_time, candle in ordered
            if candle_time + timedelta(hours=1) <= close_cutoff
        ]

    # Preserve the existing behavior for entry timeframes. This change is
    # intentionally scoped to Option 1's H1 history path.
    return [candle for _, candle in ordered[:-1]]


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
        )
    return _session


# ── Connection lifecycle ──────────────────────────────────────────────────────

async def connect(*args, **kwargs) -> bool:
    """
    Connect to MT5 via the mt5rest HTTP bridge using GET /ConnectEx.
    Returns the connection UUID which is stored in _conn_id.
    """
    global _connected, _base_url, _conn_id, _last_connect_time

    base     = MTAPI_URL.rstrip("/") if MTAPI_URL else ""
    host     = MT5_HOST
    user     = MT5_USER.strip() if MT5_USER else ""
    password = MT5_PASSWORD.strip() if MT5_PASSWORD else ""

    if not base:
        log.error(
            "MTAPI_URL is not set. "
            "Deploy the mt5rest Docker service and set MTAPI_URL to its URL."
        )
        return False
    if not user or not password:
        log.error("MT5_USER and MT5_PASSWORD must be set.")
        return False

    _base_url = base
    sess = _get_session()

    try:
        log.info(f"Connecting to MT5 via mt5rest at {base} ...")
        async with sess.get(
            f"{base}/ConnectEx",
            params=_connect_params(user, password, host),
            timeout=aiohttp.ClientTimeout(total=SYNC_TIMEOUT),
        ) as resp:
            raw = await resp.text()
            log.debug(f"ConnectEx response ({resp.status}): [response received]")

            if resp.status != 200:
                log.error(f"ConnectEx failed (status={resp.status}): {raw[:300]}")
                _connected = False
                return False

            # Response is a plain UUID string (may be quoted JSON string or raw)
            conn_id = raw.strip().strip('"')
            if not conn_id or len(conn_id) < 10:
                log.error(f"ConnectEx returned unexpected value: {raw[:200]}")
                _connected = False
                return False

            _conn_id   = conn_id
            _connected = True
            _last_connect_time = _time.monotonic()
            log.info(f"MT5 connected – broker: {host}  user: {user}  conn_id: {conn_id}")
            return True

    except Exception as exc:
        log.error(f"MT5 connect error: {exc}")
        _connected = False
        return False


async def disconnect() -> None:
    """Close the bridge connection and always release the HTTP session."""
    global _connected, _session, _conn_id, _base_url

    conn_id = _conn_id
    base_url = _base_url
    session = _session
    _connected = False
    _conn_id = ""
    _base_url = ""

    if conn_id and base_url and session and not session.closed:
        try:
            async with session.get(
                f"{base_url}/Disconnect",
                params={"id": conn_id},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status >= 400:
                    log.warning(
                        f"MT5 disconnect request returned HTTP {response.status}"
                    )
        except Exception as exc:
            log.warning(f"MT5 disconnect request failed: {exc}")

    if session and not session.closed:
        try:
            await session.close()
        except Exception as exc:
            log.warning(f"MT5 HTTP session close failed: {exc}")
    _session = None



async def keepalive_mtapi() -> bool:
    """
    Ping the mt5rest bridge to prevent Render free-tier sleep (every ~10 min).
    Returns True if the bridge responded, False otherwise.
    """
    base = MTAPI_URL.rstrip("/") if MTAPI_URL else ""
    if not base:
        return False
    try:
        sess = _get_session()
        async with sess.get(
            f"{base}/Ping",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            ok = resp.status == 200
            if ok:
                log.debug("MTAPI keepalive ping OK")
            else:
                log.warning(f"MTAPI keepalive ping returned {resp.status}")
            return ok
    except Exception as exc:
        log.warning(f"MTAPI keepalive ping failed: {exc}")
        return False


async def connect_with_retry(max_attempts: int = 5, retry_delay: float = 60.0) -> bool:
    """
    Connect to MT5, retrying up to max_attempts times.
    On Render free tier the mt5rest bridge may be sleeping and need 60-90s to cold-start.
    """
    for attempt in range(1, max_attempts + 1):
        log.info(f"MT5 connect attempt {attempt}/{max_attempts} ...")
        ok = await connect()
        if ok:
            return True
        if attempt < max_attempts:
            log.warning(
                f"MT5 connect failed (attempt {attempt}). "
                f"Waiting {retry_delay}s for mt5rest bridge to wake up ..."
            )
            await asyncio.sleep(retry_delay)
    log.error(f"MT5 connect failed after {max_attempts} attempts.")
    return False
def _invalidate_connection() -> None:
    """Mark the current conn_id as stale so the next API call triggers a fresh ConnectEx.

    Called whenever an API endpoint returns an error-shaped response that indicates
    the conn_id is no longer recognised by the mt5rest bridge (e.g. after a bridge
    restart or broker-side session timeout on Render free tier).
    """
    global _connected, _conn_id
    log.warning("MT5 conn_id is stale — invalidating connection (will reconnect on retry)")
    _connected = False
    _conn_id   = ""


async def ensure_connected(*args, **kwargs) -> bool:
    """Check live connection status; reconnect if not connected.

    Uses _reconnect_lock to prevent concurrent reconnect storms when multiple
    coroutines detect a stale connection simultaneously (e.g. fetch_candles and
    the watchdog racing on the same event loop tick).  The first coroutine
    acquires the lock and reconnects; the rest wait and benefit from the result.

    Extra positional/keyword args are accepted for backward compatibility
    with callers that pass MetaAPI-style token/account/timeout arguments.
    """
    global _connected

    # Grace period: right after a fresh ConnectEx the mt5rest bridge takes a
    # few seconds to report isConnected=true on ConnectionStatus.  Trusting the
    # module flag during this window prevents a false DISCONNECTED that would
    # otherwise trigger an immediate reconnect loop on every startup.
    # 90 s because Wine on Render free tier can take 60-90 s to fully init.
    if (_conn_id and _connected
            and _time.monotonic() - _last_connect_time < _CONNECT_GRACE_PERIOD):
        return True

    if _conn_id and _base_url:
        try:
            sess = _get_session()
            async with sess.get(
                f"{_base_url}/ConnectionStatus",
                params={"id": _conn_id},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json(content_type=None)
                if isinstance(data, dict) and data.get("isConnected"):
                    _connected = True
                    return True
        except Exception:
            pass

    _connected = False
    lock = _get_reconnect_lock()
    if lock.locked():
        # Another coroutine is already reconnecting — wait for it and return
        # its result rather than firing a second parallel ConnectEx.
        log.debug("MT5 reconnect already in progress — waiting for result …")
        async with lock:
            return _connected  # populated by the coroutine that held the lock
    log.info("MT5 not connected — reconnecting …")
    async with lock:
        # Re-check inside the lock: another waiter may have already reconnected
        if _connected and _time.monotonic() - _last_connect_time < _CONNECT_GRACE_PERIOD:
            return True
        return await connect_with_retry(max_attempts=3, retry_delay=30.0)


async def start_connection_watchdog(interval_seconds: float = 30.0) -> None:
    """Proactive background task: checks MT5 connection health every *interval_seconds*
    and reconnects before the trading loop hits a failure.

    Called once from GoldScalperLive.start() and runs until the engine stops.
    Stores the asyncio.Task in _watchdog_task so it can be cancelled on shutdown.

    Why this matters:
      Without a proactive watchdog the connector only reconnects *after* a
      trading-loop request fails — by which time the bar has already started
      and the opportunity may be lost.  Checking every 60 s means the worst-case
      reconnect latency is ~60 s, not one full 5-minute bar.
    """
    global _watchdog_task
    log.info(f"[watchdog] MT5 connection watchdog started (interval={interval_seconds}s) — reconnect within {interval_seconds*2}s of any sustained drop")
    _watchdog_consec_failures = 0   # consecutive ConnectionStatus=false counter
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            # Skip check during grace period — connection is known-good
            if _connected and _time.monotonic() - _last_connect_time < _CONNECT_GRACE_PERIOD:
                continue
            if not _connected or not _conn_id:
                log.warning("[watchdog] MT5 disconnected — proactive reconnect …")
                ok = await ensure_connected()
                if ok:
                    log.info("[watchdog] ✅ Proactive reconnect succeeded")
                else:
                    log.warning("[watchdog] ⚠️  Proactive reconnect failed — will retry next interval")
            else:
                # Verify the connection is still truly alive.
                # Require 2 consecutive failures before invalidating to avoid
                # triggering a full reconnect cycle on a single transient blip.
                try:
                    sess = _get_session()
                    async with sess.get(
                        f"{_base_url}/ConnectionStatus",
                        params={"id": _conn_id},
                        timeout=aiohttp.ClientTimeout(total=8),
                    ) as resp:
                        data = await resp.json(content_type=None)
                        if isinstance(data, dict) and data.get("isConnected"):
                            _watchdog_consec_failures = 0  # reset on success
                        else:
                            _watchdog_consec_failures += 1
                            log.warning(
                                f"[watchdog] ConnectionStatus=false "
                                f"(failure {_watchdog_consec_failures}/2) — "
                                + ("reconnecting …" if _watchdog_consec_failures >= 2
                                   else "waiting for confirmation …")
                            )
                            if _watchdog_consec_failures >= 2:
                                _watchdog_consec_failures = 0
                                _invalidate_connection()
                                try:
                                    await ensure_connected()
                                except Exception as _rc_err:
                                    log.warning(f"[watchdog] reconnect attempt failed: {_rc_err}")
                except Exception as exc:
                    _watchdog_consec_failures += 1
                    log.warning(
                        f"[watchdog] ConnectionStatus check failed (failure "
                        f"{_watchdog_consec_failures}/2): {exc}"
                        + (" — reconnecting …" if _watchdog_consec_failures >= 2 else "")
                    )
                    if _watchdog_consec_failures >= 2:
                        _watchdog_consec_failures = 0
                        _invalidate_connection()
                        try:
                            await ensure_connected()
                        except Exception as _rc_err:
                            log.warning(f"[watchdog] reconnect attempt failed: {_rc_err}")
    except asyncio.CancelledError:
        log.info("[watchdog] MT5 connection watchdog stopped")
        raise


async def start_mt5_session_keepalive(
    interval_s: float = _MT5_KEEPALIVE_INTERVAL_S,
    refresh_age_s: float = _MT5_SESSION_REFRESH_AGE_S,
) -> None:
    """
    MT5 Broker-Session Keepalive — GoldScalperPro v4.

    The MTAPI /Ping (called from server.py) keeps the HTTP bridge process
    alive on Render free-tier, but does NOT send any traffic to the MT5
    broker socket.  After extended inactivity the broker can terminate the
    session, invalidating the conn_id and triggering a "Connection Lost"
    alert even though the HTTP bridge itself is healthy.

    This task fixes that by:
      1. Every `interval_s` seconds: calling /ConnectionStatus with the
         real conn_id, which flushes traffic through the MT5 broker socket
         and resets any broker-side inactivity timer.
      2. Proactive conn_id refresh: if the session is older than
         `refresh_age_s` (default 4 h), calls ConnectEx to get a fresh
         UUID before the broker can expire it.
      3. If the session is already dead: calls ensure_connected() to restore
         it silently, before the trading loop's next bar attempt would fail.

    Runs as a background asyncio.Task — never blocks the trading loop.
    Fails silently: any exception is logged and the loop continues.
    """
    global _MT5_KEEPALIVE_TASK
    log.info(
        f"[mt5_keepalive] MT5 broker-session keepalive started "
        f"(ping every {interval_s:.0f}s, refresh after {refresh_age_s/3600:.1f}h)"
    )
    try:
        while True:
            await asyncio.sleep(interval_s)

            if not _connected or not _conn_id or not _base_url:
                # Not yet connected — let ensure_connected handle it
                if _conn_id or _base_url:
                    log.info("[mt5_keepalive] Not connected — triggering ensure_connected")
                    await ensure_connected()
                continue

            # Proactive session refresh before broker expires the conn_id
            session_age = _time.monotonic() - _last_connect_time
            if session_age > refresh_age_s:
                log.info(
                    f"[mt5_keepalive] Session age {session_age/3600:.1f}h exceeds "
                    f"{refresh_age_s/3600:.1f}h — proactively refreshing conn_id …"
                )
                try:
                    ok = await connect()
                    if ok:
                        log.info("[mt5_keepalive] ✅ Proactive conn_id refresh succeeded")
                    else:
                        log.warning("[mt5_keepalive] ⚠️  Proactive conn_id refresh failed")
                except Exception as exc:
                    log.warning(f"[mt5_keepalive] Proactive refresh error: {exc}")
                continue

            # Normal keepalive: ping ConnectionStatus to keep broker socket warm.
            # This task's ONLY job is keeping the MT5 broker socket alive.
            # Reconnection is handled exclusively by the watchdog — having two
            # tasks both call _invalidate_connection() creates a race condition
            # that crashes the trading loop.  We log warnings here but never
            # invalidate or reconnect from this task.
            try:
                sess = _get_session()
                async with sess.get(
                    f"{_base_url}/ConnectionStatus",
                    params={"id": _conn_id},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json(content_type=None)
                    is_alive = isinstance(data, dict) and data.get("isConnected")
                    if is_alive:
                        log.debug("[mt5_keepalive] ✅ Broker session alive")
                    else:
                        # Log only — watchdog will detect and reconnect within its interval
                        log.warning(
                            "[mt5_keepalive] Broker ConnectionStatus=false — "
                            "watchdog will reconnect (no action taken here)"
                        )
            except Exception as exc:
                # Fail-open: log but never crash or reconnect from keepalive
                log.debug(f"[mt5_keepalive] ping skipped: {exc}")

    except asyncio.CancelledError:
        log.info("[mt5_keepalive] MT5 broker-session keepalive stopped")
        raise


# ── Market data ───────────────────────────────────────────────────────────────

async def fetch_candles(
    symbol: str, timeframe: str, count: int = 300
) -> List[OHLCV]:
    """Fetch OHLCV candles via GET /PriceHistoryV2 (ISO datetime range).

    FIX: retries once with a fresh ConnectEx on stale-conn_id errors.
    """
    for attempt in range(2):
        if not _conn_id:
            if not await ensure_connected():
                return []

        tf_min = _TF_MAP.get(timeframe, 5)

        # PriceHistoryV2 takes a calendar date range. H1 needs a longer
        # lookback than count*1h so weekends/holidays do not reduce a
        # requested 300-bar history below the EMA-200 warm-up requirement.
        now = datetime.now(timezone.utc)
        request_minutes = (
            _h1_request_minutes(count)
            if tf_min == 60
            else tf_min * (count + 5)
        )
        from_dt = now - timedelta(minutes=request_minutes)

        from_str = from_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        to_str   = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            sess = _get_session()
            async with sess.get(
                f"{_base_url}/PriceHistoryV2",
                params={
                    "id":        _conn_id,
                    "symbol":    symbol,
                    "from":      from_str,
                    "to":        to_str,
                    "timeFrame": tf_min,
                },
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                data = await resp.json(content_type=None)

                if not isinstance(data, list):
                    # Error-shaped response — likely stale conn_id.
                    if attempt == 0:
                        log.warning(
                            f"fetch_candles unexpected response (stale conn_id?) "
                            f"— reconnecting and retrying. Response: {str(data)[:200]}"
                        )
                        _invalidate_connection()
                        continue
                    log.error(f"fetch_candles unexpected response after reconnect: {str(data)[:300]}")
                    return []

                candles_with_times: List[Tuple[datetime, OHLCV]] = []
                for bar in data:
                    candle_time = _parse_candle_time(bar.get("time", ""))
                    if candle_time is None:
                        log.warning("fetch_candles skipped bar with invalid timestamp")
                        continue
                    try:
                        candle = OHLCV(
                            # Canonical UTC timestamps make ordering and the
                            # H1 closed-bar check independent of API encoding.
                            time=candle_time.isoformat().replace("+00:00", "Z"),
                            open=float(bar.get("openPrice", 0.0)),
                            high=float(bar.get("highPrice", 0.0)),
                            low=float(bar.get("lowPrice", 0.0)),
                            close=float(bar.get("closePrice", 0.0)),
                            volume=float(bar.get("tickVolume", bar.get("volume", 0))),
                        )
                    except (TypeError, ValueError):
                        log.warning("fetch_candles skipped bar with invalid OHLCV values")
                        continue
                    candles_with_times.append((candle_time, candle))

                candles = _completed_candles(
                    candles_with_times,
                    timeframe_minutes=tf_min,
                    now=now,
                )

                # Return only the last `count` completed bars
                return candles[-count:] if len(candles) > count else candles

        except Exception as exc:
            if attempt == 0:
                log.warning(f"fetch_candles error (attempt 1) — reconnecting: {exc}")
                _invalidate_connection()
                continue
            log.error(f"fetch_candles error after reconnect: {exc}")
            return []
    return []


async def get_account_info() -> dict:
    """Fetch account balance/equity/margin from mt5rest /AccountSummary.

    FIX: retries once with a fresh ConnectEx when the bridge returns an
    error-shaped response (stale conn_id after bridge restart or broker
    session timeout).  Previously a stale conn_id caused a silent {} return
    which the panel interpreted as balance = $0.
    """
    for attempt in range(2):  # attempt 0 = normal; attempt 1 = after reconnect
        if not _conn_id:
            if not await ensure_connected():
                log.error("get_account_info: not connected to mt5rest bridge")
                return {}
        try:
            sess = _get_session()
            async with sess.get(
                f"{_base_url}/AccountSummary",
                params={"id": _conn_id},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json(content_type=None)
                if isinstance(data, dict) and "balance" in data:
                    return {
                        "balance":     float(data.get("balance",     0.0)),
                        "equity":      float(data.get("equity",      0.0)),
                        "margin":      float(data.get("margin",      0.0)),
                        "freeMargin":  float(data.get("freeMargin",  0.0)),
                        "marginLevel": float(data.get("marginLevel", 0.0)),
                        "currency":    data.get("currency", "USD"),
                        "leverage":    int(data.get("leverage") or 0),
                        # Identity fields — present in most mt5rest AccountSummary responses.
                        # These allow the Telegram panel to display broker/login even when the
                        # local SQLite DB was wiped (e.g. Render free-tier /tmp reset).
                        "broker":      str(data.get("broker") or data.get("company") or ""),
                        "server":      str(data.get("server") or ""),
                        "login":       str(data.get("login") or data.get("account") or ""),
                        "name":        str(data.get("name") or ""),
                    }
                # Error response — likely a stale conn_id (bridge restart / broker timeout).
                # Invalidate the connection and retry once with a fresh ConnectEx.
                if attempt == 0:
                    log.warning(
                        f"AccountSummary returned unexpected response "
                        f"(stale conn_id?) — reconnecting and retrying. "
                        f"Response: {str(data)[:200]}"
                    )
                    _invalidate_connection()
                    continue
                log.error(f"AccountSummary failed after reconnect: {str(data)[:200]}")
                return {}
        except Exception as exc:
            if attempt == 0:
                log.warning(f"get_account_info error (attempt 1) — reconnecting: {exc}")
                _invalidate_connection()
                continue
            log.error(f"get_account_info error after reconnect: {exc}")
            return {}
    return {}


async def get_account_balance() -> float:
    info = await get_account_info()
    return float(info.get("balance", 0.0))


async def get_open_positions(
    symbol: str = "",
    known_positions: Optional[Dict[str, dict]] = None,
    return_diagnostics: bool = False,
):
    """Fetch open positions from mt5rest.

    Raises RuntimeError when mt5rest returns an error response (dict with
    code/stackTrace) so callers treat a bridge error as a connection failure
    rather than silently assuming zero open positions.  Returning [] on an
    error could cause duplicate-entry: the robot sees 0 positions and opens
    a second trade on top of an existing one.

    known_positions: optional map of {str(ticket): {"volume":, "direction":}}
    for tickets this robot itself opened, used to repair a corrupted lone row
    for that same ticket instead of dropping it — see _dedupe_positions.

    return_diagnostics: when True, returns (positions, dropped_unknown_tickets)
    instead of just positions. dropped_unknown_tickets lists tickets that were
    dropped as phantom rows because they didn't match known_positions — this
    does NOT prove a real position is open (most such rows are genuine bridge
    garbage), but it is a signal the entry gate uses to be conservative: it
    would rather skip one bar's entry than risk opening on top of a position
    it simply doesn't recognize yet (e.g. right after a restart, before
    known_positions has repopulated — see live_loop._known_open_tickets).

    FIX: retries once with a fresh ConnectEx on stale-conn_id errors.
    """
    for attempt in range(2):
        if not _conn_id:
            if not await ensure_connected():
                raise RuntimeError("get_open_positions: not connected to mt5rest bridge")
        try:
            params: dict = {"id": _conn_id}
            if symbol:
                params["symbol"] = symbol
            async with _get_session().get(
                f"{_base_url}/OpenedOrders",
                params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json(content_type=None)
                # If the bridge returns an error dict on attempt 0, the conn_id is likely
                # stale — invalidate and retry rather than raising immediately.
                if attempt == 0 and isinstance(data, dict) and "stackTrace" in data:
                    log.warning(
                        f"OpenedOrders error (stale conn_id?) — reconnecting and retrying. "
                        f"Response: {str(data)[:200]}"
                    )
                    _invalidate_connection()
                    continue
                positions, dropped = _parse_open_positions_response(
                    data, resp.status, known_positions
                )
                return (positions, dropped) if return_diagnostics else positions
        except RuntimeError:
            raise
        except Exception as exc:
            if attempt == 0:
                log.warning(f"get_open_positions error (attempt 1) — reconnecting: {exc}")
                _invalidate_connection()
                continue
            log.error(f"get_open_positions error after reconnect: {exc}")
            raise RuntimeError(f"get_open_positions failed: {exc}") from exc
    raise RuntimeError("get_open_positions: failed after reconnect attempt")


async def get_closed_position_history(position_id: str) -> Optional[dict]:
    """Return the completed MT5 history record for one position ticket.

    An empty result is deliberately returned as ``None``.  A position can
    temporarily disappear from ``OpenedOrders`` while the bridge is syncing;
    callers must not treat that as a completed trade until this endpoint
    returns a record that contains close data.
    """
    if not position_id:
        return None

    for attempt in range(2):
        if not _conn_id:
            if not await ensure_connected():
                raise RuntimeError(
                    "get_closed_position_history: not connected to mt5rest bridge"
                )
        try:
            async with _get_session().get(
                f"{_base_url}/HistoryPositions",
                params={"id": _conn_id, "tickets": int(position_id)},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json(content_type=None)
                if attempt == 0 and isinstance(data, dict) and "stackTrace" in data:
                    log.warning(
                        "HistoryPositions error (stale conn_id?) — "
                        "reconnecting and retrying."
                    )
                    _invalidate_connection()
                    continue
                if resp.status >= 400:
                    message = (
                        data.get("message", f"HTTP {resp.status}")
                        if isinstance(data, dict)
                        else f"HTTP {resp.status}"
                    )
                    raise RuntimeError(
                        f"mt5rest HistoryPositions error: {message}"
                    )

                if isinstance(data, dict):
                    candidates = data.get("orders", [])
                elif isinstance(data, list):
                    candidates = data
                else:
                    candidates = []

                wanted = str(position_id)
                for record in candidates:
                    if not isinstance(record, dict):
                        continue
                    ticket = record.get(
                        "ticket",
                        record.get("ticketNumber", record.get("positionTicket")),
                    )
                    if ticket is not None and str(ticket) == wanted:
                        return record
                return None
        except RuntimeError:
            raise
        except Exception as exc:
            if attempt == 0:
                log.warning(
                    f"get_closed_position_history error (attempt 1) — "
                    f"reconnecting: {exc}"
                )
                _invalidate_connection()
                continue
            log.error(f"get_closed_position_history failed: {exc}")
            raise RuntimeError(
                f"get_closed_position_history failed: {exc}"
            ) from exc
    raise RuntimeError(
        "get_closed_position_history: failed after reconnect attempt"
    )


def _parse_open_positions_response(
    data: object, status: int, known_positions: Optional[Dict[str, dict]] = None
) -> Tuple[List[dict], List[str]]:
    """Accept only a successful list response from OpenedOrders.

    Any other payload is an unknown position state. Returning an empty list for
    an error-shaped or malformed response could allow a duplicate entry.

    Returns (positions, dropped_unknown_tickets).
    """
    if status < 200 or status >= 300:
        message = (
            data.get("message", f"HTTP {status}")
            if isinstance(data, dict)
            else f"HTTP {status}"
        )
        raise RuntimeError(f"mt5rest OpenedOrders error (HTTP {status}): {message}")
    if isinstance(data, list):
        return _dedupe_positions(data, known_positions)
    if isinstance(data, dict):
        message = data.get("message", "unexpected object response")
    else:
        message = f"unexpected response type: {type(data).__name__}"
    raise RuntimeError(f"mt5rest OpenedOrders error: {message}")


# mt5rest exposes both `lots` (the canonical lot value) and `volume` (an
# integer internal volume).  For example, the bridge can report
# volume=1,000,000 alongside lots=0.01.  Reading `volume` as lots flows
# straight into the
# live position list: it inflates MAX_OPEN_TRADES counting, gets written
# into the panel snapshot, and the Telegram panel's new-ticket detector
# (which iterates every row matching a newly-seen ticket) fires a second
# "TRADE OPENED" notification for the same trade with fabricated size/price.
# Prefer the bridge's `lots` field. If an older bridge omits it, accept only
# a small, already lot-like `volume`; otherwise the row is untrusted. Never
# guess at a conversion for a large raw value.
_MAX_SANE_VOLUME_LOTS = 100.0


def _finite_positive(value: object) -> Optional[float]:
    """Return a finite positive number, or None for malformed MT5 data."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number if number > 0 else None


def _position_lots(row: dict) -> Optional[float]:
    """Read the canonical lot size from an mt5rest OpenedOrder.

    `lots` is the public lot value. `volume` may be an integer internal value
    in some mt5rest responses, so a large raw value is never used as a trade
    size when the canonical `lots` field is absent.
    """
    lots = _finite_positive(row.get("lots"))
    if lots is not None:
        return lots

    volume = _finite_positive(row.get("volume"))
    if volume is None or volume > _MAX_SANE_VOLUME_LOTS:
        # Without the canonical `lots` field, a huge raw volume is
        # untrusted. Do not guess whether it is an internal unit value.
        return None
    return volume


def _position_direction(row: dict) -> str:
    """Return BUY/SELL when the bridge gives a known market direction."""
    raw = row.get("type")
    if raw is None:
        raw = row.get("orderType")
    if isinstance(raw, str):
        direction = raw.upper().strip()
        if direction in {"BUY", "SELL"}:
            return direction
        try:
            raw = int(raw)
        except (TypeError, ValueError):
            return "UNKNOWN"
    try:
        return {0: "BUY", 1: "SELL"}.get(int(raw), "UNKNOWN")
    except (TypeError, ValueError):
        return "UNKNOWN"


def _dedupe_positions(
    rows: List[dict], known_positions: Optional[Dict[str, dict]] = None
) -> List[dict]:
    """Collapse duplicate rows for the same ticket into a single sane entry.

    Prefers a row with a plausible volume; if every duplicate for a ticket
    looks corrupted, keeps the first as a fallback so a real (if noisy)
    position is never silently dropped, but logs loudly either way.

    FIX: Also drops lone phantom rows whose volume exceeds _MAX_SANE_VOLUME_LOTS
    even when they carry a different (or zero/null) ticket from the real
    position, because in that case same-ticket deduplication cannot catch them.
    Such rows are always bridge artifacts — no retail gold account can open a
    1 000 000-lot position — and letting them through would write them into the
    Redis snapshot where the Telegram heartbeat treats them as a brand-new
    trade and fires a spurious "TRADE OPENED" notification with fabricated
    direction, size, and price.

    ROOT-CAUSE FIX (observed live): for a ticket this robot itself opened,
    mt5rest can return a *lone* row for that exact ticket with the volume/type
    fields corrupted (insane volume, type=None) — and, unlike the scenario
    above, this is not a one-off blip: it can repeat on every single poll for
    the rest of that position's life. Since OpenedOrders only ever lists
    currently-open positions, the ticket showing up at all — even corrupted —
    is itself proof the position is still open in MT5. Dropping it outright
    made get_open_positions() permanently blind to a real, live position,
    which let MAX_OPEN_TRADES be bypassed (observed: 4 simultaneous BUY
    positions opened one bar apart while MAX_OPEN_TRADES=1). If the caller
    passes `known_positions` (a {str(ticket): {"volume", "direction"}} map
    built from this robot's own trade log) and the corrupted ticket is a
    known one, repair only the volume/type fields from our own record instead
    of dropping the row — the ticket/openPrice from mt5rest are kept as-is.
    An unknown ticket (never opened by this robot) is still dropped exactly
    as before; this only rescues positions we can independently verify.
    """
    known_positions = known_positions or {}
    by_ticket: "dict[object, List[dict]]" = {}
    order: List[object] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticket = row.get("ticket", row.get("identifier"))
        if ticket not in by_ticket:
            by_ticket[ticket] = []
            order.append(ticket)
        by_ticket[ticket].append(row)

    result: List[dict] = []
    dropped_unknown: List[str] = []
    for ticket in order:
        group = by_ticket[ticket]
        if len(group) == 1:
            row = group[0]
            lots = _position_lots(row)
            direction = _position_direction(row)
            if lots is None:
                known = known_positions.get(str(ticket))
                if known is not None and _finite_positive(known.get("volume")) is not None:
                    # Ticket is one we opened ourselves and mt5rest still
                    # lists it in OpenedOrders — it is genuinely open, but
                    # the bridge did not provide a usable lot value/direction.
                    # Repair rather than drop.
                    repaired = dict(row)
                    repaired["volume"] = known["volume"]
                    repaired["lots"] = known["volume"]
                    repaired["type"] = known["direction"]
                    log.warning(
                        f"mt5rest returned ticket {ticket!r} with a corrupted "
                        f"volume/type ({row.get('volume')!r}/{row.get('type')!r}) — "
                        f"repaired from this robot's own trade log "
                        f"(volume={known['volume']}, direction={known['direction']}) "
                        f"instead of dropping a position known to be open."
                    )
                    result.append(repaired)
                else:
                    # Unknown/foreign ticket: no record of ever opening it —
                    # phantom bridge artifact, drop it entirely as before.
                    # Still recorded in dropped_unknown so the entry gate can
                    # be conservative (see get_open_positions docstring): most
                    # of the time this really is bridge garbage, but right
                    # after a restart (known_positions not yet repopulated)
                    # it could be a real ticket we just don't recognise yet.
                    log.warning(
                        f"mt5rest returned a lone row for ticket {ticket!r} "
                        f"with an unusable lot value "
                        f"(volume={row.get('volume')!r}, lots={row.get('lots')!r}) "
                        f"— dropping phantom row (direction={direction}, "
                        f"openPrice={row.get('openPrice', row.get('price_open'))})"
                    )
                    dropped_unknown.append(str(ticket))
            else:
                result.append(row)
            continue

        sane = [
            row for row in group
            if _position_lots(row) is not None
        ]
        if len(sane) == 1:
            log.warning(
                f"mt5rest returned {len(group)} duplicate rows for ticket {ticket} "
                f"— keeping the one with a plausible volume, dropping the rest "
                f"(lots seen: {[_position_lots(row) for row in group]})"
            )
            result.append(sane[0])
        else:
            log.warning(
                f"mt5rest returned {len(group)} duplicate rows for ticket {ticket} "
                f"with no single plausible volume — keeping the first row only "
                f"(lots seen: {[_position_lots(row) for row in group]})"
            )
            result.append(group[0])
    return result, dropped_unknown


async def get_last_completed_bar_time(
    symbol: str, timeframe: str
) -> Optional[datetime]:
    candles = await fetch_candles(symbol, timeframe, count=3)
    if not candles:
        return None
    t = candles[-1].time
    if isinstance(t, datetime):
        return t
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00"))
    except Exception:
        return None


def mt5_pos_to_dict(pos: dict) -> dict:
    """Normalise a raw mt5rest OpenedOrder dict into the standard internal format."""
    return {
        "id":         str(pos.get("ticket", pos.get("identifier", ""))),
        "ticket":     pos.get("ticket", pos.get("identifier", 0)),
        "symbol":     pos.get("symbol", ""),
        "type":       _position_direction(pos),
        "volume":     _position_lots(pos) or 0.0,
        "open_price": float(pos.get("openPrice", pos.get("price_open", 0.0))),
        "sl":         float(pos.get("stopLoss",  pos.get("sl", 0.0))),
        "tp":         float(pos.get("takeProfit", pos.get("tp", 0.0))),
        "profit":     float(pos.get("profit",    0.0)),
        "open_time":  pos.get("openTime",  pos.get("time",    0)),
        "comment":    pos.get("comment",   ""),
    }


async def get_current_quote(symbol: str) -> dict:
    """Fetch the current bid/ask price via GET /GetQuote.

    Used by the staircase trailing-stop engine, which needs a live price
    between M5 candle closes (candles only give the price as of the last
    completed bar, up to 5 minutes stale).

    Returns {"bid": float, "ask": float} on success, {} on any failure —
    callers must treat {} as "no live price available this tick" and skip
    trailing work rather than trail off a stale/fabricated price.

    FIX: retries once with a fresh ConnectEx on stale-conn_id errors, same
    pattern as the other mt5rest calls in this module.
    """
    for attempt in range(2):
        if not _conn_id:
            if not await ensure_connected():
                return {}
        try:
            sess = _get_session()
            async with sess.get(
                f"{_base_url}/GetQuote",
                params={"id": _conn_id, "symbol": symbol},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json(content_type=None)

                if isinstance(data, dict) and not _is_error_quote(data):
                    bid = data.get("bid", data.get("Bid"))
                    ask = data.get("ask", data.get("Ask"))
                    if bid is not None and ask is not None:
                        return {"bid": float(bid), "ask": float(ask)}

                if attempt == 0:
                    log.warning(
                        f"GetQuote unexpected response (stale conn_id?) "
                        f"— reconnecting and retrying. Response: {str(data)[:200]}"
                    )
                    _invalidate_connection()
                    continue
                log.warning(f"GetQuote failed after reconnect: {str(data)[:200]}")
                return {}
        except Exception as exc:
            if attempt == 0:
                log.warning(f"get_current_quote error (attempt 1) — reconnecting: {exc}")
                _invalidate_connection()
                continue
            log.warning(f"get_current_quote error after reconnect: {exc}")
            return {}
    return {}


def _is_error_quote(data: dict) -> bool:
    return "code" in data and "stackTrace" in data


def get_connection() -> Optional[str]:
    """Return the base URL when connected, None otherwise (used by executor)."""
    return _base_url if _connected else None


def get_conn_id() -> Optional[str]:
    """Return the active connection UUID (used by executor)."""
    return _conn_id if _connected else None


def is_connected() -> bool:
    return _connected


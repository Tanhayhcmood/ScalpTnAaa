"""
MTAPI MT5 REST Connector – GoldScalperPro v4

Connects directly to the MTAPI REST API. The token returned by ``/ConnectEx``
is held in memory and passed as ``id`` to every later request.

Required env vars:
    MTAPI_URL     – normally https://mt5.mtapi.io
    MT5_HOST      – broker server name accepted by MTAPI
    MT5_PORT      – broker port, normally 443
    MT5_USER      – MT5 account login number
    MT5_PASSWORD  – MT5 account password

MTAPI endpoints used:
    GET  /ConnectEx        – authenticate with broker server name, returns session token
    GET  /Disconnect       – close connection
    GET  /ConnectionStatus – check live connection
    GET  /AccountSummary   – balance, equity, margin
    GET  /OpenedOrders     – open positions
    GET  /HistoryPositions – completed positions by ticket
    GET  /OrderHistory    – account order/deal history by UTC range
    GET  /PriceHistoryV2   – OHLCV candles (ISO datetime range)
    GET  /GetQuote         – current bid/ask price
    GET  /SymbolList       – verify the configured instrument is available
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
_conn_id:    str  = ""   # Token returned by Connect; passed to every call
_last_connect_time: float = 0.0   # monotonic timestamp of last successful connect()
# Kept as a narrow test/adaptor seam for callers that provide an RPC-shaped
# fake. Production traffic never uses this object; it always uses MTAPI REST.
_connection = None
_account = None
_consecutive_health_failures = 0
_health_lock = asyncio.Lock()
# Deprecated compatibility names intentionally remain empty. They are not read
# by the direct MTAPI implementation and do not enable MetaAPI.
METAAPI_TOKEN = ""
METAAPI_ACCOUNT_ID = ""

_CONNECT_GRACE_PERIOD: float = 150.0
_CONNECT_READY_POLL_INITIAL_S: float = 2.0
_CONNECT_READY_POLL_MAX_S: float = 15.0

_reconnect_lock: asyncio.Lock | None = None
_watchdog_task: asyncio.Task | None = None
_MT5_KEEPALIVE_TASK: asyncio.Task | None = None
_MT5_KEEPALIVE_INTERVAL_S: float = 180.0
_MT5_SESSION_REFRESH_AGE_S: float = 14400.0


def _connect_params(
    user: str,
    password: str,
    host: str,
    port: int = 443,
) -> dict[str, object]:
    """Build query parameters for MTAPI's server-name ``/ConnectEx`` endpoint.

    The host/IP ``/Connect`` path has been returning a disposed-socket error
    immediately after sending the login. ``/ConnectEx`` keeps the broker server
    name and lets MTAPI resolve the correct cluster member.
    """
    return {
        "user": user,
        "password": password,
        "server": host,
        "connectTimeoutClusterMemberSeconds": 30,
        "connectToNearestByPing": "true",
        "connectTimeoutSeconds": 60,
        "errorReplyStatusCode": 400,
    }


def _get_reconnect_lock() -> asyncio.Lock:
    """Lazily create the reconnect lock on the running event loop."""
    global _reconnect_lock
    if _reconnect_lock is None:
        _reconnect_lock = asyncio.Lock()
    return _reconnect_lock


_TF_MAP = {
    "1m": 1, "5m": 5, "10m": 10, "15m": 15, "20m": 20, "30m": 30,
    "1h": 60, "4h": 240, "1d": 1440,
    "M1": 1, "M5": 5, "M10": 10, "M15": 15, "M20": 20, "M30": 30,
    "H1": 60, "H4": 240, "D1": 1440,
}

def _parse_candle_time(value: object) -> Optional[datetime]:
    """Parse an MTAPI candle timestamp into an aware UTC datetime."""
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


def _parse_time(value: object) -> Optional[datetime]:
    """Backward-compatible name for the connector timestamp normalizer."""
    if value is None:
        return None
    return _parse_candle_time(value)


def _account_field(info: object, key: str, default: object = None) -> object:
    """Read a field from either a dict or a simple adapter object."""
    if isinstance(info, dict):
        return info.get(key, default)
    return getattr(info, key, default)


def _h1_request_minutes(count: int) -> int:
    active_bars = max(count + 10, math.ceil(count * 7 / 5))
    return active_bars * 60 + (3 * 24 * 60)


def _completed_candles(
    candles: List[Tuple[datetime, OHLCV]],
    *,
    timeframe_minutes: int,
    now: datetime,
) -> List[OHLCV]:
    """Keep chronologically ordered, completed candles."""
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
            candle for candle_time, candle in ordered
            if candle_time + timedelta(hours=1) <= close_cutoff
        ]
    return [candle for _, candle in ordered[:-1]]


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    return _session


def _connection_status_is_alive(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    value = data.get("isConnected", data.get("connected"))
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "connected"}
    return bool(value)


async def _wait_for_broker_ready(
    base_url: str,
    conn_id: str,
    *,
    timeout_s: float = _CONNECT_GRACE_PERIOD,
) -> bool:
    deadline = _time.monotonic() + timeout_s
    delay = _CONNECT_READY_POLL_INITIAL_S
    last_status = ""
    while True:
        try:
            sess = _get_session()
            async with sess.get(
                f"{base_url}/ConnectionStatus",
                params={"id": conn_id},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json(content_type=None)
                if _connection_status_is_alive(data):
                    return True
                last_status = str(data)[:180] if resp.status < 500 else f"HTTP {resp.status}"
        except Exception as exc:
            last_status = str(exc)[:180]
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            log.error(
                "MT5 session was created but broker was not ready within "
                f"{timeout_s:.0f}s (last status: {last_status})"
            )
            return False
        await asyncio.sleep(min(delay, remaining))
        delay = min(delay * 1.5, _CONNECT_READY_POLL_MAX_S)


async def _connect_unlocked() -> bool:
    """Perform one MTAPI connection attempt.

    Callers must use ``connect()`` rather than invoking this helper directly.
    Keeping the HTTP handshake under the reconnect lock prevents a watchdog,
    retry loop, or manual reconnect from replacing the same broker session
    concurrently.
    """
    global _connected, _base_url, _conn_id, _last_connect_time

    base = MTAPI_URL.rstrip("/") if MTAPI_URL else ""
    host = MT5_HOST
    user = MT5_USER.strip() if MT5_USER else ""
    password = MT5_PASSWORD.strip() if MT5_PASSWORD else ""
    if not base:
        log.error("MTAPI_URL is not set. Set it to the MTAPI REST base URL.")
        return False
    if not user or not password or not host:
        log.error("MT5_USER, MT5_PASSWORD, and MT5_HOST must be set for MTAPI /ConnectEx.")
        return False

    previous_conn_id = _conn_id
    previous_connected = _connected
    _base_url = base
    sess = _get_session()
    try:
        log.info(
            "MTAPI /ConnectEx request prepared: "
            f"account={user[:3]}*** host={host} port={MT5_PORT} "
            f"password_configured={bool(password)} (value redacted)"
        )
        log.info(
            f"MTAPI connection attempt: {base}/ConnectEx "
            f"(host={host}, port={MT5_PORT}, account={user[:3]}***)"
        )
        async with sess.get(
            f"{base}/ConnectEx",
            params=_connect_params(user, password, host, MT5_PORT),
            timeout=aiohttp.ClientTimeout(total=SYNC_TIMEOUT),
        ) as resp:
            raw = await resp.text()
            log.debug(f"MTAPI /ConnectEx response ({resp.status}): [response received]")
            if resp.status != 200:
                log.error(f"MTAPI /ConnectEx failed (status={resp.status}): {raw[:300]}")
                _connected = False
                return False
            conn_id = raw.strip().strip('"')
            if not conn_id or len(conn_id) < 10:
                log.error(f"MTAPI /ConnectEx returned an unexpected token: {raw[:200]}")
                _connected = False
                return False
            if not await _wait_for_broker_ready(base, conn_id):
                try:
                    async with sess.get(
                        f"{base}/Disconnect",
                        params={"id": conn_id},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ):
                        pass
                except Exception:
                    pass
                _connected = previous_connected
                _conn_id = previous_conn_id
                return False
            _conn_id = conn_id
            _connected = True
            _last_connect_time = _time.monotonic()
            log.info(
                f"MTAPI connection success: MT5 account connected "
                f"(host={host}, port={MT5_PORT}, account={user[:3]}***)"
            )
            if previous_conn_id and previous_conn_id != conn_id:
                try:
                    async with sess.get(
                        f"{base}/Disconnect",
                        params={"id": previous_conn_id},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ):
                        pass
                except Exception as exc:
                    log.debug(f"Previous MT5 session cleanup skipped: {exc}")
            return True
    except Exception as exc:
        log.error(f"MTAPI connection failure: {exc}")
        _connected = False
        return False


async def connect(*args, **kwargs) -> bool:
    """Connect through MTAPI with one serialized broker handshake."""
    lock = _get_reconnect_lock()
    async with lock:
        if (
            _connected
            and _conn_id
            and _time.monotonic() - _last_connect_time < _CONNECT_GRACE_PERIOD
        ):
            return True
        return await _connect_unlocked()


async def disconnect() -> None:
    """Close the MTAPI connection and release the HTTP session."""
    global _connected, _session, _conn_id, _base_url
    lock = _get_reconnect_lock()
    async with lock:
        conn_id, base_url, session = _conn_id, _base_url, _session
        _connected, _conn_id, _base_url = False, "", ""
        if conn_id and base_url and session and not session.closed:
            try:
                async with session.get(
                    f"{base_url}/Disconnect",
                    params={"id": conn_id},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    if response.status >= 400:
                        log.warning(f"MT5 disconnect request returned HTTP {response.status}")
            except Exception as exc:
                log.warning(f"MT5 disconnect request failed: {exc}")
        if session and not session.closed:
            try:
                await session.close()
            except Exception as exc:
                log.warning(f"MT5 HTTP session close failed: {exc}")
        _session = None


async def keepalive_mtapi() -> bool:
    if not _base_url or not _conn_id:
        return False
    try:
        async with _get_session().get(
            f"{_base_url}/ConnectionStatus",
            params={"id": _conn_id},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
            ok = resp.status == 200 and _connection_status_is_alive(data)
            if not ok:
                log.warning(f"MTAPI broker-session keepalive returned {resp.status}")
            return ok
    except Exception as exc:
        log.warning(f"MTAPI broker-session keepalive failed: {exc}")
        return False


async def keepalive_metaapi() -> bool:
    """Compatibility shim for the old health-check test/adaptor contract.

    It deliberately uses only a supplied fake connection. Production code
    calls ``keepalive_mtapi`` and never imports or instantiates MetaAPI.
    """
    global _connected, _consecutive_health_failures
    legacy_connection = globals().get("_connection")
    get_info = getattr(legacy_connection, "get_account_information", None)
    if get_info is None:
        return await keepalive_mtapi()
    try:
        await get_info()
        _consecutive_health_failures = 0
        return True
    except Exception as exc:
        _consecutive_health_failures += 1
        if _consecutive_health_failures >= 3:
            _connected = False
        return False


async def connect_with_retry(max_attempts: int = 5, retry_delay: float = 60.0) -> bool:
    for attempt in range(1, max_attempts + 1):
        log.info(f"MT5 connect attempt {attempt}/{max_attempts} ...")
        if await connect():
            return True
        if attempt < max_attempts:
            log.warning(f"MT5 connect failed (attempt {attempt}). Waiting {retry_delay}s ...")
            await asyncio.sleep(retry_delay)
    log.error(f"MT5 connect failed after {max_attempts} attempts.")
    return False


def _invalidate_connection() -> None:
    global _connected, _conn_id
    log.warning("MT5 conn_id is stale — invalidating connection (will reconnect on retry)")
    _connected = False
    _conn_id = ""


async def ensure_connected(*args, **kwargs) -> bool:
    global _connected
    if (
        _conn_id and _connected
        and _time.monotonic() - _last_connect_time < _CONNECT_GRACE_PERIOD
    ):
        return True
    if _conn_id and _base_url:
        try:
            async with _get_session().get(
                f"{_base_url}/ConnectionStatus",
                params={"id": _conn_id},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json(content_type=None)
                if _connection_status_is_alive(data):
                    _connected = True
                    return True
        except Exception:
            pass
    _connected = False
    # connect() owns the lock and rechecks state after waiting for any other
    # handshake to finish. Avoid holding the lock across retry sleeps.
    return await connect_with_retry(max_attempts=3, retry_delay=30.0)


async def start_connection_watchdog(interval_seconds: float = 30.0) -> None:
    global _watchdog_task
    log.info(f"[watchdog] MT5 connection watchdog started (interval={interval_seconds}s)")
    failures = 0
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            if _connected and _time.monotonic() - _last_connect_time < _CONNECT_GRACE_PERIOD:
                continue
            if not _connected or not _conn_id:
                if await ensure_connected():
                    log.info("[watchdog] MT5 proactive reconnect succeeded")
                continue
            try:
                async with _get_session().get(
                    f"{_base_url}/ConnectionStatus",
                    params={"id": _conn_id},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    data = await resp.json(content_type=None)
                    if _connection_status_is_alive(data):
                        failures = 0
                    else:
                        failures += 1
            except Exception as exc:
                failures += 1
                log.warning(f"[watchdog] ConnectionStatus check failed ({failures}/2): {exc}")
            if failures >= 2:
                failures = 0
                _invalidate_connection()
                await ensure_connected()
    except asyncio.CancelledError:
        log.info("[watchdog] MT5 connection watchdog stopped")
        raise


async def start_mt5_session_keepalive(
    interval_s: float = _MT5_KEEPALIVE_INTERVAL_S,
    refresh_age_s: float = _MT5_SESSION_REFRESH_AGE_S,
) -> None:
    global _MT5_KEEPALIVE_TASK
    log.info(
        f"[mt5_keepalive] MT5 broker-session keepalive started "
        f"(ping every {interval_s:.0f}s, refresh after {refresh_age_s/3600:.1f}h)"
    )
    try:
        while True:
            await asyncio.sleep(interval_s)
            if not _connected or not _conn_id or not _base_url:
                continue
            if _time.monotonic() - _last_connect_time > refresh_age_s:
                await connect()
                continue
            await keepalive_mtapi()
    except asyncio.CancelledError:
        log.info("[mt5_keepalive] MT5 broker-session keepalive stopped")
        raise


async def check_symbol_available(symbol: str) -> bool:
    target = symbol.strip().upper()
    if not target:
        return False
    for attempt in range(2):
        if not _conn_id and not await ensure_connected():
            return False
        try:
            async with _get_session().get(
                f"{_base_url}/SymbolList",
                params={"id": _conn_id},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    raise RuntimeError(f"HTTP {resp.status}: {str(data)[:160]}")
                if isinstance(data, list):
                    names = [str(item) for item in data]
                elif isinstance(data, dict):
                    names = [str(item) for item in data.keys()]
                else:
                    raise RuntimeError(f"unexpected response type {type(data).__name__}")
                available = {name.upper() for name in names}
                if target in available:
                    log.info(f"{symbol} availability: AVAILABLE via MTAPI")
                    return True
                log.error(f"{symbol} availability: NOT AVAILABLE via MTAPI")
                return False
        except Exception as exc:
            if attempt == 0:
                _invalidate_connection()
                continue
            log.error(f"MTAPI symbol check failed after reconnect: {exc}")
            return False
    return False


async def fetch_candles(
    symbol: str, timeframe: str, count: int = 300
) -> List[OHLCV]:
    """Fetch the latest completed OHLCV candles via MTAPI.

    The returned window is always the most recent completed history, never a
    day-start slice. Callers choose the exact rolling window needed by their
    indicator warm-up requirements.
    """
    requested_count = max(1, int(count))
    # Compatibility path for the historical RPC-shaped adapter contract.
    # This is only exercised when a caller supplies _account explicitly.
    legacy_account = globals().get("_account")
    get_history = getattr(legacy_account, "get_historical_candles", None)
    if get_history is not None:
        try:
            tf = _TF_MAP.get(timeframe, 5)
            tf_label = next(
                (label for label, minutes in _TF_MAP.items() if minutes == tf and label.islower()),
                timeframe,
            )
            now = datetime.now(timezone.utc)
            request_minutes = (
                _h1_request_minutes(requested_count)
                if tf == 60 else tf * (requested_count + 5)
            )
            raw_candles = await get_history(
                symbol=symbol,
                timeframe=tf_label,
                start_time=now - timedelta(minutes=request_minutes),
                limit=requested_count + 5,
            )
            parsed: list[tuple[datetime, OHLCV]] = []
            for row in raw_candles or []:
                if not isinstance(row, dict):
                    continue
                candle_time = _parse_time(row.get("time") or row.get("brokerTime"))
                if candle_time is None:
                    continue
                parsed.append((
                    candle_time,
                    OHLCV(
                        time=candle_time,
                        open=float(row.get("open", row.get("openPrice", 0))),
                        high=float(row.get("high", row.get("highPrice", 0))),
                        low=float(row.get("low", row.get("lowPrice", 0))),
                        close=float(row.get("close", row.get("closePrice", 0))),
                        volume=float(row.get("tickVolume", row.get("volume", 0))),
                    ),
                ))
            parsed.sort(key=lambda item: item[0])
            return [candle for _, candle in parsed[:-1]][-requested_count:]
        except Exception:
            return []
    for attempt in range(2):
        if not _conn_id and not await ensure_connected():
            return []
        tf_min = _TF_MAP.get(timeframe, 5)
        now = datetime.now(timezone.utc)
        request_minutes = (
            _h1_request_minutes(requested_count)
            if tf_min == 60 else tf_min * (requested_count + 5)
        )
        from_str = (now - timedelta(minutes=request_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        to_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            async with _get_session().get(
                f"{_base_url}/PriceHistoryV2",
                params={
                    "id": _conn_id,
                    "symbol": symbol,
                    "from": from_str,
                    "to": to_str,
                    "timeFrame": tf_min,
                },
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                data = await resp.json(content_type=None)
                if not isinstance(data, list):
                    if attempt == 0:
                        _invalidate_connection()
                        continue
                    return []
                candles_with_times: List[Tuple[datetime, OHLCV]] = []
                for bar in data:
                    candle_time = _parse_candle_time(bar.get("time", ""))
                    if candle_time is None:
                        continue
                    try:
                        candle = OHLCV(
                            time=candle_time.isoformat().replace("+00:00", "Z"),
                            open=float(bar.get("openPrice", 0.0)),
                            high=float(bar.get("highPrice", 0.0)),
                            low=float(bar.get("lowPrice", 0.0)),
                            close=float(bar.get("closePrice", 0.0)),
                            volume=float(bar.get("tickVolume", bar.get("volume", 0))),
                        )
                    except (TypeError, ValueError):
                        continue
                    candles_with_times.append((candle_time, candle))
                candles = _completed_candles(
                    candles_with_times,
                    timeframe_minutes=tf_min,
                    now=now,
                )
                return (
                    candles[-requested_count:]
                    if len(candles) > requested_count
                    else candles
                )
        except Exception as exc:
            if attempt == 0:
                log.warning(f"fetch_candles error (attempt 1) — reconnecting: {exc}")
                _invalidate_connection()
                continue
            log.error(f"fetch_candles error after reconnect: {exc}")
            return []
    return []


async def get_account_info() -> dict:
    """Fetch account balance/equity/margin from MTAPI /AccountSummary."""
    for attempt in range(2):
        if not _conn_id and not await ensure_connected():
            return {}
        try:
            async with _get_session().get(
                f"{_base_url}/AccountSummary",
                params={"id": _conn_id},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json(content_type=None)
                if isinstance(data, dict) and "balance" in data:
                    return {
                        "balance": float(data.get("balance", 0.0)),
                        "equity": float(data.get("equity", 0.0)),
                        "margin": float(data.get("margin", 0.0)),
                        "freeMargin": float(data.get("freeMargin", 0.0)),
                        "marginLevel": float(data.get("marginLevel", 0.0)),
                        "currency": data.get("currency", "USD"),
                        "leverage": int(data.get("leverage") or 0),
                        "broker": str(data.get("broker") or data.get("company") or MT5_HOST),
                        "server": str(data.get("server") or MT5_HOST),
                        "login": str(data.get("login") or data.get("account") or MT5_USER),
                        "synced": bool(data.get("synced", True)),
                        "name": str(data.get("name") or ""),
                    }
                if attempt == 0:
                    _invalidate_connection()
                    continue
                return {}
        except Exception as exc:
            if attempt == 0:
                _invalidate_connection()
                continue
            log.error(f"get_account_info error after reconnect: {exc}")
            return {}
    return {}


async def get_account_balance() -> float:
    return float((await get_account_info()).get("balance", 0.0))


async def get_open_positions(
    symbol: str = "",
    known_positions: Optional[Dict[str, dict]] = None,
    return_diagnostics: bool = False,
):
    """Fetch open positions from MTAPI and fail closed on malformed payloads."""
    legacy_connection = globals().get("_connection")
    get_positions = getattr(legacy_connection, "get_positions", None)
    if get_positions is not None and not _conn_id:
        try:
            await get_positions()
        except Exception as exc:
            raise RuntimeError(f"MTAPI get_positions timed out: {exc}") from exc
        return []
    for attempt in range(2):
        if not _conn_id and not await ensure_connected():
            raise RuntimeError("get_open_positions: not connected to MTAPI")
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
                if attempt == 0 and isinstance(data, dict) and "stackTrace" in data:
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
                _invalidate_connection()
                continue
            raise RuntimeError(f"get_open_positions failed: {exc}") from exc
    raise RuntimeError("get_open_positions: failed after reconnect attempt")


async def get_closed_position_history(position_id: str) -> Optional[dict]:
    # Compatibility with the normalized history contract used by the local
    # test/adaptor seam. The production path below calls MTAPI REST directly.
    legacy_connection = globals().get("_connection")
    get_deals = getattr(legacy_connection, "get_deals_by_position", None)
    if get_deals is not None:
        raw_deals = await get_deals(position_id)
        deals = raw_deals.get("deals", []) if isinstance(raw_deals, dict) else raw_deals
        if isinstance(raw_deals, dict) and "entryType" in raw_deals:
            deals = [raw_deals]
        if isinstance(deals, list):
            valid = [item for item in deals if isinstance(item, dict)]
            if valid:
                closing = [
                    item for item in valid
                    if str(item.get("entryType", "")).upper()
                    in {"DEAL_ENTRY_OUT", "DEAL_ENTRY_OUT_BY"}
                ] or valid
                deal = closing[-1]
                return {
                    "closePrice": deal.get("price", deal.get("closePrice")),
                    "closeTime": deal.get("time", deal.get("brokerTime")),
                    "profit": deal.get("profit", 0.0),
                    "commission": deal.get("commission", 0.0),
                    "swap": deal.get("swap", 0.0),
                    "fee": deal.get("fee", 0.0),
                    "closeComment": deal.get("comment", deal.get("brokerComment", "")),
                    "dealId": deal.get("id", deal.get("dealId", "")),
                }
    if not position_id:
        return None
    for attempt in range(2):
        if not _conn_id and not await ensure_connected():
            raise RuntimeError("get_closed_position_history: not connected to MTAPI")
        try:
            async with _get_session().get(
                f"{_base_url}/HistoryPositions",
                params={"id": _conn_id, "tickets": int(position_id)},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json(content_type=None)
                if attempt == 0 and isinstance(data, dict) and "stackTrace" in data:
                    _invalidate_connection()
                    continue
                if resp.status >= 400:
                    message = data.get("message", f"HTTP {resp.status}") if isinstance(data, dict) else f"HTTP {resp.status}"
                    raise RuntimeError(f"MTAPI HistoryPositions error: {message}")
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
                    ticket = record.get("ticket", record.get("ticketNumber", record.get("positionTicket")))
                    if ticket is not None and str(ticket) == wanted:
                        return record
                return None
        except RuntimeError:
            raise
        except Exception as exc:
            if attempt == 0:
                _invalidate_connection()
                continue
            raise RuntimeError(f"get_closed_position_history failed: {exc}") from exc
    raise RuntimeError("get_closed_position_history: failed after reconnect attempt")


async def get_deals_by_time_range(
    start_time: datetime,
    end_time: datetime,
) -> list[dict]:
    """Return normalized account deals for a UTC time range.

    MTAPI's real history endpoint is ``GET /OrderHistory?id=...&from=...&to=...``.
    Keep the RPC-shaped compatibility seam for tests/adapters, but never
    silently return an empty history on the production path.
    """
    legacy_connection = globals().get("_connection")
    get_deals = getattr(legacy_connection, "get_deals_by_time_range", None)
    if get_deals is not None:
        try:
            raw = await get_deals(start_time=start_time, end_time=end_time)
        except TypeError:
            raw = await get_deals(start_time, end_time)
        if isinstance(raw, dict):
            raw = raw.get("deals") or raw.get("items") or raw.get("data") or []
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    from_str = start_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    to_str = end_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for attempt in range(2):
        if not _conn_id and not await ensure_connected():
            raise RuntimeError("get_deals_by_time_range: not connected to MTAPI")
        try:
            async with _get_session().get(
                f"{_base_url}/OrderHistory",
                params={"id": _conn_id, "from": from_str, "to": to_str},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json(content_type=None)
                if attempt == 0 and isinstance(data, dict) and "stackTrace" in data:
                    _invalidate_connection()
                    continue
                if resp.status >= 400:
                    message = (
                        data.get("message", f"HTTP {resp.status}")
                        if isinstance(data, dict)
                        else f"HTTP {resp.status}"
                    )
                    raise RuntimeError(f"MTAPI OrderHistory error: {message}")

                if isinstance(data, list):
                    rows = data
                elif isinstance(data, dict):
                    rows = (
                        data.get("orders")
                        or data.get("deals")
                        or data.get("items")
                        or data.get("history")
                        or data.get("data")
                        or []
                    )
                else:
                    rows = []
                if isinstance(rows, dict):
                    rows = (
                        rows.get("orders")
                        or rows.get("deals")
                        or rows.get("items")
                        or rows.get("data")
                        or []
                    )
                return [row for row in rows if isinstance(row, dict)]
        except RuntimeError:
            raise
        except Exception as exc:
            if attempt == 0:
                _invalidate_connection()
                continue
            raise RuntimeError(f"get_deals_by_time_range failed: {exc}") from exc
    raise RuntimeError("get_deals_by_time_range: failed after reconnect attempt")


def _parse_open_positions_response(
    data: object, status: int, known_positions: Optional[Dict[str, dict]] = None
) -> Tuple[List[dict], List[str]]:
    if status < 200 or status >= 300:
        message = data.get("message", f"HTTP {status}") if isinstance(data, dict) else f"HTTP {status}"
        raise RuntimeError(f"MTAPI OpenedOrders error (HTTP {status}): {message}")
    if isinstance(data, list):
        return _dedupe_positions(data, known_positions)
    message = data.get("message", "unexpected object response") if isinstance(data, dict) else f"unexpected response type: {type(data).__name__}"
    raise RuntimeError(f"MTAPI OpenedOrders error: {message}")


_MAX_SANE_VOLUME_LOTS = 100.0


def _finite_positive(value: object) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number if number > 0 else None


def _position_lots(row: dict) -> Optional[float]:
    lots = _finite_positive(row.get("lots"))
    if lots is not None:
        return lots
    volume = _finite_positive(row.get("volume"))
    if volume is None or volume > _MAX_SANE_VOLUME_LOTS:
        return None
    return volume


def _position_direction(row: dict) -> str:
    raw = row.get("type", row.get("orderType"))
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
) -> Tuple[List[dict], List[str]]:
    """Collapse duplicate rows and return (safe rows, dropped ticket ids)."""
    known_positions = known_positions or {}
    by_ticket: dict[object, List[dict]] = {}
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
            if lots is None:
                known = known_positions.get(str(ticket))
                known_volume = _finite_positive(known.get("volume")) if known else None
                if known and known_volume is not None:
                    repaired = dict(row)
                    repaired["volume"] = known_volume
                    repaired["lots"] = known_volume
                    repaired["type"] = known["direction"]
                    result.append(repaired)
                else:
                    dropped_unknown.append(str(ticket))
            else:
                result.append(row)
            continue
        sane = [row for row in group if _position_lots(row) is not None]
        result.append(sane[0] if sane else group[0])
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
    return {
        "id": str(pos.get("ticket", pos.get("identifier", ""))),
        "ticket": pos.get("ticket", pos.get("identifier", 0)),
        "symbol": pos.get("symbol", ""),
        "type": _position_direction(pos),
        "volume": _position_lots(pos) or 0.0,
        "open_price": float(pos.get("openPrice", pos.get("price_open", 0.0))),
        "sl": float(pos.get("stopLoss", pos.get("sl", 0.0))),
        "tp": float(pos.get("takeProfit", pos.get("tp", 0.0))),
        "profit": float(pos.get("profit", 0.0)),
        "open_time": pos.get("openTime", pos.get("time", 0)),
        "comment": pos.get("comment", ""),
    }


async def get_current_quote(symbol: str) -> dict:
    for attempt in range(2):
        if not _conn_id and not await ensure_connected():
            return {}
        try:
            async with _get_session().get(
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
                    _invalidate_connection()
                    continue
                return {}
        except Exception as exc:
            if attempt == 0:
                _invalidate_connection()
                continue
            log.warning(f"get_current_quote error after reconnect: {exc}")
            return {}
    return {}


async def get_symbol_params(symbol: str) -> dict:
    """Fetch broker-provided symbol trading constraints from MTAPI.

    MTAPI's ``/SymbolParams`` response contains ``symbolInfo`` (digits,
    point/tick size) and ``symbolGroup``.  The latter exposes the broker's
    minimum SL/TP distances as ``sl`` and ``tp`` points on current MTAPI
    versions; newer versions may also expose explicit stops/freeze-level
    fields.  Keep the raw response so callers can handle both shapes without
    inventing broker defaults.
    """
    for attempt in range(2):
        if not _conn_id and not await ensure_connected():
            return {}
        try:
            async with _get_session().get(
                f"{_base_url}/SymbolParams",
                params={"id": _conn_id, "symbol": symbol},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                data = await resp.json(content_type=None)
                if (
                    resp.status < 400
                    and isinstance(data, dict)
                    and not _is_error_quote(data)
                ):
                    return data
                if attempt == 0:
                    _invalidate_connection()
                    continue
                return {}
        except Exception as exc:
            if attempt == 0:
                _invalidate_connection()
                continue
            log.warning(f"get_symbol_params error after reconnect: {exc}")
            return {}
    return {}


def _is_error_quote(data: dict) -> bool:
    return "code" in data and "stackTrace" in data


def get_connection() -> Optional[str]:
    """Return the MTAPI base URL when connected."""
    return _base_url if _connected else None


def get_conn_id() -> Optional[str]:
    """Return the active MTAPI session token."""
    return _conn_id if _connected else None


def is_connected() -> bool:
    return _connected
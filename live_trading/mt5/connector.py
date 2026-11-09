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
    GET  /PriceHistory     – OHLCV candles (ISO datetime range)
    GET  /PriceHistoryV2   – compatibility fallback for older bridges
    GET  /GetQuote         – current bid/ask price
    GET  /SymbolList       – verify the configured instrument is available
"""

import asyncio
import errno
import json
import math
import random
import threading
import re
import time as _time
from collections import deque
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, quote_plus

import aiohttp

from live_trading.config import (
    MTAPI_URL, MT5_HOST, MT5_PORT,
    MT5_USER, MT5_PASSWORD,
    SYNC_TIMEOUT, XAUUSD_TICK_SIZE,
    MT5_CONNECT_RETRY_JITTER,
)
from live_trading.mt5.volume_units import (
    configured_mtapi_volume_scale,
    extended_volume_to_lots,
    volume_constraint_sanity_error,
)
from live_trading.signals.gold_engine import OHLCV
from live_trading.logger import get_logger
from live_trading.utils.broker_time import observe_broker_server_time

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

_CONNECT_READY_POLL_INITIAL_S: float = 2.0
_CONNECT_READY_POLL_MAX_S: float = 15.0
_CONNECT_READY_TIMEOUT_S: float = 150.0

_reconnect_lock: asyncio.Lock | None = None
_connect_retry_lock: asyncio.Lock | None = None
_ensure_connected_thread_lock = threading.Lock()
_connect_retry_attempt = 0
_connect_retry_service_unavailable = False
_connection_generation = 0
_watchdog_task: asyncio.Task | None = None
_MT5_KEEPALIVE_TASK: asyncio.Task | None = None
_MT5_KEEPALIVE_INTERVAL_S: float = 180.0
_MT5_SESSION_REFRESH_AGE_S: float = 14400.0
_CANDLE_FETCH_CONNECT_TIMEOUT_S: float = 30.0
_CANDLE_FETCH_HTTP_TIMEOUT_S: float = 60.0
_CONNECTION_EVENT_LIMIT = 50
_connection_events: deque[dict] = deque(maxlen=_CONNECTION_EVENT_LIMIT)
_connection_health: dict = {
    "last_success_utc": None,
    "last_disconnect_utc": None,
    "connection_status": "disconnected",
    "consecutive_failures": 0,
    "total_reconnects": 0,
    "last_error_class": None,
    "last_error_text": None,
}
_connection_history_loaded = False
_SYMBOL_PARAMS_RETRY_ATTEMPTS = 3
_SYMBOL_PARAMS_RETRY_DELAY_S = 0.5
_SYMBOL_TICK_SIZE_CACHE: dict[str, float] = {}


def _redact_connection_text(value: object, *, limit: int = 200) -> str:
    """Remove credentials, broker URLs, and account/session fields from diagnostics."""
    text = str(value or "")
    for secret in (MT5_PASSWORD, _conn_id, MT5_USER):
        secret = str(secret or "").strip()
        if secret:
            text = text.replace(secret, "[REDACTED]")
            for encoded in {quote(secret, safe=""), quote_plus(secret, safe="")}:
                if encoded:
                    text = text.replace(encoded, "[REDACTED]")
    text = re.sub(
        r"""(?i)(["']?(?:password|passwd|token|conn[_-]?id|account(?:[_ -]?number)?|login|user|id)["']?\s*[:=]\s*["']?)[^&,\s"'}]+""",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)https?://\S+", "[URL REDACTED]", text)
    return text[:limit]


def _classify_connection_error(
    error: object | None = None,
    message: object | None = None,
    status: int | None = None,
) -> str:
    """Map MTAPI response and transport failures to stable operator categories."""
    parts = [str(value) for value in (message, error) if value is not None]
    text = " ".join(parts).casefold()
    if status is None:
        match = re.search(r"\bhttp(?:/\d(?:\.\d)?)?\s+(\d{3})\b", text)
        if match:
            status = int(match.group(1))

    if "trial expired" in text:
        return "TRIAL_EXPIRED"
    if "service_not_available" in text or "service not available" in text:
        return "BROKER_UNAVAILABLE"
    if any(
        phrase in text
        for phrase in (
            "invalid account",
            "account is invalid",
            "invalid password",
            "password is invalid",
            "wrong password",
            "incorrect password",
            "authentication failed",
            "unauthorized",
            "bad credentials",
        )
    ) or status in {401, 403}:
        return "AUTH_ERROR"

    current = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ConnectionRefusedError) or getattr(
            current, "errno", None
        ) == errno.ECONNREFUSED:
            return "BRIDGE_DOWN"
        if isinstance(current, (asyncio.TimeoutError, TimeoutError)):
            return "BROKER_UNAVAILABLE"
        current = (
            getattr(current, "os_error", None)
            or getattr(current, "__cause__", None)
            or getattr(current, "__context__", None)
        )

    if status in {502, 503, 504} or any(
        phrase in text for phrase in ("connection refused", "econnrefused")
    ):
        return "BRIDGE_DOWN"
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)) or any(
        phrase in text
        for phrase in (
            "timeout",
            "timed out",
            "timedout",
            "connect timeout",
            "connection timeout",
        )
    ):
        return "BROKER_UNAVAILABLE"
    return "UNKNOWN"


_RECONNECT_BACKOFF_SECONDS = (3, 6, 12, 30, 60, 120)


def _retry_backoff_delay(
    classification: str,
    attempt: int,
    *,
    message: object | None = None,
    service_unavailable_started: bool = False,
) -> float:
    """Return the bounded reconnect schedule with positive-only jitter."""
    error_text = str(message or "").casefold().replace(" ", "_")
    starts_at_thirty = (
        service_unavailable_started or "service_not_available" in error_text
    )
    index = max(0, int(attempt) - 1) + (3 if starts_at_thirty else 0)
    nominal = _RECONNECT_BACKOFF_SECONDS[min(index, len(_RECONNECT_BACKOFF_SECONDS) - 1)]
    configured_jitter = float(MT5_CONNECT_RETRY_JITTER)
    jitter = (
        min(max(configured_jitter, 0.0), 0.2)
        if math.isfinite(configured_jitter)
        else 0.2
    )
    if jitter == 0.0 or nominal >= _RECONNECT_BACKOFF_SECONDS[-1]:
        return float(nominal)
    return random.uniform(float(nominal), min(120.0, nominal * (1 + jitter)))

def _load_connection_history() -> None:
    """Restore health counters and events once Redis is reachable."""
    global _connection_history_loaded
    if _connection_history_loaded:
        return
    try:
        from live_trading.redis_ipc import (
            redis_available,
            redis_read_connection_events,
        )
        if not redis_available():
            return
        saved = redis_read_connection_events()
    except Exception:
        return

    _connection_history_loaded = True
    if not isinstance(saved, dict):
        return

    counters = saved.get("counters")
    if isinstance(counters, dict):
        _connection_health["last_success_utc"] = counters.get("last_success_utc")
        _connection_health["last_disconnect_utc"] = counters.get("last_disconnect_utc")
        saved_status = str(counters.get("connection_status", "disconnected")).lower()
        if saved_status == "connected":
            _connection_health["last_disconnect_utc"] = datetime.now(timezone.utc).isoformat()
        _connection_health["connection_status"] = "disconnected"
        for key in ("consecutive_failures", "total_reconnects"):
            try:
                _connection_health[key] = max(0, int(counters.get(key, 0)))
            except (TypeError, ValueError):
                _connection_health[key] = 0
        error_class = counters.get("last_error_class")
        _connection_health["last_error_class"] = (
            _redact_connection_text(error_class, limit=80) if error_class else None
        )
        error_text = counters.get("last_error_text")
        _connection_health["last_error_text"] = (
            _redact_connection_text(error_text) if error_text else None
        )

    events = saved.get("events")
    if isinstance(events, list):
        _connection_events.clear()
        for event in events[-_CONNECTION_EVENT_LIMIT:]:
            if not isinstance(event, dict):
                continue
            restored = {
                "timestamp_utc": str(event.get("timestamp_utc", ""))[:40],
                "event": _redact_connection_text(event.get("event", "unknown"), limit=80),
            }
            if event.get("source"):
                restored["source"] = _redact_connection_text(event["source"], limit=80)
            if event.get("error_class"):
                restored["error_class"] = _redact_connection_text(
                    event["error_class"], limit=80
                )
            if event.get("error_text"):
                restored["error_text"] = _redact_connection_text(event["error_text"])
            if event.get("classification"):
                restored["classification"] = _redact_connection_text(
                    event["classification"], limit=40
                )
            _connection_events.append(restored)


def _persist_connection_history() -> None:
    try:
        from live_trading.redis_ipc import redis_write_connection_events
        redis_write_connection_events({
            "counters": dict(_connection_health),
            "events": list(_connection_events),
        })
    except Exception:
        # Connection telemetry must never interrupt broker or trading work.
        pass


def _record_connection_event(
    event: str,
    *,
    source: str | None = None,
    error: BaseException | None = None,
    message: object | None = None,
    status: int | None = None,
    classification: str | None = None,
    failure: bool = False,
) -> None:
    """Append a redacted event and persist the bounded health snapshot."""
    _load_connection_history()
    now = datetime.now(timezone.utc).isoformat()
    event_record: dict = {
        "timestamp_utc": now,
        "event": _redact_connection_text(event, limit=80),
    }
    if source:
        event_record["source"] = _redact_connection_text(source, limit=80)

    if event == "connect_success":
        if _connection_health["last_success_utc"] is not None:
            _connection_health["total_reconnects"] += 1
        _connection_health["last_success_utc"] = now
        _connection_health["connection_status"] = "connected"
        _connection_health["consecutive_failures"] = 0

    outage_events = {
        "connect_failure",
        "connection_invalidated",
        "connection_status_not_alive",
        "connection_status_error",
        "watchdog_status_check_error",
        "watchdog_reconnect_failure",
    }
    if event in outage_events:
        if _connection_health.get("connection_status") == "connected":
            _connection_health["last_disconnect_utc"] = now
        _connection_health["connection_status"] = "disconnected"

    if failure:
        error_class = type(error).__name__ if error is not None else "ConnectionError"
        error_text = _redact_connection_text(
            message if message is not None else (error or event)
        )
        _connection_health["consecutive_failures"] += 1
        _connection_health["last_error_class"] = _redact_connection_text(
            error_class, limit=80
        )
        _connection_health["last_error_text"] = error_text
        event_record["error_class"] = _connection_health["last_error_class"]
        event_record["error_text"] = error_text
        classified = classification or _classify_connection_error(
            error=error,
            message=message if message is not None else error_text,
            status=status,
        )
        event_record["classification"] = classified
        if classified == "AUTH_ERROR":
            log.error(
                "MTAPI %s failed with AUTH_ERROR; automatic retries continue "
                "with bounded backoff. Verify the broker credentials.",
                source or event,
            )
        elif classified == "TRIAL_EXPIRED":
            log.error(
                "MTAPI %s failed with TRIAL_EXPIRED; automatic retries continue "
                "with bounded backoff. Verify the broker trial status.",
                source or event,
            )

    _connection_events.append(event_record)
    _persist_connection_history()


def get_connection_health() -> dict:
    """Return a copy of counters and the most recent redacted connection events."""
    _load_connection_history()
    return {
        **_connection_health,
        "events": [dict(event) for event in _connection_events],
    }


def _record_data_call_failure(
    source: str,
    *,
    error: BaseException | None = None,
    message: object | None = None,
    status: int | None = None,
) -> None:
    """Record an endpoint failure with its normalized connection classification."""
    _record_connection_event(
        "data_call_failure",
        source=source,
        error=error,
        message=message,
        status=status,
        failure=True,
    )


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


def _get_connect_retry_lock() -> asyncio.Lock:
    """Ensure concurrent callers share one unbounded reconnect loop."""
    global _connect_retry_lock
    if _connect_retry_lock is None:
        _connect_retry_lock = asyncio.Lock()
    return _connect_retry_lock


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


def _field_value(data: object, *names: str) -> object:
    """Read a mapping field without depending on casing or underscore style."""
    if not isinstance(data, dict):
        return None
    normalized = {
        str(key).replace("_", "").casefold(): value
        for key, value in data.items()
    }
    for name in names:
        value = normalized.get(name.replace("_", "").casefold())
        if value is not None and value != "":
            return value
    return None


def _parse_broker_server_time(
    value: object,
) -> tuple[Optional[datetime], Optional[datetime]]:
    """Return the broker clock as UTC and as its original server-wall time."""
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None, None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            parsed_utc = _parse_candle_time(value)
            if parsed_utc is None:
                return None, None
            return parsed_utc, parsed_utc.replace(tzinfo=None)

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc), parsed
    return parsed.astimezone(timezone.utc), parsed.replace(tzinfo=None)


def _account_field(info: object, key: str, default: object = None) -> object:
    """Read a field from either a dict or a simple adapter object."""
    if isinstance(info, dict):
        return info.get(key, default)
    return getattr(info, key, default)


def _h1_request_minutes(count: int) -> int:
    active_bars = max(count + 10, math.ceil(count * 7 / 5))
    return active_bars * 60 + (3 * 24 * 60)


def _candle_history_minutes(timeframe_minutes: int, count: int) -> int:
    """Size history ranges for calibration, bar polling, and indicator windows."""
    if count >= 500:
        return 5 * 24 * 60
    if count <= 3:
        # get_last_completed_bar_time polls this small window every loop tick.
        return 2 * 60
    if timeframe_minutes == 60:
        return _h1_request_minutes(count)
    return timeframe_minutes * (count + 5)


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
    close_cutoff = now - timedelta(seconds=2)
    return [
        candle
        for candle_time, candle in ordered
        if candle_time + timedelta(minutes=timeframe_minutes) <= close_cutoff
    ]


def _redact_candle_diagnostic(value: object, *, limit: int = 500) -> str:
    """Bound diagnostic text and remove credentials/session IDs before logging."""
    text = str(value)
    for secret in (MT5_PASSWORD, _conn_id):
        if not secret:
            continue
        for encoded in {secret, quote(secret, safe=""), quote_plus(secret, safe="")}:
            if encoded:
                text = text.replace(encoded, "[REDACTED]")
    return text[:limit]


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
    timeout_s: float = _CONNECT_READY_TIMEOUT_S,
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
                if resp.status == 200 and _connection_status_is_alive(data):
                    return True
                _record_connection_event(
                    "connection_status_not_alive",
                    source="connectex_readiness",
                    error=ConnectionError("ConnectionStatus reported not alive"),
                    message=(
                        f"ConnectionStatus reported not alive (HTTP {resp.status}): "
                        f"{data!s}"
                    ),
                    status=resp.status,
                    failure=True,
                )
                classification = _classify_connection_error(
                    message=data,
                    status=resp.status,
                )
                if classification in {"AUTH_ERROR", "TRIAL_EXPIRED"}:
                    return False
                last_status = str(data)[:180] if resp.status < 500 else f"HTTP {resp.status}"
        except Exception as exc:
            _record_connection_event(
                "connection_status_error",
                source="connectex_readiness",
                error=exc,
                failure=True,
            )
            last_status = str(exc)[:180]
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            log.error(
                "MT5 session was created but broker was not ready within "
                f"{timeout_s:.0f}s (last status: "
                f"{_redact_connection_text(last_status, limit=180)})"
            )
            return False
        await asyncio.sleep(min(delay, remaining))
        delay = min(delay * 1.5, _CONNECT_READY_POLL_MAX_S)


async def _previous_connection_is_alive(
    session: aiohttp.ClientSession,
    base_url: str,
    conn_id: str,
) -> Optional[bool]:
    """Return True/False for confirmed status, or None when it is unknown."""
    try:
        async with session.get(
            f"{base_url}/ConnectionStatus",
            params={"id": conn_id},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status == 200:
                alive = _connection_status_is_alive(data)
                if alive:
                    _connection_health["connection_status"] = "connected"
                    _persist_connection_history()
                    return True
                _record_connection_event(
                    "connection_status_not_alive",
                    source="previous_session_validation",
                    error=ConnectionError("previous ConnectionStatus was not alive"),
                    message=f"Previous ConnectionStatus reported not alive (HTTP {resp.status}): {data!s}",
                    status=resp.status,
                    failure=True,
                )
                return False
            if resp.status in {404, 410}:
                _record_connection_event(
                    "connection_status_not_alive",
                    source="previous_session_validation",
                    error=ConnectionError("previous MTAPI session was not found"),
                    status=resp.status,
                    failure=True,
                )
                return False
            _record_connection_event(
                "connection_status_error",
                source="previous_session_validation",
                error=ConnectionError(f"ConnectionStatus returned HTTP {resp.status}"),
                status=resp.status,
                failure=True,
            )
            return None
    except Exception as exc:
        _record_connection_event(
            "connection_status_error",
            source="previous_session_validation",
            error=exc,
            failure=True,
        )
        return None


async def _connect_unlocked() -> bool:
    """Perform one MTAPI connection attempt.

    Callers must use ``connect()`` rather than invoking this helper directly.
    Keeping the HTTP handshake under the reconnect lock prevents a watchdog,
    retry loop, or manual reconnect from replacing the same broker session
    concurrently.
    """
    global _connected, _base_url, _conn_id, _last_connect_time, _connection_generation

    base = MTAPI_URL.rstrip("/") if MTAPI_URL else ""
    host = MT5_HOST
    user = MT5_USER.strip() if MT5_USER else ""
    password = MT5_PASSWORD.strip() if MT5_PASSWORD else ""
    if not base:
        log.error("MTAPI_URL is not set. Set it to the MTAPI REST base URL.")
        _record_connection_event(
            "connect_failure",
            error=ValueError("MTAPI_URL is not configured"),
            failure=True,
        )
        return False
    if not user or not password or not host:
        log.error("MT5_USER, MT5_PASSWORD, and MT5_HOST must be set for MTAPI /ConnectEx.")
        _record_connection_event(
            "connect_failure",
            error=ValueError("MT5 connection configuration is incomplete"),
            failure=True,
        )
        return False

    previous_conn_id = _conn_id
    previous_base_url = _base_url or base
    try:
        sess = _get_session()
        if previous_conn_id:
            previous_alive = await _previous_connection_is_alive(
                sess, previous_base_url, previous_conn_id
            )
            if previous_alive is True:
                _base_url = previous_base_url
                _connected = True
                _last_connect_time = _time.monotonic()
                _connection_health["connection_status"] = "connected"
                _persist_connection_history()
                log.info("MTAPI broker session is already active; reusing confirmed session")
                return True
            if previous_alive is None:
                _connected = False
                log.warning("MTAPI session status is unknown; skipping ConnectEx until it is confirmed")
                return False

            _connected = False
            try:
                async with sess.get(
                    f"{previous_base_url}/Disconnect",
                    params={"id": previous_conn_id},
                    timeout=aiohttp.ClientTimeout(total=10),
                ):
                    pass
            except Exception as exc:
                log.warning("Previous MTAPI session cleanup failed: %s", _redact_connection_text(exc))
            _conn_id = ""
            previous_conn_id = ""

        _base_url = base
        log.info("MTAPI /ConnectEx connection attempt (host=%s, port=%s)", host, MT5_PORT)
        async with sess.get(
            f"{base}/ConnectEx",
            params=_connect_params(user, password, host, MT5_PORT),
            timeout=aiohttp.ClientTimeout(total=SYNC_TIMEOUT),
        ) as resp:
            raw = await resp.text()
            log.debug(f"MTAPI /ConnectEx response ({resp.status}): [response received]")
            classification = _classify_connection_error(
                message=raw, status=resp.status
            )
            if resp.status != 200 or classification != "UNKNOWN":
                safe_body = _redact_connection_text(raw, limit=300)
                if classification not in {"AUTH_ERROR", "TRIAL_EXPIRED"}:
                    log.error(
                        "MTAPI /ConnectEx failed (status=%s, classification=%s): %s",
                        resp.status,
                        classification,
                        safe_body,
                    )
                _connected = False
                _record_connection_event(
                    "connect_failure",
                    error=ConnectionError(f"ConnectEx returned HTTP {resp.status}"),
                    message=f"ConnectEx returned HTTP {resp.status}: {raw}",
                    status=resp.status,
                    failure=True,
                )
                return False
            conn_id = raw.strip().strip('"')
            if not conn_id or len(conn_id) < 10:
                log.error(
                    "MTAPI /ConnectEx returned an unexpected session token "
                    "(response body redacted)"
                )
                _connected = False
                _record_connection_event(
                    "connect_failure",
                    error=ConnectionError("ConnectEx returned an invalid session token"),
                    failure=True,
                )
                return False
            if not await _wait_for_broker_ready(base, conn_id):
                readiness_classification = next(
                    (
                        event.get("classification", "UNKNOWN")
                        for event in reversed(_connection_events)
                        if event.get("source") == "connectex_readiness"
                        and event.get("classification")
                    ),
                    "UNKNOWN",
                )
                try:
                    async with sess.get(
                        f"{base}/Disconnect",
                        params={"id": conn_id},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ):
                        pass
                except Exception:
                    pass
                previous_alive = bool(
                    previous_conn_id
                    and await _previous_connection_is_alive(
                        sess, previous_base_url or base, previous_conn_id
                    )
                )
                if previous_alive:
                    _base_url = previous_base_url or base
                    _connected = True
                    _conn_id = previous_conn_id
                else:
                    _connected = False
                    _conn_id = ""
                _record_connection_event(
                    "connect_failure",
                    error=ConnectionError("broker session was not ready after ConnectEx"),
                    classification=readiness_classification,
                    failure=True,
                )
                return False
            _conn_id = conn_id
            _connected = True
            _last_connect_time = _time.monotonic()
            _connection_generation += 1
            _record_connection_event("connect_success", source="ConnectEx")
            log.info("MTAPI connection success (host=%s, port=%s)", host, MT5_PORT)
            return True
    except Exception as exc:
        log.error("MTAPI connection failure: %s", _redact_connection_text(exc))
        _connected = False
        _record_connection_event("connect_failure", source="ConnectEx", error=exc, failure=True)
        return False


async def connect(*args, **kwargs) -> bool:
    """Connect through MTAPI with one serialized broker handshake."""
    lock = _get_reconnect_lock()
    async with lock:
        return await _connect_unlocked()


async def disconnect() -> None:
    """Close the MTAPI connection and release the HTTP session."""
    global _connected, _session, _conn_id, _base_url
    lock = _get_reconnect_lock()
    async with lock:
        conn_id, base_url, session = _conn_id, _base_url, _session
        if conn_id:
            _record_connection_event("connection_invalidated", source="disconnect")
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
                log.warning("MT5 disconnect request failed: %s", _redact_connection_text(exc))
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
                if not _connection_status_is_alive(data):
                    _record_connection_event(
                        "connection_status_not_alive",
                        source="keepalive",
                        error=ConnectionError("ConnectionStatus reported not alive"),
                        message=(
                            f"ConnectionStatus reported not alive "
                            f"(HTTP {resp.status}): {data!s}"
                        ),
                        status=resp.status,
                        failure=True,
                    )
                else:
                    _record_connection_event(
                        "connection_status_error",
                        source="keepalive",
                        error=ConnectionError(f"ConnectionStatus returned HTTP {resp.status}"),
                        message=f"ConnectionStatus HTTP {resp.status}: {data!s}",
                        status=resp.status,
                        failure=True,
                    )
            return ok
    except Exception as exc:
        log.warning(f"MTAPI broker-session keepalive failed: {exc}")
        _record_connection_event(
            "connection_status_error", source="keepalive", error=exc, failure=True
        )
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
    """Retry forever with the bounded MTAPI reconnect schedule.

    Legacy arguments remain accepted for call-site compatibility but do not
    cap attempts or override the requested delay sequence.
    """
    global _connect_retry_attempt, _connect_retry_service_unavailable
    async with _get_connect_retry_lock():
        while True:
            _connect_retry_attempt += 1
            log.info("MT5 connect attempt %d", _connect_retry_attempt)
            if await connect():
                _connect_retry_attempt = 0
                _connect_retry_service_unavailable = False
                return True

            event = _connection_events[-1] if _connection_events else {}
            classification = str(event.get("classification", "UNKNOWN"))
            error_text = str(event.get("error_text", _connection_health.get("last_error_text") or ""))
            if "service_not_available" in error_text.casefold().replace(" ", "_"):
                _connect_retry_service_unavailable = True
            delay = _retry_backoff_delay(
                classification,
                _connect_retry_attempt,
                message=error_text,
                service_unavailable_started=_connect_retry_service_unavailable,
            )
            log.warning(
                "MT5 connection failed (%s, attempt %d); retrying in %.1f seconds.",
                classification,
                _connect_retry_attempt,
                delay,
            )
            await asyncio.sleep(delay)


def _invalidate_connection() -> None:
    global _connected
    log.warning("MT5 conn_id is unconfirmed — retaining it until status is checked")
    _record_connection_event(
        "connection_invalidated",
        source="invalidate",
        error=ConnectionError("MT5 connection was invalidated"),
        failure=True,
    )
    _connected = False


async def _acquire_thread_lock(lock: threading.Lock) -> None:
    """Acquire a threading lock without blocking the asyncio event loop."""
    while not lock.acquire(blocking=False):
        await asyncio.sleep(0.05)


async def ensure_connected(*args, **kwargs) -> bool:
    global _connected, _conn_id, _base_url
    await _acquire_thread_lock(_ensure_connected_thread_lock)
    try:
        if _conn_id and _base_url:
            previous_conn_id = _conn_id
            previous_base_url = _base_url
            alive = await _previous_connection_is_alive(
                _get_session(), previous_base_url, previous_conn_id
            )
            if alive is True:
                _connected = True
                return True
            _connected = False
            if alive is False:
                try:
                    async with _get_session().get(
                        f"{previous_base_url}/Disconnect",
                        params={"id": previous_conn_id},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ):
                        pass
                except Exception as exc:
                    log.warning(
                        "Stale MTAPI session cleanup failed: %s",
                        _redact_connection_text(exc),
                    )
                _conn_id = ""
                _base_url = ""
        return await connect_with_retry()
    finally:
        _ensure_connected_thread_lock.release()


async def start_connection_watchdog(interval_seconds: float = 30.0) -> None:
    global _watchdog_task
    log.info(f"[watchdog] MT5 connection watchdog started (interval={interval_seconds}s)")
    _record_connection_event("watchdog_started", source="watchdog")
    failures = 0
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            if not _connected or not _conn_id:
                _record_connection_event("watchdog_reconnect_attempt", source="watchdog")
                if await ensure_connected():
                    log.info("[watchdog] MT5 proactive reconnect succeeded")
                    _record_connection_event("watchdog_reconnect_success", source="watchdog")
                else:
                    _record_connection_event(
                        "watchdog_reconnect_failure",
                        source="watchdog",
                        error=ConnectionError("watchdog reconnect failed"),
                        failure=True,
                    )
                continue
            try:
                async with _get_session().get(
                    f"{_base_url}/ConnectionStatus",
                    params={"id": _conn_id},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status == 200 and _connection_status_is_alive(data):
                        failures = 0
                        _record_connection_event(
                            "watchdog_connection_status_alive", source="watchdog"
                        )
                    else:
                        failures += 1
                        _record_connection_event(
                            (
                                "connection_status_not_alive"
                                if not _connection_status_is_alive(data)
                                else "connection_status_error"
                            ),
                            source="watchdog",
                            error=ConnectionError("ConnectionStatus reported not alive"),
                            message=(
                                f"ConnectionStatus reported not alive "
                                f"(HTTP {resp.status}): {data!s}"
                            ),
                            status=resp.status,
                            failure=True,
                        )
            except Exception as exc:
                failures += 1
                log.warning("[watchdog] ConnectionStatus check failed (%s/1): %s", failures, _redact_connection_text(exc))
                _record_connection_event(
                    "watchdog_status_check_error",
                    source="watchdog",
                    error=exc,
                    failure=True,
                )
            if failures >= 1:
                failures = 0
                _invalidate_connection()
                _record_connection_event("watchdog_reconnect_attempt", source="watchdog")
                if await ensure_connected():
                    _record_connection_event("watchdog_reconnect_success", source="watchdog")
                else:
                    _record_connection_event(
                        "watchdog_reconnect_failure",
                        source="watchdog",
                        error=ConnectionError("watchdog reconnect failed"),
                        failure=True,
                    )
    except asyncio.CancelledError:
        log.info("[watchdog] MT5 connection watchdog stopped")
        _record_connection_event("watchdog_stopped", source="watchdog")
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


async def resolve_symbol_name(symbol: str) -> Optional[str]:
    """Resolve a configured name against MTAPI's broker-provided Symbols map."""
    target = symbol.strip().upper()
    if not target:
        return None
    for attempt in range(2):
        if not _conn_id and not await ensure_connected():
            return None
        try:
            async with _get_session().get(
                f"{_base_url}/Symbols",
                params={"id": _conn_id},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    failure_message = f"HTTP {resp.status}: {str(data)[:160]}"
                    raise RuntimeError(failure_message)
                if isinstance(data, dict) and _field_value(
                    data, "error", "exception", "stackTrace", "message"
                ) is not None:
                    raise RuntimeError(
                        f"MTAPI Symbols error: {str(data)[:160]}"
                    )
                if isinstance(data, list):
                    names = [str(item) for item in data]
                elif isinstance(data, dict):
                    names = [str(item) for item in data.keys()]
                else:
                    raise RuntimeError(f"unexpected response type {type(data).__name__}")
                exact = next((name for name in names if name.upper() == target), None)
                if exact is not None:
                    log.info(
                        "MTAPI broker symbol resolved configured=%s exact=%s",
                        symbol,
                        exact,
                    )
                    return exact
                suffix_matches = [
                    name for name in names if name.upper().startswith(target)
                ]
                if len(suffix_matches) == 1:
                    resolved = suffix_matches[0]
                    log.info(
                        "MTAPI broker symbol resolved configured=%s exact=%s "
                        "match=unique-suffix",
                        symbol,
                        resolved,
                    )
                    return resolved
                nearby = [name for name in names if target[:3] in name.upper()]
                log.error(
                    "MTAPI broker symbol is not uniquely available configured=%s "
                    "matches=%s",
                    symbol,
                    nearby[:12],
                )
                return None
        except Exception as exc:
            _record_connection_event(
                "data_call_failure",
                source="Symbols",
                error=exc,
                failure=True,
            )
            if attempt == 0:
                _invalidate_connection()
                continue
            log.error(f"MTAPI symbol check failed after reconnect: {exc}")
            return None
    return None


async def check_symbol_available(symbol: str) -> bool:
    return await resolve_symbol_name(symbol) is not None


async def fetch_candles(
    symbol: str, timeframe: str, count: int = 300
) -> List[OHLCV]:
    """Fetch the latest completed OHLCV candles via MTAPI.

    The hosted API expects an integer-minute timeframe and broker-server
    timestamps. Empty range results fall back to today's history and then the
    30-day history endpoint.
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
            # Preserve the historical adapter's larger warm-up request; the
            # bounded windows below apply to the hosted MTAPI REST contract.
            legacy_count = max(200, requested_count)
            request_minutes = (
                _h1_request_minutes(legacy_count)
                if tf == 60
                else tf * (legacy_count + 5)
            )
            raw_candles = await asyncio.wait_for(
                get_history(
                    symbol=symbol,
                    timeframe=tf_label,
                    start_time=now - timedelta(minutes=request_minutes),
                    limit=legacy_count + 5,
                ),
                timeout=_CANDLE_FETCH_HTTP_TIMEOUT_S,
            )
            parsed: list[tuple[datetime, OHLCV]] = []
            for row in raw_candles or []:
                if not isinstance(row, dict):
                    continue
                candle_time = _parse_time(
                    _field_value(row, "time", "openTime", "brokerTime", "timestamp")
                )
                if candle_time is None:
                    continue
                parsed.append((
                    candle_time,
                    OHLCV(
                        time=candle_time,
                        open=float(_field_value(row, "openPrice", "open", "o") or 0),
                        high=float(_field_value(row, "highPrice", "high", "h") or 0),
                        low=float(_field_value(row, "lowPrice", "low", "l") or 0),
                        close=float(_field_value(row, "closePrice", "close", "c") or 0),
                        volume=float(
                            _field_value(row, "tickVolume", "volume", "realVolume") or 0
                        ),
                    ),
                ))
            parsed.sort(key=lambda item: item[0])
            return [candle for _, candle in parsed[:-1]][-requested_count:]
        except Exception as exc:
            log.error(
                "fetch_candles legacy adapter failed "
                "(symbol=%s timeframe=%s count=%d exception=%s: %s)",
                symbol,
                timeframe,
                requested_count,
                type(exc).__name__,
                _redact_candle_diagnostic(exc),
            )
            return []
    for attempt in range(2):
        if not _conn_id:
            try:
                connected = await ensure_connected()
            except Exception as exc:
                _record_data_call_failure(
                    "PriceHistory_connect",
                    error=exc,
                    message="fetch_candles connection check failed",
                )
                log.error(
                    "fetch_candles connection check failed endpoint=%s "
                    "status=unavailable body='' exception=%s: %s "
                    "(symbol=%s timeframe=%s attempt=%d)",
                    f"{_base_url.rstrip('/')}/PriceHistory",
                    type(exc).__name__,
                    _redact_candle_diagnostic(exc),
                    symbol,
                    timeframe,
                    attempt + 1,
                )
                return []
            if not connected:
                _record_data_call_failure(
                    "PriceHistory_connect",
                    error=ConnectionError("No active MTAPI session"),
                    message="No active MTAPI session before PriceHistory request",
                )
                log.error(
                    "fetch_candles connection unavailable endpoint=%s "
                    "status=unavailable body='' exception=NoActiveSession "
                    "(symbol=%s timeframe=%s attempt=%d)",
                    f"{_base_url.rstrip('/')}/PriceHistory",
                    symbol,
                    timeframe,
                    attempt + 1,
                )
                return []
        tf_min = _TF_MAP.get(timeframe, 5)
        quote = await get_current_quote(symbol)
        server_time, server_wall_time = _parse_broker_server_time(
            quote.get("server_time_raw", quote.get("server_time"))
        )
        if server_time is None or server_wall_time is None:
            # Keep operating if an otherwise healthy quote omits its timestamp,
            # but make the clock source visible for diagnosis.
            server_time = datetime.now(timezone.utc)
            server_wall_time = server_time.replace(tzinfo=None)
            log.warning(
                "fetch_candles could not read broker server time; "
                "using local UTC clock (symbol=%s timeframe=%s)",
                symbol,
                timeframe,
            )

        request_minutes = _candle_history_minutes(tf_min, requested_count)
        from_wall_time = server_wall_time - timedelta(minutes=request_minutes)
        if from_wall_time >= server_wall_time:
            # PriceHistory requires a strictly increasing interval.
            from_wall_time = server_wall_time - timedelta(minutes=max(1, tf_min))
        from_str = from_wall_time.strftime("%Y-%m-%dT%H:%M:%S")
        to_str = server_wall_time.strftime("%Y-%m-%dT%H:%M:%S")
        range_params = {
            "id": _conn_id,
            "symbol": symbol,
            "from": from_str,
            "to": to_str,
            "timeFrame": tf_min,
        }
        today_params = {
            "id": _conn_id,
            "symbol": symbol,
            "timeFrame": tf_min,
        }
        month_params = {
            "id": _conn_id,
            "symbol": symbol,
            "year": server_wall_time.year,
            "month": server_wall_time.month,
            "day": server_wall_time.day,
            "timeFrame": tf_min,
        }
        endpoint_specs = (
            ("/PriceHistory", range_params),
            ("/PriceHistoryV2", range_params),
            ("/PriceHistoryToday", today_params),
            ("/PriceHistoryMonth", month_params),
        )
        retry_after_connection_error = False
        try_legacy_endpoint = False
        for endpoint_path, request_params in endpoint_specs:
            if endpoint_path == "/PriceHistoryV2" and not try_legacy_endpoint:
                continue
            endpoint = f"{_base_url.rstrip('/')}{endpoint_path}"
            safe_params = {
                key: value for key, value in request_params.items() if key != "id"
            }
            log.info(
                "fetch_candles request endpoint=%s params=%s",
                endpoint,
                safe_params,
            )
            status: Optional[int] = None
            raw_body = ""
            try:
                async with _get_session().get(
                    endpoint,
                    params=request_params,
                    timeout=aiohttp.ClientTimeout(total=_CANDLE_FETCH_HTTP_TIMEOUT_S),
                ) as resp:
                    status = resp.status
                    raw_body = await resp.text()
            except Exception as exc:
                _record_data_call_failure(
                    "PriceHistory",
                    error=exc,
                    message=f"PriceHistory request failed: {raw_body}",
                    status=status,
                )
                log.error(
                    "fetch_candles request failed endpoint=%s status=%s params=%s "
                    "body=%s exception=%s: %s (attempt=%d)",
                    endpoint,
                    status if status is not None else "unavailable",
                    safe_params,
                    _redact_candle_diagnostic(raw_body, limit=300),
                    type(exc).__name__,
                    _redact_candle_diagnostic(exc),
                    attempt + 1,
                )
                if attempt == 0:
                    _invalidate_connection()
                    retry_after_connection_error = True
                    break
                return []

            log.info(
                "fetch_candles response endpoint=%s status=%s params=%s body=%s",
                endpoint,
                status,
                safe_params,
                _redact_candle_diagnostic(raw_body, limit=300),
            )

            if status != 200:
                _record_data_call_failure(
                    endpoint_path.lstrip("/"),
                    message=f"HTTP {status}: {raw_body}",
                    status=status,
                )
                if endpoint_path == "/PriceHistory" and status in {404, 405}:
                    try_legacy_endpoint = True
                    log.warning(
                        "fetch_candles range endpoint unavailable; trying "
                        "compatibility/fallback endpoint=%s status=%d params=%s "
                        "body=%s",
                        endpoint,
                        status,
                        safe_params,
                        _redact_candle_diagnostic(raw_body, limit=300),
                    )
                else:
                    log.error(
                        "fetch_candles HTTP failure endpoint=%s status=%d "
                        "params=%s body=%s exception=HTTP %d (attempt=%d)",
                        endpoint,
                        status,
                        safe_params,
                        _redact_candle_diagnostic(raw_body, limit=300),
                        status,
                        attempt + 1,
                    )
                continue

            try:
                data = json.loads(raw_body)
            except (TypeError, ValueError) as exc:
                _record_data_call_failure(
                    endpoint_path.lstrip("/"),
                    error=exc,
                    message=f"Invalid JSON response: {raw_body}",
                    status=status,
                )
                log.error(
                    "fetch_candles invalid JSON endpoint=%s status=%d params=%s "
                    "body=%s exception=%s: %s (attempt=%d)",
                    endpoint,
                    status,
                    safe_params,
                    _redact_candle_diagnostic(raw_body, limit=300),
                    type(exc).__name__,
                    _redact_candle_diagnostic(exc),
                    attempt + 1,
                )
                continue

            if not isinstance(data, list):
                _record_data_call_failure(
                    endpoint_path.lstrip("/"),
                    error=TypeError(f"Unexpected payload type {type(data).__name__}"),
                    message=f"Unexpected payload: {raw_body}",
                    status=status,
                )
                log.error(
                    "fetch_candles returned non-list payload endpoint=%s "
                    "status=%d params=%s body=%s "
                    "exception=UnexpectedPayloadType:%s (attempt=%d)",
                    endpoint,
                    status,
                    safe_params,
                    _redact_candle_diagnostic(raw_body, limit=300),
                    type(data).__name__,
                    attempt + 1,
                )
                continue

            candles_with_times: List[Tuple[datetime, OHLCV]] = []
            skipped_rows = 0
            for bar in data:
                if not isinstance(bar, dict):
                    skipped_rows += 1
                    continue
                candle_time = _parse_candle_time(
                    _field_value(
                        bar, "time", "openTime", "brokerTime", "timestamp"
                    )
                )
                if candle_time is None:
                    skipped_rows += 1
                    continue
                open_raw = _field_value(bar, "openPrice", "open", "o")
                high_raw = _field_value(bar, "highPrice", "high", "h")
                low_raw = _field_value(bar, "lowPrice", "low", "l")
                close_raw = _field_value(bar, "closePrice", "close", "c")
                try:
                    if any(
                        value is None
                        for value in (open_raw, high_raw, low_raw, close_raw)
                    ):
                        raise ValueError("missing OHLC field")
                    open_value, high_value, low_value, close_value = (
                        float(value)
                        for value in (open_raw, high_raw, low_raw, close_raw)
                    )
                    if not all(
                        math.isfinite(value)
                        for value in (
                            open_value,
                            high_value,
                            low_value,
                            close_value,
                        )
                    ):
                        raise ValueError("non-finite OHLC field")
                    volume_raw = _field_value(
                        bar, "tickVolume", "volume", "realVolume"
                    )
                    volume_value = float(volume_raw or 0)
                except (TypeError, ValueError, OverflowError):
                    skipped_rows += 1
                    continue
                candle = OHLCV(
                    time=candle_time.isoformat().replace("+00:00", "Z"),
                    open=open_value,
                    high=high_value,
                    low=low_value,
                    close=close_value,
                    volume=volume_value,
                )
                candles_with_times.append((candle_time, candle))

            if skipped_rows:
                log.warning(
                    "fetch_candles skipped invalid history rows endpoint=%s "
                    "status=%d skipped=%d total=%d (symbol=%s timeframe=%s)",
                    endpoint,
                    status,
                    skipped_rows,
                    len(data),
                    symbol,
                    timeframe,
                )
            if not candles_with_times:
                log.warning(
                    "fetch_candles history endpoint returned no parseable rows; "
                    "trying fallback endpoint=%s status=%d params=%s "
                    "body=%s (symbol=%s timeframe=%s)",
                    endpoint,
                    status,
                    safe_params,
                    _redact_candle_diagnostic(raw_body, limit=300),
                    symbol,
                    timeframe,
                )
                continue

            candles = _completed_candles(
                candles_with_times,
                timeframe_minutes=tf_min,
                now=server_time,
            )
            if not candles:
                log.warning(
                    "fetch_candles history endpoint had no completed candles; "
                    "trying fallback endpoint=%s status=%d params=%s "
                    "body=%s (symbol=%s timeframe=%s)",
                    endpoint,
                    status,
                    safe_params,
                    _redact_candle_diagnostic(raw_body, limit=300),
                    symbol,
                    timeframe,
                )
                continue
            result = (
                candles[-requested_count:]
                if len(candles) > requested_count
                else candles
            )
            log.info(
                "fetch_candles history succeeded endpoint=%s bars=%d "
                "symbol=%s timeframe=%s",
                endpoint,
                len(result),
                symbol,
                timeframe,
            )
            return result

        if retry_after_connection_error:
            continue
        log.error(
            "fetch_candles returned no usable candles after history fallbacks "
            "(symbol=%s timeframe=%s count=%d)",
            symbol,
            timeframe,
            requested_count,
        )
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
                if resp.status >= 400 or not (
                    isinstance(data, dict) and "balance" in data
                ):
                    _record_data_call_failure(
                        "AccountSummary",
                        message=f"HTTP {resp.status}: {data!s}",
                        status=resp.status,
                    )
                if isinstance(data, dict) and "balance" in data:
                    return {
                        "balance": float(data.get("balance", 0.0)),
                        "equity": float(data.get("equity", 0.0)),
                        "margin": float(data.get("margin", 0.0)),
                        "freeMargin": float(data.get("freeMargin", 0.0)),
                        "marginLevel": float(data.get("marginLevel", 0.0)),
                        # Do not invent USD when the broker omits account
                        # currency; risk sizing must fail closed if it cannot
                        # match tick economics to the account denomination.
                        "currency": str(data.get("currency") or "").strip().upper(),
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
            _record_data_call_failure("AccountSummary", error=exc)
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
        if not await ensure_connected():
            raise RuntimeError("get_open_positions: MTAPI connection is not confirmed")
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
                    _record_data_call_failure(
                        "OpenedOrders",
                        message=f"API error response: {data!s}",
                        status=resp.status,
                    )
                    _invalidate_connection()
                    continue
                positions, dropped = _parse_open_positions_response(
                    data, resp.status, known_positions
                )
                return (positions, dropped) if return_diagnostics else positions
        except RuntimeError as exc:
            _record_data_call_failure("OpenedOrders", error=exc)
            raise
        except Exception as exc:
            _record_data_call_failure("OpenedOrders", error=exc)
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
                    _record_data_call_failure(
                        "HistoryPositions",
                        message=f"API error response: {data!s}",
                        status=resp.status,
                    )
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
        except RuntimeError as exc:
            _record_data_call_failure("HistoryPositions", error=exc)
            raise
        except Exception as exc:
            _record_data_call_failure("HistoryPositions", error=exc)
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
                    _record_data_call_failure(
                        "OrderHistory",
                        message=f"API error response: {data!s}",
                        status=resp.status,
                    )
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
        except RuntimeError as exc:
            _record_data_call_failure("OrderHistory", error=exc)
            raise
        except Exception as exc:
            _record_data_call_failure("OrderHistory", error=exc)
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
    def _finite_or_zero(*keys: str) -> float:
        for key in keys:
            value = pos.get(key)
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                return number
        return 0.0

    return {
        "id": str(pos.get("ticket", pos.get("identifier", ""))),
        "ticket": pos.get("ticket", pos.get("identifier", 0)),
        "symbol": pos.get("symbol", ""),
        "type": _position_direction(pos),
        "volume": _position_lots(pos) or 0.0,
        "open_price": _finite_or_zero("openPrice", "price_open"),
        "sl": _finite_or_zero("stopLoss", "sl"),
        "tp": _finite_or_zero("takeProfit", "tp"),
        "profit": _finite_or_zero("profit"),
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
                failure_recorded = resp.status >= 400
                if failure_recorded:
                    _record_data_call_failure(
                        "GetQuote",
                        message=f"HTTP {resp.status}: {data!s}",
                        status=resp.status,
                    )
                if isinstance(data, dict) and not _is_error_quote(data):
                    bid = _field_value(data, "bid")
                    ask = _field_value(data, "ask")
                    if bid is not None and ask is not None:
                        server_time_raw = _field_value(
                            data, "time", "serverTime", "timeCurrent", "brokerTime"
                        )
                        server_time, server_wall_time = _parse_broker_server_time(
                            server_time_raw
                        )
                        observe_broker_server_time(server_wall_time)
                        return {
                            "bid": float(bid),
                            "ask": float(ask),
                            "server_time": server_time,
                            "server_time_raw": server_time_raw,
                        }
                if not failure_recorded:
                    _record_data_call_failure(
                        "GetQuote",
                        message=f"Invalid quote response (HTTP {resp.status}): {data!s}",
                        status=resp.status,
                    )
                if attempt == 0:
                    _invalidate_connection()
                    continue
                return {}
        except Exception as exc:
            _record_data_call_failure("GetQuote", error=exc)
            if attempt == 0:
                _invalidate_connection()
                continue
            log.warning(
                "get_current_quote error after reconnect: %s",
                _redact_candle_diagnostic(exc),
            )
            return {}
    return {}


def _positive_symbol_number(value: object) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _symbol_params_payload(data: object, symbol: str) -> Optional[dict]:
    """Unwrap the documented object plus common API envelope/list variants."""
    if isinstance(data, list):
        rows = [row for row in data if isinstance(row, dict)]
        wanted = symbol.strip().casefold()
        matching = [
            row for row in rows
            if str(_field_value(row, "symbol", "name") or "").strip().casefold()
            == wanted
        ]
        if matching:
            data = matching[0]
        elif len(rows) == 1 and not _field_value(rows[0], "symbol", "name"):
            data = rows[0]
        else:
            return None
    if not isinstance(data, dict):
        return None

    # Some MTAPI bridges wrap their result; the official SymbolParams contract
    # is the unwrapped {symbol, symbolInfo, symbolGroup} object.
    current = data
    for _ in range(4):
        nested = _field_value(
            current, "data", "result", "symbolParams", "symbol_parameters",
            "payload",
        )
        if isinstance(nested, list):
            return _symbol_params_payload(nested, symbol)
        if not isinstance(nested, dict) or nested is current:
            break
        current = nested
    return current


_RAW_SYMBOL_DIAGNOSTIC_FIELDS = {
    "ticksize", "tradeticksize", "minpriceincrement", "pricestep",
    "tickvalue", "tradetickvalue", "tickvalueloss", "tradetickvalueloss",
    "tickvaluecurrency", "tradetickvaluecurrency", "contractsize",
    "tradecontractsize", "currencyprofit", "profitcurrency", "digits",
    "precision", "pricedigits", "decimalplaces", "points", "point",
    "pointsize", "pricepoint", "volumemin", "minvolume", "volumestep",
    "volumeincrement", "volumemax", "maxvolume",
}


def _raw_symbol_field_snapshot(root: dict) -> dict[str, dict]:
    """Retain original sizing fields before canonical fallbacks overwrite them."""
    info = _field_value(
        root, "symbolInfo", "symbol_info", "info", "symbolDetails",
        "symbol_details",
    )
    group = _field_value(root, "symbolGroup", "symbol_group", "group")
    sources = {"root": root, "symbolInfo": info, "symbolGroup": group}
    snapshot: dict[str, dict] = {}
    for section, fields in sources.items():
        if not isinstance(fields, dict):
            continue
        selected = {
            str(key): value
            for key, value in fields.items()
            if str(key).replace("_", "").casefold()
            in _RAW_SYMBOL_DIAGNOSTIC_FIELDS
        }
        if selected:
            snapshot[section] = selected
    return snapshot


def _canonical_symbol_fields(source: object) -> dict:
    """Copy broker aliases into stable MT5-style names without dropping fields."""
    if not isinstance(source, dict):
        return {}
    result = dict(source)
    aliases = {
        "tickSize": (
            "tickSize", "tick_size", "tradeTickSize", "trade_tick_size",
            "minPriceIncrement", "priceStep",
        ),
        "tickValue": (
            "tickValue", "tick_value", "tradeTickValue", "trade_tick_value",
            "tickValueLoss", "tick_value_loss", "tradeTickValueLoss",
        ),
        "tickValueCurrency": (
            "tickValueCurrency", "tick_value_currency",
            "tradeTickValueCurrency", "trade_tick_value_currency",
        ),
        "contractSize": (
            "contractSize", "contract_size", "tradeContractSize",
        ),
        "currencyProfit": (
            "currencyProfit", "currency_profit", "profitCurrency",
            "profit_currency",
        ),
        "digits": ("digits", "precision", "priceDigits", "decimalPlaces"),
        "points": ("points", "point", "pointSize", "pricePoint"),
        "volumeMin": ("volumeMin", "volume_min", "minVolume", "min_volume"),
        "volumeStep": (
            "volumeStep", "volume_step", "volumeIncrement", "volume_increment",
        ),
        "volumeMax": ("volumeMax", "volume_max", "maxVolume", "max_volume"),
    }
    for canonical, names in aliases.items():
        value = _field_value(source, *names)
        if value is not None:
            result[canonical] = value
    return result


_VOLUME_FIELD_ALIASES = {
    "volumeMin": ("volumeMin", "volume_min", "minVolume", "min_volume"),
    "volumeStep": (
        "volumeStep", "volume_step", "volumeIncrement", "volume_increment",
    ),
    "volumeMax": ("volumeMax", "volume_max", "maxVolume", "max_volume"),
}


def _convert_normalized_volume_fields(
    normalized: dict,
    source_maps: tuple[tuple[str, object, dict], ...],
    scale: float,
) -> Optional[str]:
    """Convert canonical SymbolParams volumes while retaining raw diagnostics."""
    errors: list[str] = []
    for section, source, target in source_maps:
        if not isinstance(source, dict):
            continue
        for canonical, aliases in _VOLUME_FIELD_ALIASES.items():
            raw_value = _field_value(source, *aliases)
            if raw_value is None:
                continue
            try:
                lots = extended_volume_to_lots(raw_value, scale)
            except ValueError as exc:
                reason = (
                    f"{section}.{canonical} raw={raw_value!r} scale={scale:g}: {exc}"
                )
                log.error("MTAPI volume conversion ERROR %s", reason)
                errors.append(reason)
                continue

            target[canonical] = lots
            log.info(
                "MTAPI_VOLUME_CONVERSION symbol=%s field=%s raw=%s "
                "scale=%s lots=%s source=%s",
                normalized.get("symbol", "unknown"),
                canonical,
                raw_value,
                f"{scale:g}",
                f"{lots:g}",
                section,
            )

    if errors:
        return "; ".join(errors)

    values: dict[str, Optional[float]] = {
        "volumeMin": None,
        "volumeStep": None,
        "volumeMax": None,
    }
    for canonical in values:
        for mapping in (
            normalized.get("symbolInfo"),
            normalized,
            normalized.get("symbolGroup"),
        ):
            if not isinstance(mapping, dict) or canonical not in mapping:
                continue
            try:
                value = float(mapping[canonical])
            except (TypeError, ValueError):
                break
            if math.isfinite(value):
                values[canonical] = value
            break

    sanity_error = volume_constraint_sanity_error(
        values["volumeMin"], values["volumeStep"], values["volumeMax"]
    )
    if sanity_error:
        log.error(
            "MTAPI volume sanity ERROR symbol=%s: %s; order sizing will be blocked",
            normalized.get("symbol", "unknown"),
            sanity_error,
        )
        return sanity_error
    return None


def _normalize_symbol_params(data: object, symbol: str) -> dict:
    """Normalize MTAPI's SymbolParams and common casing/envelope variants."""
    root = _symbol_params_payload(data, symbol)
    if root is None:
        return {}

    info = _field_value(
        root, "symbolInfo", "symbol_info", "info", "symbolDetails",
        "symbol_details",
    )
    group = _field_value(root, "symbolGroup", "symbol_group", "group")
    if not isinstance(info, dict):
        # A bridge may return the symbol's fields directly instead of nesting
        # them under symbolInfo.
        info = root

    normalized = dict(root)
    normalized["symbol"] = str(
        _field_value(root, "symbol", "name") or symbol
    ).strip()
    normalized["symbolInfo"] = _canonical_symbol_fields(info)
    normalized["symbolGroup"] = _canonical_symbol_fields(group)
    # Preserve a top-level form too; the risk parser supports both layouts.
    normalized = _canonical_symbol_fields(normalized)
    normalized["_raw_symbol_fields"] = _raw_symbol_field_snapshot(root)
    try:
        volume_scale = configured_mtapi_volume_scale()
    except ValueError as exc:
        reason = str(exc)
        normalized["_mtapi_volume_units_error"] = reason
        log.error(
            "MTAPI volume configuration ERROR symbol=%s: %s; order sizing will be blocked",
            normalized["symbol"],
            reason,
        )
        return normalized

    normalized["_mtapi_volume_units"] = "lots"
    normalized["_mtapi_volume_scale"] = volume_scale
    volume_error = _convert_normalized_volume_fields(
        normalized,
        (
            ("root", root, normalized),
            ("symbolInfo", info, normalized["symbolInfo"]),
            ("symbolGroup", group, normalized["symbolGroup"]),
        ),
        volume_scale,
    )
    if volume_error:
        normalized["_mtapi_volume_units_error"] = volume_error
    return normalized


def _symbol_tick_size(symbol_params: dict) -> Optional[float]:
    for mapping in (
        symbol_params.get("symbolInfo"),
        symbol_params,
        symbol_params.get("symbolGroup"),
    ):
        if not isinstance(mapping, dict):
            continue
        value = _field_value(
            mapping, "tickSize", "tick_size", "tradeTickSize",
            "trade_tick_size", "minPriceIncrement", "priceStep",
        )
        number = _positive_symbol_number(value)
        if number is not None:
            return number
    return None


def _decimal_places(value: object) -> Optional[int]:
    try:
        exponent = Decimal(str(value)).normalize().as_tuple().exponent
    except (InvalidOperation, TypeError, ValueError):
        return None
    return max(0, -exponent)


def _with_tick_size(
    symbol_params: dict,
    symbol: str,
    tick_size: float,
    source: str,
    *,
    fetched_from_broker: bool,
) -> dict:
    """Attach a verified/fallback tick size and usable point precision."""
    result = dict(symbol_params)
    info = dict(result.get("symbolInfo") or {})
    info["tickSize"] = tick_size

    point = _positive_symbol_number(
        _field_value(info, "points", "point", "pointSize", "pricePoint")
    )
    digits_raw = _field_value(
        info, "digits", "precision", "priceDigits", "decimalPlaces"
    )
    digits: Optional[int] = None
    if digits_raw is not None and not isinstance(digits_raw, bool):
        try:
            parsed_digits = float(digits_raw)
            if math.isfinite(parsed_digits) and 0 <= parsed_digits <= 12:
                digits = int(parsed_digits)
        except (TypeError, ValueError, OverflowError):
            pass
    if digits is None:
        digits = _decimal_places(point if point is not None else tick_size)
    if digits is None:
        digits = 2
    if point is None:
        point = 10 ** (-digits)

    info["points"] = point
    info["digits"] = digits
    result["symbolInfo"] = info
    result["symbol"] = str(result.get("symbol") or symbol)
    result["_tick_size_source"] = source
    result["_symbol_params_fetch_ok"] = fetched_from_broker
    return result


def _is_xauusd_symbol(symbol: str) -> bool:
    # Broker suffixes such as XAUUSDb and XAUUSD.a are common.
    return symbol.strip().upper().startswith("XAUUSD")


async def get_symbol_params(symbol: str) -> dict:
    """Fetch and normalize broker metadata, with explicit tick-size fallbacks.

    MTAPI documents /SymbolParams as GET with query fields ``id`` and
    ``symbol``. Its normal response is {symbol, symbolInfo, symbolGroup}; error
    responses may use HTTP 201, so only HTTP 200 is considered successful.
    """
    endpoint = f"{_base_url.rstrip('/')}/SymbolParams"
    request_description = (
        f"GET {endpoint} params={{'id': '[REDACTED]', 'symbol': {symbol!r}}}"
    )
    cache_key = symbol.strip().upper()
    last_params: dict = {}
    last_failure = "no response"

    for attempt in range(1, _SYMBOL_PARAMS_RETRY_ATTEMPTS + 1):
        status: object = "unavailable"
        raw_body = ""
        try:
            if not _conn_id and not await ensure_connected():
                raise RuntimeError("no active MTAPI session")
            async with _get_session().get(
                endpoint,
                params={"id": _conn_id, "symbol": symbol},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                status = resp.status
                raw_body = await resp.text()
            data: object = None
            parse_error = ""
            try:
                data = json.loads(raw_body) if raw_body.strip() else None
            except (TypeError, ValueError) as exc:
                parse_error = f"invalid JSON ({type(exc).__name__})"

            normalized = _normalize_symbol_params(data, symbol)
            tick_size = _symbol_tick_size(normalized)
            is_error_payload = (
                isinstance(data, dict)
                and (
                    _is_error_quote(data)
                    or _field_value(data, "error", "exception", "stackTrace")
                    is not None
                )
            )
            if status == 200 and normalized and tick_size is not None and not is_error_payload:
                _SYMBOL_TICK_SIZE_CACHE[cache_key] = tick_size
                loaded = _with_tick_size(
                    normalized,
                    symbol,
                    tick_size,
                    "broker",
                    fetched_from_broker=True,
                )
                log.debug(
                    "MTAPI SymbolParams loaded symbol=%s tick_size=%s",
                    symbol,
                    tick_size,
                )
                return loaded

            if normalized:
                last_params = normalized
            if status != 200:
                last_failure = f"HTTP {status}"
            elif parse_error:
                last_failure = parse_error
            elif is_error_payload:
                last_failure = "error payload"
            elif not normalized:
                last_failure = "empty or unsupported response shape"
            else:
                last_failure = "tick size field missing, empty, or invalid"
        except Exception as exc:
            last_failure = f"{type(exc).__name__}: {_redact_candle_diagnostic(exc, limit=200)}"
            # The request did not yield a response. The empty body and
            # unavailable status are still included in the failure diagnostic.

        status_code = status if isinstance(status, int) else None
        _record_data_call_failure(
            "SymbolParams",
            message=f"{last_failure}: {raw_body}",
            status=status_code,
        )
        log.warning(
            "MTAPI SymbolParams tick-size lookup failed request=%s status=%s "
            "body=%s reason=%s attempt=%d/%d",
            request_description,
            status,
            _redact_candle_diagnostic(raw_body, limit=500),
            last_failure,
            attempt,
            _SYMBOL_PARAMS_RETRY_ATTEMPTS,
        )

        # Prefer a previously broker-verified tick size over a guessed value.
        cached_tick_size = _SYMBOL_TICK_SIZE_CACHE.get(cache_key)
        if cached_tick_size is not None:
            log.warning(
                "MTAPI SymbolParams fallback=cache symbol=%s tick_size=%s "
                "(last successfully fetched broker value)",
                symbol,
                cached_tick_size,
            )
            return _with_tick_size(
                last_params,
                symbol,
                cached_tick_size,
                "cache",
                fetched_from_broker=False,
            )

        if attempt < _SYMBOL_PARAMS_RETRY_ATTEMPTS:
            await asyncio.sleep(_SYMBOL_PARAMS_RETRY_DELAY_S)

    if _is_xauusd_symbol(symbol):
        log.error(
            "MTAPI SymbolParams unavailable for XAUUSD after %d attempts; "
            "using configured fallback XAUUSD_TICK_SIZE=%s symbol=%s",
            _SYMBOL_PARAMS_RETRY_ATTEMPTS,
            XAUUSD_TICK_SIZE,
            symbol,
        )
        log.warning(
            "MTAPI SymbolParams fallback=configured_env symbol=%s "
            "tick_size=%s source=XAUUSD_TICK_SIZE",
            symbol,
            XAUUSD_TICK_SIZE,
        )
        return _with_tick_size(
            last_params,
            symbol,
            XAUUSD_TICK_SIZE,
            "env:XAUUSD_TICK_SIZE",
            fetched_from_broker=False,
        )

    log.error(
        "MTAPI SymbolParams tick size unavailable for symbol=%s after %d attempts; "
        "no symbol-specific configured fallback exists",
        symbol,
        _SYMBOL_PARAMS_RETRY_ATTEMPTS,
    )
    return last_params


def _is_error_quote(data: dict) -> bool:
    return "code" in data and "stackTrace" in data


def get_connection() -> Optional[str]:
    """Return the MTAPI base URL when connected."""
    return _base_url if _connected else None


def get_conn_id() -> Optional[str]:
    """Return the active MTAPI session token."""
    return _conn_id if _connected else None


def get_connection_generation() -> int:
    """Return the count of confirmed ConnectEx sessions in this process."""
    return _connection_generation


def is_connected() -> bool:
    return _connected
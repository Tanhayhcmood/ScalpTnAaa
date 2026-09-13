"""
MetaAPI.cloud Connector — GoldScalperPro v4

Replaces the mtapi.io bridge with MetaAPI.cloud which has a complete
MT5 broker database (including AMarkets-Demo) and handles broker server
discovery automatically.

Required env vars:
    METAAPI_TOKEN      — API token from metaapi.cloud dashboard
    METAAPI_ACCOUNT_ID — MT5 account ID from metaapi.cloud dashboard

Optional (kept for backward compat but not used when MetaAPI token is set):
    MT5_USER / MT5_PASSWORD / MT5_HOST / MTAPI_URL

MetaAPI docs: https://metaapi.cloud/docs/client/
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Any

from live_trading.config import (
    METAAPI_TOKEN, METAAPI_ACCOUNT_ID,
    MT5_HOST, MT5_USER, MT5_PASSWORD,
)
from live_trading.signals.gold_engine import OHLCV
from live_trading.logger import get_logger

log = get_logger()

# ── Module-level MetaAPI state ────────────────────────────────────────────────
_api              = None   # MetaApi instance
_account          = None   # MetatraderAccount
_connection       = None   # RpcMetaApiConnection
_connected: bool  = False

# ── Timeframe → MetaAPI period map ───────────────────────────────────────────
_TF_MAP = {
    "1m":  "1m",  "5m":  "5m",  "15m": "15m", "30m": "30m",
    "1h":  "1h",  "4h":  "4h",  "1d":  "1d",
    "M1":  "1m",  "M5":  "5m",  "M15": "15m", "M30": "30m",
    "H1":  "1h",  "H4":  "4h",  "D1":  "1d",
}

# Candle period duration in seconds
_TF_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}


# ── Connection lifecycle ──────────────────────────────────────────────────────

async def connect(*args, **kwargs) -> bool:
    """
    Connect to MT5 via MetaAPI.cloud.
    Signature: connect(token, account_id, timeout_seconds)
    Falls back to env vars METAAPI_TOKEN / METAAPI_ACCOUNT_ID.
    """
    global _api, _account, _connection, _connected

    token      = args[0] if args else METAAPI_TOKEN
    account_id = args[1] if len(args) > 1 else METAAPI_ACCOUNT_ID
    timeout    = args[2] if len(args) > 2 else 300

    # Prefer env vars when args are empty strings
    token      = token      or METAAPI_TOKEN
    account_id = account_id or METAAPI_ACCOUNT_ID

    if not token or not account_id:
        log.error(
            "METAAPI_TOKEN and METAAPI_ACCOUNT_ID must be set. "
            "Sign up at https://metaapi.cloud, add your MT5 account, "
            "and set these two env vars on Render."
        )
        return False

    try:
        from metaapi_cloud_sdk import MetaApi  # type: ignore

        log.info(f"Connecting to MetaAPI.cloud — account {account_id[:12]}…")
        _api     = MetaApi(token)
        _account = await _api.metatrader_account_api.get_account(account_id)

        # Deploy account if not yet deployed
        if _account.state not in ("DEPLOYED", "DEPLOYING"):
            log.info("Deploying MetaAPI account…")
            await _account.deploy()

        log.info("Waiting for account to connect to broker…")
        await _account.wait_connected(timeout_in_seconds=min(timeout, 120))

        # RPC connection is simpler and sufficient for candle + order data
        _connection = _account.get_rpc_connection()
        await _connection.connect()
        await _connection.wait_synchronized(timeout_in_seconds=min(timeout, 60))

        _connected = True
        log.info(f"✅ MetaAPI connected — broker: {_account.broker_name or 'MT5'}")
        return True

    except Exception as exc:
        log.error(f"❌ MetaAPI connect failed: {exc}")
        _connected = False
        return False


async def disconnect() -> None:
    global _connection, _account, _api, _connected
    _connected = False
    try:
        if _connection:
            await _connection.close()
        if _api:
            _api.close()
    except Exception:
        pass
    _connection = None
    _account    = None
    _api        = None


async def ensure_connected(
    token:      str,
    account_id: str,
    timeout:    int = 300,
    attempt:    int = 1,
) -> bool:
    global _connected
    if _connected and _connection is not None:
        return True
    log.info(f"Reconnecting (attempt {attempt})…")
    return await connect(token, account_id, timeout)


def is_connected() -> bool:
    return _connected and _connection is not None


def get_connection():
    """Return the active RPC connection or None."""
    return _connection if _connected else None


# ── Market data ───────────────────────────────────────────────────────────────

async def fetch_candles(
    symbol:    str,
    timeframe: str = "5m",
    count:     int = 300,
) -> List[OHLCV]:
    """Fetch the last `count` closed candles for `symbol`."""
    if not is_connected():
        log.warning("fetch_candles: not connected")
        return []

    tf = _TF_MAP.get(timeframe, timeframe)
    try:
        tf_secs   = _TF_SECONDS.get(tf, 300)
        # Start far enough back to guarantee `count` candles
        start_time = datetime.now(timezone.utc) - timedelta(
            seconds=tf_secs * (count + 10)
        )
        candles = await _connection.get_historical_candles(
            symbol, tf, start_time, count + 5
        )
        # Convert to OHLCV; skip the still-open last candle
        result: List[OHLCV] = []
        for c in candles[:-1]:   # drop the forming candle
            t = _parse_time(c.get("time") or c.get("brokerTime"))
            result.append(OHLCV(
                time=t,
                open=float(c.get("open",  0)),
                high=float(c.get("high",  0)),
                low= float(c.get("low",   0)),
                close=float(c.get("close",0)),
                volume=float(c.get("tickVolume", c.get("volume", 0))),
            ))
        return result[-count:]
    except Exception as exc:
        log.warning(f"fetch_candles error: {exc}")
        return []


async def get_account_balance() -> float:
    info = await get_account_info()
    return info.get("balance", 0.0)


async def get_account_info() -> dict:
    if not is_connected():
        return {}
    try:
        info = await _connection.get_account_information()
        return {
            "balance":     info.get("balance",    0.0),
            "equity":      info.get("equity",     0.0),
            "margin":      info.get("margin",     0.0),
            "freeMargin":  info.get("freeMargin", info.get("free_margin", 0.0)),
            "profit":      info.get("profit",     0.0),
            "currency":    info.get("currency",   "USD"),
            "leverage":    info.get("leverage",   100),
            "name":        info.get("name",       ""),
            "login":       info.get("login",      MT5_USER),
            "server":      info.get("server",     MT5_HOST),
        }
    except Exception as exc:
        log.warning(f"get_account_info error: {exc}")
        return {}


async def get_open_positions(symbol: str) -> list:
    if not is_connected():
        return []
    try:
        positions = await _connection.get_positions()
        return [p for p in positions if p.get("symbol") == symbol]
    except Exception as exc:
        log.warning(f"get_open_positions error: {exc}")
        return []


def mt5_pos_to_dict(pos: dict) -> dict:
    ptype     = pos.get("type", "POSITION_TYPE_BUY")
    direction = "BUY" if "BUY" in str(ptype).upper() else "SELL"
    return {
        "id":         str(pos.get("id", pos.get("ticket", ""))),
        "symbol":     pos.get("symbol", ""),
        "direction":  direction,
        "lot_size":   pos.get("volume", 0),
        "price_open": pos.get("openPrice", pos.get("priceOpen", 0)),
        "sl":         pos.get("stopLoss",   pos.get("sl", 0)),
        "tp":         pos.get("takeProfit", pos.get("tp", 0)),
        "profit":     pos.get("profit",  0),
        "time_str":   str(pos.get("time", pos.get("openTime", ""))),
        # keep original for executor
        "_raw":       pos,
    }


# ── Bar-close detection ───────────────────────────────────────────────────────

async def get_last_completed_bar_time(
    symbol:    str,
    timeframe: str = "5m",
) -> Optional[datetime]:
    candles = await fetch_candles(symbol, timeframe, count=2)
    if len(candles) < 2:
        return None
    return candles[-2].time


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_time(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=timezone.utc) if raw.tzinfo is None else raw
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)

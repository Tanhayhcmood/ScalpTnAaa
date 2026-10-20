"""
MTAPI Order Executor – GoldScalperPro v4

Places and closes MT5 orders via the official MTAPI REST API.

Endpoints used:
    GET /OrderSendSafe   – place a market order
    GET /OrderCloseSafe  – close an open position by ticket
    GET /OrderModifySafe – modify SL/TP on an open position
"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
import math
from typing import Optional

import aiohttp

from live_trading.logger import get_logger
from live_trading.mt5.connector import _get_session, get_connection, get_conn_id

log = get_logger()


@dataclass
class TradeResult:
    success:     bool
    position_id: Optional[str]
    message:     str
    order_id:    Optional[str] = None


# ── Lot normalisation ─────────────────────────────────────────────────────────

def _normalise_lot(
    lot: float,
    vol_min: float,
    vol_step: float,
    vol_max: float,
) -> float:
    if not math.isfinite(lot) or lot <= 0:
        raise ValueError("lot size must be a finite positive number")
    if (
        not all(math.isfinite(value) for value in (vol_min, vol_step, vol_max))
        or vol_step <= 0
        or vol_min <= 0
        or vol_max < vol_min
    ):
        raise ValueError("invalid broker volume constraints")
    if lot < vol_min:
        raise ValueError("lot size is below the broker minimum")
    # Decimal arithmetic makes the step floor exact and never rounds the
    # risk-sized lot upward or beyond the broker's maximum.
    minimum = Decimal(str(vol_min))
    step = Decimal(str(vol_step))
    capped_lot = min(Decimal(str(lot)), Decimal(str(vol_max)))
    steps = int(
        ((capped_lot - minimum) / step).to_integral_value(rounding=ROUND_FLOOR)
    )
    result = minimum + steps * step
    if result < minimum or result > capped_lot:
        raise ValueError("normalized lot violates broker volume constraints")
    return float(result)


def _is_error(data: dict) -> bool:
    """MTAPI returns {message, code, stackTrace} on errors."""
    return isinstance(data, dict) and "code" in data and "stackTrace" in data


# ── Place market order ────────────────────────────────────────────────────────

async def place_market_order(
    symbol:    str,
    direction: str,       # "BUY" | "SELL"
    lot_size:  float,
    sl:        float,
    tp:        float,
    comment:   str = "GSPv4",
    deviation: int = 30,
    price_digits: int = 2,
    volume_min: Optional[float] = None,
    volume_step: Optional[float] = None,
    volume_max: Optional[float] = None,
) -> TradeResult:
    base    = get_connection()
    conn_id = get_conn_id()
    if not base or not conn_id:
        return TradeResult(False, None, "Not connected to MTAPI")

    normalized_direction = direction.upper().strip()
    if normalized_direction not in {"BUY", "SELL"}:
        return TradeResult(False, None, f"Invalid trade direction: {direction!r}")
    if any(value is None for value in (volume_min, volume_step, volume_max)):
        return TradeResult(
            False,
            None,
            "Broker volume constraints are unavailable; refusing to guess lot limits",
        )
    try:
        lot = _normalise_lot(
            float(lot_size),
            vol_min=float(volume_min),
            vol_step=float(volume_step),
            vol_max=float(volume_max),
        )
    except (TypeError, ValueError) as exc:
        return TradeResult(False, None, f"Invalid lot size: {exc}")
    if not all(math.isfinite(float(value)) and float(value) > 0 for value in (sl, tp)):
        return TradeResult(False, None, "Stop loss and take profit must be finite positive prices")

    operation = 0 if normalized_direction == "BUY" else 1   # 0=BUY  1=SELL

    log.debug(f"Placing {normalized_direction} {lot} lots {symbol}  SL={sl}  TP={tp}")

    try:
        normalized_digits = max(0, min(8, int(price_digits)))
    except (TypeError, ValueError):
        normalized_digits = 2

    params = {
        "id":         conn_id,
        "symbol":     symbol,
        "operation":  operation,
        "volume":     lot,
        "slippage":   deviation,
        "stoploss":   round(sl, normalized_digits),
        "takeprofit": round(tp, normalized_digits),
        "comment":    comment[:32],
    }

    try:
        sess = _get_session()
        async with sess.get(
            f"{base}/OrderSendSafe",
            params=params,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            data = await resp.json(content_type=None)

            if _is_error(data):
                msg = data.get("message", f"code={data.get('code','?')}")
                log.error(f"MTAPI order execution result: FAILED — {msg}")
                return TradeResult(False, None, msg)

            ticket = data.get("ticket") if isinstance(data, dict) else None
            if ticket is not None and resp.status == 200:
                pos_id = str(ticket)
                log.info(
                    f"MTAPI order execution result: SUCCESS — ticket={pos_id}  "
                    f"{normalized_direction} {lot} lots  SL={sl}  TP={tp}"
                )
                return TradeResult(True, pos_id, "OK", pos_id)

            msg = f"Unexpected response (status={resp.status}): {str(data)[:200]}"
            log.error(f"place_market_order: {msg}")
            return TradeResult(False, None, msg)

    except Exception as exc:
        log.error(f"place_market_order error: {exc}")
        return TradeResult(False, None, str(exc))


# ── Close position ────────────────────────────────────────────────────────────

async def close_position(position_id: str, deviation: int = 30, **kwargs) -> TradeResult:
    base    = get_connection()
    conn_id = get_conn_id()
    if not base or not conn_id:
        return TradeResult(False, None, "Not connected to MTAPI")

    try:
        sess = _get_session()
        async with sess.get(
            f"{base}/OrderCloseSafe",
            params={
                "id":         conn_id,
                "ticket":     int(position_id),
                "slippage": deviation,
            },
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            data = await resp.json(content_type=None)

            if _is_error(data):
                msg = data.get("message", f"code={data.get('code','?')}")
                log.error(f"MTAPI close execution result: FAILED — {msg}")
                return TradeResult(False, None, msg)

            if resp.status == 200:
                log.info(f"MTAPI close execution result: SUCCESS — position {position_id} closed")
                return TradeResult(True, position_id, "Closed")

            msg = f"Unexpected response (status={resp.status}): {str(data)[:200]}"
            log.error(f"close_position: {msg}")
            return TradeResult(False, None, msg)

    except Exception as exc:
        log.error(f"close_position error: {exc}")
        return TradeResult(False, None, str(exc))


# ── Modify position ───────────────────────────────────────────────────────────

async def modify_position(
    position_id: str, sl: float, tp: float
) -> TradeResult:
    base    = get_connection()
    conn_id = get_conn_id()
    if not base or not conn_id:
        return TradeResult(False, None, "Not connected to MTAPI")

    try:
        sess = _get_session()
        async with sess.get(
            f"{base}/OrderModifySafe",
            params={
                "id":         conn_id,
                "ticket":     int(position_id),
                "stoploss":   round(sl, 2),
                "takeprofit": round(tp, 2),
            },
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            data = await resp.json(content_type=None)

            if _is_error(data):
                msg = data.get("message", f"code={data.get('code','?')}")
                log.error(f"MTAPI modify execution result: FAILED — {msg}")
                return TradeResult(False, None, msg)

            if resp.status == 200:
                log.info(
                    f"MTAPI modify execution result: SUCCESS — "
                    f"position {position_id}  SL={sl}  TP={tp}"
                )
                return TradeResult(True, position_id, "Modified")

            msg = f"Unexpected response (status={resp.status}): {str(data)[:200]}"
            log.error(f"modify_position: {msg}")
            return TradeResult(False, None, msg)

    except Exception as exc:
        log.error(f"modify_position error: {exc}")
        return TradeResult(False, None, str(exc))
"""
MetaAPI.cloud Order Executor — GoldScalperPro v4

Places, modifies, and closes MT5 orders via the MetaAPI RPC connection
managed by connector.py.

MetaAPI trade docs: https://metaapi.cloud/docs/client/
"""

import asyncio
from dataclasses import dataclass
import math
from typing import Optional

from live_trading.config import RPC_CALL_TIMEOUT
from live_trading.logger import get_logger
from live_trading.mt5.connector import get_connection, mark_connection_unhealthy

log = get_logger()


@dataclass
class TradeResult:
    success:     bool
    position_id: Optional[str]
    message:     str
    order_id:    Optional[str] = None


# ── Lot normalisation ─────────────────────────────────────────────────────────

def _normalise_lot(lot: float,
                   vol_min:  float = 0.01,
                   vol_step: float = 0.01,
                   vol_max:  float = 500.0) -> float:
    try:
        lot = float(lot)
        vol_min = float(vol_min)
        vol_step = float(vol_step)
        vol_max = float(vol_max)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("lot and broker volume constraints must be numeric") from exc
    if (
        not math.isfinite(lot)
        or lot <= 0
        or not math.isfinite(vol_min)
        or not math.isfinite(vol_step)
        or not math.isfinite(vol_max)
        or vol_step <= 0
        or vol_min <= 0
        or vol_max < vol_min
    ):
        raise ValueError("invalid lot size or broker volume constraints")
    steps  = round((lot - vol_min) / vol_step)
    result = vol_min + steps * vol_step
    return max(vol_min, min(vol_max, round(result, 4)))


def _is_market_closed_error(exc: BaseException) -> bool:
    """Return True for broker session-closed responses."""
    text = str(exc).lower()
    return any(marker in text for marker in (
        "market is closed",
        "market closed",
        "market is not open",
        "trading is disabled",
        "trade is disabled",
    ))


# ── Place market order ────────────────────────────────────────────────────────

async def place_market_order(
    symbol:    str,
    direction: str,     # "BUY" | "SELL"
    lot_size:  float,
    sl:        float,
    tp:        float,
    comment:   str = "GSPv4",
    deviation: int = 30,
) -> TradeResult:
    conn = get_connection()
    if conn is None:
        return TradeResult(False, None, "No MetaAPI connection")

    lot = _normalise_lot(lot_size)
    log.debug(f"Placing {direction} {lot} lots {symbol} — SL={sl}  TP={tp}")

    try:
        options = {
            "comment":   comment[:32],
            "slippage":  deviation,
        }
        if direction.upper() == "BUY":
            result = await asyncio.wait_for(
                conn.create_market_buy_order(
                    symbol, lot,
                    stop_loss=round(sl, 2),
                    take_profit=round(tp, 2),
                    options=options,
                ),
                timeout=RPC_CALL_TIMEOUT,
            )
        else:
            result = await asyncio.wait_for(
                conn.create_market_sell_order(
                    symbol, lot,
                    stop_loss=round(sl, 2),
                    take_profit=round(tp, 2),
                    options=options,
                ),
                timeout=RPC_CALL_TIMEOUT,
            )

        # MetaAPI returns a dict with 'orderId' and 'tradeExecutionTime'
        pos_id = str(result.get("positionId", result.get("orderId", "")))
        log.info(
            f"✅ Trade opened — positionId={pos_id}  "
            f"{direction} {lot} lots  SL={sl}  TP={tp}"
        )
        return TradeResult(True, pos_id, "OK", pos_id)

    except Exception as exc:
        if _is_market_closed_error(exc):
            # A closed symbol session is temporary; do not mark MetaAPI
            # unhealthy and let the live loop retry on the next scan.
            log.warning(f"⏸️ Broker market closed for {symbol}; retrying on the next scan")
            return TradeResult(False, None, "MARKET_CLOSED")
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in str(exc).lower():
            mark_connection_unhealthy(f"place order failed: {exc}")
        log.error(f"❌ place_market_order error: {exc}")
        return TradeResult(False, None, str(exc))


# ── Close position ────────────────────────────────────────────────────────────

async def close_position(position_id: str, **kwargs) -> TradeResult:
    conn = get_connection()
    if conn is None:
        return TradeResult(False, None, "No MetaAPI connection")

    try:
        result = await asyncio.wait_for(
            conn.close_position(position_id),
            timeout=RPC_CALL_TIMEOUT,
        )
        log.info(f"✅ Position {position_id} closed")
        return TradeResult(True, position_id, "Closed")

    except Exception as exc:
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in str(exc).lower():
            mark_connection_unhealthy(f"close position failed: {exc}")
        log.error(f"❌ close_position error: {exc}")
        return TradeResult(False, None, str(exc))


# ── Modify position ───────────────────────────────────────────────────────────

async def modify_position(position_id: str,
                           sl: float,
                           tp: float) -> TradeResult:
    conn = get_connection()
    if conn is None:
        return TradeResult(False, None, "No MetaAPI connection")

    try:
        await asyncio.wait_for(
            conn.modify_position(
                position_id,
                stop_loss=round(sl, 2),
                take_profit=round(tp, 2),
            ),
            timeout=RPC_CALL_TIMEOUT,
        )
        log.info(f"✅ Position {position_id} modified — SL={sl}  TP={tp}")
        return TradeResult(True, position_id, "Modified")

    except Exception as exc:
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in str(exc).lower():
            mark_connection_unhealthy(f"modify position failed: {exc}")
        log.error(f"❌ modify_position error: {exc}")
        return TradeResult(False, None, str(exc))

"""
Capital Manager — Smart SL/TP/LotSize for XAUUSD.
Ported from capitalManager.ts
"""
from dataclasses import dataclass
import math
from typing import Optional

DEFAULT_RISK_PCT    = 1.0
ATR_BUFFER_MULT     = 0.25
DEFAULT_SL_ATR_MULT = 3.00
MIN_SL_ATR_MULT     = 3.00
MAX_SL_ATR_MULT     = 3.50
FIXED_TP_RR         = 2.00
LOT_DOLLAR_PER_UNIT = 100
MIN_LOT             = 0.01
LOT_STEP            = 0.01
MAX_LOT             = 50.0


@dataclass
class CapitalInput:
    direction:           str    # BUY | SELL
    entry_price:         float
    atr:                 float
    account_balance:     float
    risk_percent:        float = DEFAULT_RISK_PCT
    take_profit_rr:      float = FIXED_TP_RR
    take_profit_level:   Optional[float] = None
    order_block_top:     Optional[float] = None
    order_block_bottom:  Optional[float] = None
    swing_high:          Optional[float] = None
    swing_low:           Optional[float] = None
    resistance_level:    Optional[float] = None
    support_level:       Optional[float] = None
    # Protective stop distance is based on a higher-timeframe ATR supplied by
    # the decision engine, not the signal candle's ATR.
    sl_atr_multiplier:   float = DEFAULT_SL_ATR_MULT


@dataclass
class CapitalOutput:
    entry_price:            float
    stop_loss:              float
    take_profit:            float
    risk_reward_ratio:      float
    trailing_stop_distance: float
    trailing_activation_at: float
    break_even_at:          float
    break_even_sl:          float
    lot_size:               float
    risk_amount:            float
    sl_distance_usd:        float
    sl_distance_pips:       float
    risk_budget:            float
    risk_percent:            float
    min_lot_risk_exceeded:  bool


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _r2(n: float) -> float: return round(n, 2)
def _r4(n: float) -> float: return round(n, 4)


def _calc_smart_sl(direction: str, entry: float, atr: float, inp: CapitalInput) -> float:
    buffer = atr * ATR_BUFFER_MULT
    requested_mult = _clamp(
        float(inp.sl_atr_multiplier),
        MIN_SL_ATR_MULT,
        MAX_SL_ATR_MULT,
    )
    min_sl = atr * requested_mult
    max_sl = atr * MAX_SL_ATR_MULT
    raw_sl = None

    if direction == "BUY":
        cands = []
        if inp.order_block_bottom is not None and inp.order_block_bottom < entry:
            cands.append(inp.order_block_bottom)
        if inp.swing_low is not None and inp.swing_low < entry:
            cands.append(inp.swing_low)
        if inp.support_level is not None and inp.support_level < entry:
            cands.append(inp.support_level)
        if cands:
            # Use the outer structural invalidation for a long as well. The
            # nearest support can be a wick or the edge of the active order
            # block; use the lowest valid level and keep the ATR cap below.
            level  = min(cands)
            raw_sl = entry - (entry - level + buffer)
    else:
        cands = []
        if inp.order_block_top is not None and inp.order_block_top > entry:
            cands.append(inp.order_block_top)
        if inp.swing_high is not None and inp.swing_high > entry:
            cands.append(inp.swing_high)
        if inp.resistance_level is not None and inp.resistance_level > entry:
            cands.append(inp.resistance_level)
        if cands:
            # Use the outer structural invalidation for a short. The nearest
            # resistance is often only a wick or the edge of the active
            # order block; putting the stop there lets a normal retracement
            # invalidate a still-valid trend. MAX_SL_ATR_MULT below still
            # bounds the result.
            level  = max(cands)
            raw_sl = entry + (level - entry + buffer)

    fallback  = atr * requested_mult
    sl_dist   = abs(entry - raw_sl) if raw_sl is not None else fallback
    clamped   = _clamp(sl_dist, min_sl, max_sl)
    return _r2(entry - clamped if direction == "BUY" else entry + clamped)


def _calc_lot_size(sl_dist_usd: float, balance: float, risk_pct: float):
    """Calculate a broker-stepped lot size from the percentage risk budget.

    The lot is rounded down to the broker step so a valid stepped lot cannot
    exceed the requested percentage budget.  If the broker minimum itself
    exceeds that budget, the caller must reject the trade rather than silently
    over-risk it.
    """
    risk_budget = max(0.0, balance * risk_pct / 100)
    if sl_dist_usd <= 0:
        return MIN_LOT, 0.0, _r2(risk_budget), True

    raw_lot = risk_budget / (sl_dist_usd * LOT_DOLLAR_PER_UNIT)
    stepped_lot = math.floor(raw_lot / LOT_STEP + 1e-9) * LOT_STEP
    lot_size = _r4(_clamp(stepped_lot, MIN_LOT, MAX_LOT))
    actual_risk = _r2(lot_size * sl_dist_usd * LOT_DOLLAR_PER_UNIT)
    risk_budget_exceeded = actual_risk > risk_budget + 0.01
    return lot_size, actual_risk, _r2(risk_budget), risk_budget_exceeded


def calc_trade_parameters(inp: CapitalInput) -> CapitalOutput:
    entry      = inp.entry_price
    direction  = inp.direction
    atr        = inp.atr
    risk_pct   = inp.risk_percent

    sl         = _calc_smart_sl(direction, entry, atr, inp)
    sl_dist    = _r2(abs(entry - sl))
    sl_pips    = _r2(sl_dist * 100)

    range_target_valid = (
        inp.take_profit_level is not None
        and (
            (direction == "BUY" and inp.take_profit_level > entry)
            or (direction == "SELL" and inp.take_profit_level < entry)
        )
    )
    if range_target_valid:
        tp = _r2(inp.take_profit_level)  # type: ignore[arg-type]
        tp_dist = abs(tp - entry)
    else:
        tp_dist = sl_dist * inp.take_profit_rr
        tp = _r2(entry + tp_dist if direction == "BUY" else entry - tp_dist)
    rr         = _r2(tp_dist / sl_dist) if sl_dist > 0 else 0.0

    lot, risk, risk_budget, min_lot_risk_exceeded = _calc_lot_size(
        sl_dist, inp.account_balance, risk_pct
    )

    # Break-even trigger: price must move 1× SL distance in our favour before
    # we can safely move the stop to entry.  Trailing stop activates at the
    # same level.  These are informational fields — the live loop does not yet
    # implement automatic BE/trailing moves; they're displayed on the panel.
    be_dist    = sl_dist * 1.0
    be_at      = _r2(entry + be_dist if direction == "BUY" else entry - be_dist)
    trail_act  = be_at
    trail_dist = _r2(sl_dist * 0.5)   # trail distance = half of original SL

    return CapitalOutput(
        entry_price=_r2(entry),
        stop_loss=sl,
        take_profit=tp,
        risk_reward_ratio=rr,
        trailing_stop_distance=trail_dist,
        trailing_activation_at=trail_act,
        break_even_at=be_at,
        break_even_sl=entry,
        lot_size=lot,
        risk_amount=risk,
        sl_distance_usd=sl_dist,
        sl_distance_pips=sl_pips,
        risk_budget=risk_budget,
        risk_percent=_r2(risk_pct),
        min_lot_risk_exceeded=min_lot_risk_exceeded,
    )

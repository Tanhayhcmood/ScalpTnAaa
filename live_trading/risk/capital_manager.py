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
MIN_SL_ATR_MULT     = 1.50
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
    sl_min_atr_multiplier: float = MIN_SL_ATR_MULT
    structure_buffer_atr: float = 0.20


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
    stop_loss_mode:          str = "ATR_FALLBACK"
    stop_loss_reason:        str = ""
    stop_loss_valid:         bool = True
    structural_level:        Optional[float] = None


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _r2(n: float) -> float: return round(n, 2)
def _r4(n: float) -> float: return round(n, 4)


def _finite_level(value: Optional[float], entry: float, direction: str) -> Optional[float]:
    try:
        level = float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return None
    if not math.isfinite(level) or level <= 0:
        return None
    if direction == "BUY" and level < entry:
        return level
    if direction == "SELL" and level > entry:
        return level
    return None

@dataclass(frozen=True)
class SmartStopPlan:
    stop_loss: float
    valid: bool
    mode: str
    reason: str
    structural_level: Optional[float]
    distance_atr: float


def _calc_smart_stop(direction: str, entry: float, atr: float, inp: CapitalInput) -> SmartStopPlan:
    """Build a bounded structural stop and refuse unsafe clipping.

    A valid structural invalidation level is preferred over a stale outer
    level. The stop receives an ATR buffer, is protected by a noise floor,
    and is rejected when the structure is farther away than the configured
    risk envelope; placing a stop inside invalidation would be misleading.
    """
    normalized_direction = str(direction).upper().strip()
    safe_entry = float(entry)
    safe_atr = max(float(atr), 1e-9)
    min_mult = _clamp(float(inp.sl_min_atr_multiplier), 0.75, MAX_SL_ATR_MULT)
    max_mult = _clamp(float(inp.sl_atr_multiplier), min_mult, MAX_SL_ATR_MULT)
    min_distance = safe_atr * min_mult
    max_distance = safe_atr * max_mult
    buffer = safe_atr * max(0.0, float(inp.structure_buffer_atr))

    raw_levels = (
        (inp.order_block_bottom, inp.swing_low, inp.support_level)
        if normalized_direction == "BUY"
        else (inp.order_block_top, inp.swing_high, inp.resistance_level)
    )
    levels = [
        level for level in (
            _finite_level(value, safe_entry, normalized_direction)
            for value in raw_levels
        )
        if level is not None
    ]

    if levels:
        structural_level = max(levels) if normalized_direction == "BUY" else min(levels)
        raw_distance = abs(safe_entry - structural_level) + buffer
        if raw_distance > max_distance + 1e-9:
            fallback_stop = (
                safe_entry - max_distance
                if normalized_direction == "BUY"
                else safe_entry + max_distance
            )
            return SmartStopPlan(
                stop_loss=_r2(fallback_stop),
                valid=False,
                mode="STRUCTURE_TOO_FAR",
                reason=(f"structural invalidation is {raw_distance / safe_atr:.2f} ATR "
                        f"beyond the {max_mult:.2f} ATR risk envelope"),
                structural_level=_r2(structural_level),
                distance_atr=round(max_distance / safe_atr, 4),
            )
        distance = max(raw_distance, min_distance)
        mode = "STRUCTURAL" if raw_distance >= min_distance else "STRUCTURAL_MIN_FLOOR"
        reason = "structural invalidation plus ATR buffer"
    else:
        structural_level = None
        distance = max_distance
        mode = "ATR_FALLBACK"
        reason = "no valid directional structure was available"

    stop = (
        safe_entry - distance
        if normalized_direction == "BUY"
        else safe_entry + distance
    )
    return SmartStopPlan(
        stop_loss=_r2(stop),
        valid=True,
        mode=mode,
        reason=reason,
        structural_level=(_r2(structural_level) if structural_level is not None else None),
        distance_atr=round(distance / safe_atr, 4),
    )


def _calc_smart_sl(direction: str, entry: float, atr: float, inp: CapitalInput) -> float:
    """Backward-compatible scalar wrapper for callers/tests."""
    return _calc_smart_stop(direction, entry, atr, inp).stop_loss

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

    stop_plan  = _calc_smart_stop(direction, entry, atr, inp)
    sl         = stop_plan.stop_loss
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
        stop_loss_mode=stop_plan.mode,
        stop_loss_reason=stop_plan.reason,
        stop_loss_valid=stop_plan.valid,
        structural_level=stop_plan.structural_level,
    )

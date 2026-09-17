"""Three-stage, risk-multiple trailing-stop calculations.

This module is deliberately pure.  It does not call MTAPI; the live loop owns
broker I/O, persistence, and per-ticket error isolation.

Stages:
  1R   -> breakeven plus a small buffer
  1.5R -> lock 0.5R
  2R   -> lock 1R, optionally improved by an ATR trail

The ATR candidate is always clamped so it can never weaken the stage-3 lock.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class TrailingConfig:
    enabled: bool = True
    activation_r: float = 1.0
    step_r: float = 0.5
    lock_buffer_r: float = 0.1
    atr_gap_mult: float = 0.5
    min_step_price: float = 0.05
    breakeven_r: float = 0.0
    stage_two_r: float = 0.5
    stage_three_r: float = 1.0
    atr_activation_r: float = 2.0


def _r2(value: float) -> float:
    return round(float(value), 2)


def r_multiple_of(
    direction: str,
    entry: float,
    risk_distance: float,
    current_price: float,
) -> float:
    """Return realised open profit in R, using the close-side price."""
    if risk_distance <= 0:
        return 0.0
    is_buy = direction.upper() == "BUY"
    profit_distance = (
        current_price - entry if is_buy else entry - current_price
    )
    return round(profit_distance / risk_distance, 3)


def _locked_distance_r(r_multiple: float, cfg: TrailingConfig) -> Optional[float]:
    """Return the strongest discrete lock reached by ``r_multiple``."""
    if r_multiple < cfg.activation_r:
        return None
    if r_multiple >= cfg.atr_activation_r:
        return max(cfg.stage_three_r, cfg.stage_two_r, cfg.lock_buffer_r)
    if r_multiple >= cfg.activation_r + cfg.step_r:
        return max(cfg.stage_two_r, cfg.lock_buffer_r)
    return max(cfg.breakeven_r, cfg.lock_buffer_r)


def stage_for_r(r_multiple: float, cfg: TrailingConfig) -> tuple[str, str]:
    """Return a stable telemetry label and activation threshold."""
    if r_multiple < cfg.activation_r:
        return "PRE_ACTIVATION", f"{cfg.activation_r:g}R"
    if r_multiple < cfg.activation_r + cfg.step_r:
        return "BREAKEVEN_PLUS", f"{cfg.activation_r:g}R"
    if r_multiple < cfg.atr_activation_r:
        return "LOCK_HALF_R", f"{cfg.activation_r + cfg.step_r:g}R"
    return "LOCK_ONE_R_ATR", f"{cfg.atr_activation_r:g}R"


def compute_staircase_sl(
    direction: str,
    entry: float,
    risk_distance: float,
    current_price: float,
    atr: float,
    cfg: TrailingConfig,
) -> Optional[float]:
    """Return the strongest candidate SL reached by the current R profit.

    The thresholds intentionally form a fixed three-stage policy when the
    default config is used: 1R, 1.5R, and 2R.  ``activation_r`` and ``step_r``
    remain configurable for backwards compatibility with existing Render
    settings.  The caller must still compare this candidate to the live SL
    with :func:`should_apply`.
    """
    if not cfg.enabled or risk_distance <= 0 or cfg.step_r <= 0:
        return None

    is_buy = direction.upper() == "BUY"
    r_multiple = r_multiple_of(direction, entry, risk_distance, current_price)
    locked_r = _locked_distance_r(r_multiple, cfg)
    if locked_r is None:
        return None

    base = entry + locked_r * risk_distance if is_buy else entry - locked_r * risk_distance
    candidate = base

    # ATR trailing only starts at 2R and can improve the 1R lock.  It is never
    # allowed to pull the stop back toward entry or below a prior stage.
    if r_multiple >= cfg.atr_activation_r and atr and atr > 0 and cfg.atr_gap_mult > 0:
        atr_candidate = (
            current_price - atr * cfg.atr_gap_mult
            if is_buy
            else current_price + atr * cfg.atr_gap_mult
        )
        candidate = max(base, atr_candidate) if is_buy else min(base, atr_candidate)

    return _r2(candidate)


def should_apply(
    direction: str,
    current_sl: float,
    candidate_sl: Optional[float],
    min_step_price: float,
) -> bool:
    """Return true only for a meaningful move in the trade's favour."""
    if candidate_sl is None:
        return False
    if direction.upper() == "BUY":
        return candidate_sl - current_sl >= min_step_price
    return current_sl - candidate_sl >= min_step_price
"""
Smart Money Concepts Engine — BOS, CHoCH, Order Blocks, FVG, Liquidity
Ported from smcEngine.ts
This engine is self-contained: it only uses OHLCV input.
Cross-engine combining (Wyckoff, PA, Trend) is done in decision_engine.py.
"""
from dataclasses import dataclass, field, replace
from typing import List, Literal, Optional, Set
from live_trading.signals.gold_engine import OHLCV


# ── Per-timeframe config ──────────────────────────────────────────────────────

@dataclass
class SmcConfig:
    swing_lookback: int
    fvg_min_size: float
    equal_level_tolerance: float
    liquidity_sweep_min: float
    min_sweep_close_margin: float
    near_ob_threshold: float
    near_fvg_threshold: float
    min_ob_body_size: float
    min_ob_body_ratio: float
    min_break_distance: float
    min_bos_body_ratio: float
    max_order_blocks: int
    max_fvgs: int
    max_bos: int
    max_choch: int
    max_sweeps: int


CFG_M5 = SmcConfig(
    swing_lookback=5, fvg_min_size=0.10,
    equal_level_tolerance=0.15,
    liquidity_sweep_min=0.15, min_sweep_close_margin=0.10,
    near_ob_threshold=0.50, near_fvg_threshold=0.30,
    min_ob_body_size=0.15, min_ob_body_ratio=0.30,
    min_break_distance=0.20, min_bos_body_ratio=0.35,
    max_order_blocks=6, max_fvgs=6, max_bos=5, max_choch=3, max_sweeps=5,
)


def _config_for_timeframe(timeframe: str) -> SmcConfig:
    """Scale SMC zone thresholds with the candle timeframe."""
    tf = str(timeframe or "M5").upper()
    if tf in {"M10", "10M"}:
        return replace(
            CFG_M5,
            fvg_min_size=0.14,
            near_ob_threshold=0.60,
            near_fvg_threshold=0.40,
            min_ob_body_size=0.18,
            min_break_distance=0.24,
        )
    if tf in {"M15", "15M"}:
        return replace(
            CFG_M5,
            fvg_min_size=0.18,
            near_ob_threshold=0.75,
            near_fvg_threshold=0.50,
            min_ob_body_size=0.22,
            min_break_distance=0.30,
        )
    if tf in {"M20", "20M"}:
        return replace(
            CFG_M5,
            fvg_min_size=0.22,
            near_ob_threshold=0.90,
            near_fvg_threshold=0.60,
            min_ob_body_size=0.26,
            min_break_distance=0.35,
        )
    if tf in {"M30", "30M"}:
        return replace(
            CFG_M5,
            fvg_min_size=0.28,
            near_ob_threshold=1.05,
            near_fvg_threshold=0.75,
            min_ob_body_size=0.30,
            min_break_distance=0.45,
        )
    if tf in {"H1", "1H"}:
        return replace(
            CFG_M5,
            swing_lookback=4,
            fvg_min_size=0.40,
            near_ob_threshold=1.50,
            near_fvg_threshold=1.00,
            min_ob_body_size=0.40,
            min_break_distance=0.60,
        )
    return CFG_M5


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class SmcBos:
    type: Literal["BUY", "SELL"]
    price: float
    bar_index: int
    time: str

@dataclass
class SmcChoch:
    type: Literal["BUY", "SELL"]
    price: float
    bar_index: int
    time: str

@dataclass
class SmcOrderBlock:
    type: Literal["BULLISH", "BEARISH"]
    high: float
    low: float
    open: float
    close: float
    bar_index: int
    time: str
    mitigated: bool
    mitigation_state: Literal["FRESH", "RETESTED"] = "FRESH"

@dataclass
class SmcFvg:
    type: Literal["BULLISH", "BEARISH"]
    top: float
    bottom: float
    bar_index: int
    time: str
    filled: bool
    fill_state: Literal["UNTOUCHED", "PARTIAL", "FULL"] = "UNTOUCHED"

@dataclass
class SmcLiquiditySweep:
    type: Literal["BULLISH", "BEARISH"]
    swept_level: float
    wick_extreme: float
    bar_index: int
    time: str

@dataclass
class SmcEqualLevel:
    type: Literal["HIGH", "LOW"]
    price: float
    bar_indices: List[int]
    time: str

@dataclass
class SmcMitigationBlock:
    original_ob: SmcOrderBlock
    mitigated_at_bar_index: int
    mitigated_at_time: str

@dataclass
class SmcResult:
    timeframe: str
    timestamp: str
    current_price: float
    trend: Literal["BULLISH", "BEARISH", "NEUTRAL"]
    bos_signals: List[SmcBos]
    choch_signals: List[SmcChoch]
    order_blocks: List[SmcOrderBlock]
    fair_value_gaps: List[SmcFvg]
    liquidity_sweeps: List[SmcLiquiditySweep]
    equal_highs: List[SmcEqualLevel]
    equal_lows: List[SmcEqualLevel]
    mitigation_blocks: List[SmcMitigationBlock]
    smc_signal: Literal["BUY", "SELL", "NEUTRAL"]
    smc_score: float


def get_latest_structure_event(smc: SmcResult) -> Optional[SmcBos | SmcChoch]:
    """Return the newest confirmed BOS/CHoCH event.

    Structure events are ordered by their closed-candle index, not by the
    separate truncated BOS/CHoCH lists. On the same candle, CHoCH wins
    because it represents a reversal event.
    """
    events: List[SmcBos | SmcChoch] = [*smc.bos_signals, *smc.choch_signals]
    if not events:
        return None
    return max(events, key=lambda event: (event.bar_index, isinstance(event, SmcChoch)))


def detect_order_block_fake_breakout(
    candles: List[OHLCV],
    smc: SmcResult,
    direction: str,
    retrace_bars: int = 3,
) -> Optional[SmcOrderBlock]:
    """Return the direction-aligned OB involved in a fake breakout.

    A bullish setup is rejected when a recently closed candle closed above a
    bullish OB and the latest closed candle has returned inside that block.
    The bearish case is the mirror image.  Both active and recently mitigated
    blocks are considered because a return into a block is exactly what marks
    it as mitigated in the SMC detector.

    This is deliberately a hard entry guard, not another confidence bonus or
    penalty.  It only evaluates closed candles and only looks back a small,
    bounded number of bars, so an old OB cannot block a fresh setup by itself.
    """
    if direction not in {"BUY", "SELL"} or len(candles) < 2:
        return None

    current = candles[-1]
    recent_start = max(0, len(candles) - retrace_bars - 1)
    recent = candles[recent_start:-1]
    if not recent:
        return None

    expected_type = "BULLISH" if direction == "BUY" else "BEARISH"
    blocks: List[SmcOrderBlock] = list(smc.order_blocks)
    blocks.extend(m.original_ob for m in smc.mitigation_blocks)

    seen: Set[tuple[str, int]] = set()
    for ob in blocks:
        key = (ob.type, ob.bar_index)
        if key in seen or ob.type != expected_type:
            continue
        seen.add(key)

        current_inside = ob.low <= current.close <= ob.high
        if not current_inside:
            continue

        broke_up = any(c.close > ob.high for c in recent)
        broke_down = any(c.close < ob.low for c in recent)
        midpoint = (ob.high + ob.low) / 2

        # A first retest is the setup we want to trade.  Only classify it as a
        # fake breakout when the retest candle has no directional rejection.
        # A bullish candle closing in the upper half of a bullish block (or the
        # bearish mirror) is valid confirmation, not a failed breakout.
        bullish_rejection = (
            current.close > current.open and current.close >= midpoint
        )
        bearish_rejection = (
            current.close < current.open and current.close <= midpoint
        )
        if direction == "BUY" and broke_up and not bullish_rejection:
            return ob
        if direction == "SELL" and broke_down and not bearish_rejection:
            return ob

    return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _candle_body(c: OHLCV) -> float:
    return abs(c.close - c.open)

def _candle_range(c: OHLCV) -> float:
    return c.high - c.low

def _body_ratio(c: OHLCV) -> float:
    r = _candle_range(c)
    return _candle_body(c) / r if r > 0 else 0.0


# ── Swing point detection ─────────────────────────────────────────────────────

def _detect_swing_highs(candles: List[OHLCV], lookback: int) -> List[int]:
    result = []
    end = len(candles) - lookback
    for i in range(lookback, end):
        h = candles[i].high
        valid = all(
            candles[i - j].high < h and candles[i + j].high < h
            for j in range(1, lookback + 1)
        )
        if valid:
            result.append(i)
    return result


def _detect_swing_lows(candles: List[OHLCV], lookback: int) -> List[int]:
    result = []
    end = len(candles) - lookback
    for i in range(lookback, end):
        lo = candles[i].low
        valid = all(
            candles[i - j].low > lo and candles[i + j].low > lo
            for j in range(1, lookback + 1)
        )
        if valid:
            result.append(i)
    return result


# ── BOS + CHoCH ───────────────────────────────────────────────────────────────

def _detect_bos_and_choch(
    candles: List[OHLCV],
    swing_high_idx: List[int],
    swing_low_idx: List[int],
    cfg: SmcConfig,
):
    bos_signals:   List[SmcBos]   = []
    choch_signals: List[SmcChoch] = []

    used_highs: Set[int] = set()
    used_lows:  Set[int] = set()

    local_trend = "NEUTRAL"
    recent_bos_dir: List[str] = []
    choch_body_ratio = cfg.min_bos_body_ratio * 0.8

    for i in range(cfg.swing_lookback, len(candles)):
        c  = candles[i]
        br = _body_ratio(c)

        # Most recent unbroken swing high before bar i
        recent_sh_pos = -1
        for k in range(len(swing_high_idx) - 1, -1, -1):
            if swing_high_idx[k] < i and k not in used_highs:
                recent_sh_pos = k
                break

        # Most recent unbroken swing low before bar i
        recent_sl_pos = -1
        for k in range(len(swing_low_idx) - 1, -1, -1):
            if swing_low_idx[k] < i and k not in used_lows:
                recent_sl_pos = k
                break

        # M-3 FIX: one structural break per bar — bullish checked first,
        # bearish skipped if bullish already fired (prevents BUY+SELL dual
        # signals on wide news candles that pierce both a swing high and low).
        bar_broke = False

        # Bullish break
        if recent_sh_pos >= 0:
            sh_price   = candles[swing_high_idx[recent_sh_pos]].high
            break_dist = c.close - sh_price
            min_body   = choch_body_ratio if local_trend == "BEARISH" else cfg.min_bos_body_ratio
            if break_dist >= cfg.min_break_distance and br >= min_body:
                if local_trend == "BEARISH":
                    choch_signals.append(SmcChoch("BUY", round(sh_price, 2), i, c.time))
                    local_trend = "NEUTRAL"
                    recent_bos_dir.clear()
                else:
                    bos_signals.append(SmcBos("BUY", round(sh_price, 2), i, c.time))
                    recent_bos_dir.append("BUY")
                    if len(recent_bos_dir) >= 2 and all(d == "BUY" for d in recent_bos_dir[-2:]):
                        local_trend = "BULLISH"
                used_highs.add(recent_sh_pos)
                bar_broke = True

        # Bearish break (skipped if bullish break already registered this bar)
        if not bar_broke and recent_sl_pos >= 0:
            sl_price   = candles[swing_low_idx[recent_sl_pos]].low
            break_dist = sl_price - c.close
            min_body   = choch_body_ratio if local_trend == "BULLISH" else cfg.min_bos_body_ratio
            if break_dist >= cfg.min_break_distance and br >= min_body:
                if local_trend == "BULLISH":
                    choch_signals.append(SmcChoch("SELL", round(sl_price, 2), i, c.time))
                    local_trend = "NEUTRAL"
                    recent_bos_dir.clear()
                else:
                    bos_signals.append(SmcBos("SELL", round(sl_price, 2), i, c.time))
                    recent_bos_dir.append("SELL")
                    if len(recent_bos_dir) >= 2 and all(d == "SELL" for d in recent_bos_dir[-2:]):
                        local_trend = "BEARISH"
                used_lows.add(recent_sl_pos)

    return (
        bos_signals[-cfg.max_bos:],
        choch_signals[-cfg.max_choch:],
        local_trend,
    )


# ── Order Blocks ──────────────────────────────────────────────────────────────

def _detect_order_blocks(
    candles: List[OHLCV],
    bos_signals: List[SmcBos],
    cfg: SmcConfig,
):
    order_blocks:      List[SmcOrderBlock]      = []
    mitigation_blocks: List[SmcMitigationBlock] = []
    used_ob_idx: Set[int] = set()

    for bos in bos_signals:
        max_look = min(8, bos.bar_index)
        ob_idx   = -1

        if bos.type == "BUY":
            for k in range(bos.bar_index - 1, bos.bar_index - max_look - 1, -1):
                if k < 0:
                    break
                cand = candles[k]
                if (cand.close < cand.open and
                        _candle_body(cand) >= cfg.min_ob_body_size and
                        _body_ratio(cand) >= cfg.min_ob_body_ratio):
                    ob_idx = k
                    break
        else:
            for k in range(bos.bar_index - 1, bos.bar_index - max_look - 1, -1):
                if k < 0:
                    break
                cand = candles[k]
                if (cand.close > cand.open and
                        _candle_body(cand) >= cfg.min_ob_body_size and
                        _body_ratio(cand) >= cfg.min_ob_body_ratio):
                    ob_idx = k
                    break

        if ob_idx < 0 or ob_idx in used_ob_idx:
            continue
        used_ob_idx.add(ob_idx)

        raw = candles[ob_idx]
        ob  = SmcOrderBlock(
            type="BULLISH" if bos.type == "BUY" else "BEARISH",
            high=round(raw.high, 2), low=round(raw.low, 2),
            open=round(raw.open, 2), close=round(raw.close, 2),
            bar_index=ob_idx, time=raw.time, mitigated=False,
            mitigation_state="FRESH",
        )

        # Mitigation check.  A first touch is a retest entry opportunity, not
        # an invalidated block.  Keep the block in the result so scoring,
        # telemetry, and capital placement can use the retest zone.
        for j in range(bos.bar_index + 1, len(candles)):
            if candles[j].low <= ob.high and candles[j].high >= ob.low:
                ob.mitigated = True
                ob.mitigation_state = "RETESTED"
                mitigation_blocks.append(SmcMitigationBlock(
                    original_ob=ob,
                    mitigated_at_bar_index=j,
                    mitigated_at_time=candles[j].time,
                ))
                break
        order_blocks.append(ob)

    # Do not discard retested blocks: the first mitigation is where an OB
    # entry is normally expected.  Fake-breakout protection decides whether
    # the current retest candle is valid.
    return order_blocks[-cfg.max_order_blocks:], mitigation_blocks[-cfg.max_order_blocks:]


# ── Fair Value Gaps ───────────────────────────────────────────────────────────

def _detect_fvgs(candles: List[OHLCV], cfg: SmcConfig) -> List[SmcFvg]:
    fvgs: List[SmcFvg] = []
    if len(candles) < 3:
        return fvgs
    for i in range(2, len(candles)):
        c1 = candles[i - 2]
        c3 = candles[i]

        # Bullish FVG: gap between c1.high and c3.low
        if c1.high < c3.low and (c3.low - c1.high) >= cfg.fvg_min_size:
            touched = any(
                candles[j].low <= c3.low and candles[j].high >= c1.high
                for j in range(i + 1, len(candles))
            )
            # A touch/overlap is a partial fill, not a full fill.  Require
            # price to reach the far edge of the gap before removing it.
            filled = any(candles[j].low <= c1.high
                         for j in range(i + 1, len(candles)))
            state = "FULL" if filled else "PARTIAL" if touched else "UNTOUCHED"
            fvgs.append(SmcFvg(
                "BULLISH", round(c3.low, 2), round(c1.high, 2),
                i - 1, candles[i - 1].time, filled, state,
            ))

        # Bearish FVG: gap between c3.high and c1.low
        if c1.low > c3.high and (c1.low - c3.high) >= cfg.fvg_min_size:
            touched = any(
                candles[j].high >= c3.high and candles[j].low <= c1.low
                for j in range(i + 1, len(candles))
            )
            filled = any(candles[j].high >= c1.low
                         for j in range(i + 1, len(candles)))
            state = "FULL" if filled else "PARTIAL" if touched else "UNTOUCHED"
            fvgs.append(SmcFvg(
                "BEARISH", round(c1.low, 2), round(c3.high, 2),
                i - 1, candles[i - 1].time, filled, state,
            ))

    return [g for g in fvgs if not g.filled][-cfg.max_fvgs:]


# ── Liquidity Sweeps ──────────────────────────────────────────────────────────

def _detect_liquidity_sweeps(
    candles: List[OHLCV],
    swing_high_idx: List[int],
    swing_low_idx: List[int],
    cfg: SmcConfig,
) -> List[SmcLiquiditySweep]:
    sweeps: List[SmcLiquiditySweep] = []

    for i in range(cfg.swing_lookback, len(candles)):
        c = candles[i]

        # Bullish sweep: wick below swing low, close above level
        for sl_idx in reversed([idx for idx in swing_low_idx if i - cfg.swing_lookback <= idx < i]):
            level = candles[sl_idx].low
            if (level - c.low  >= cfg.liquidity_sweep_min and
                    c.close - level >= cfg.min_sweep_close_margin and
                    c.close > c.open):
                if not any(s.type == "BULLISH" and abs(s.swept_level - level) < 0.05
                           for s in sweeps):
                    sweeps.append(SmcLiquiditySweep(
                        "BULLISH", round(level, 2), round(c.low, 2), i, c.time))
                break

        # Bearish sweep: wick above swing high, close below level
        for sh_idx in reversed([idx for idx in swing_high_idx if i - cfg.swing_lookback <= idx < i]):
            level = candles[sh_idx].high
            if (c.high - level  >= cfg.liquidity_sweep_min and
                    level - c.close >= cfg.min_sweep_close_margin and
                    c.close < c.open):
                if not any(s.type == "BEARISH" and abs(s.swept_level - level) < 0.05
                           for s in sweeps):
                    sweeps.append(SmcLiquiditySweep(
                        "BEARISH", round(level, 2), round(c.high, 2), i, c.time))
                break

    return sweeps[-cfg.max_sweeps:]


# ── Equal Levels ──────────────────────────────────────────────────────────────

def _detect_equal_levels(
    candles: List[OHLCV],
    swing_high_idx: List[int],
    swing_low_idx: List[int],
    cfg: SmcConfig,
):
    tol = cfg.equal_level_tolerance

    def group_levels(indices: List[int], get_val, level_type: str) -> List[SmcEqualLevel]:
        levels: List[SmcEqualLevel] = []
        processed: Set[int] = set()
        for i in range(len(indices)):
            if i in processed:
                continue
            base_price = get_val(indices[i])
            group = [i]
            for j in range(i + 1, len(indices)):
                if j not in processed and abs(get_val(indices[j]) - base_price) <= tol:
                    group.append(j)
                    processed.add(j)
            if len(group) >= 2:
                avg_p  = sum(get_val(indices[k]) for k in group) / len(group)
                last_k = group[-1]
                levels.append(SmcEqualLevel(
                    type=level_type,  # type: ignore
                    price=round(avg_p, 2),
                    bar_indices=[indices[k] for k in group],
                    time=candles[indices[last_k]].time,
                ))
            processed.add(i)
        return levels

    equal_highs = group_levels(swing_high_idx, lambda i: candles[i].high, "HIGH")
    equal_lows  = group_levels(swing_low_idx,  lambda i: candles[i].low,  "LOW")
    return equal_highs, equal_lows


# ── Composite SMC signal (SMC data only) ──────────────────────────────────────

def _compute_smc_signal(
    trend: str,
    bos_signals: List[SmcBos],
    choch_signals: List[SmcChoch],
    order_blocks: List[SmcOrderBlock],
    fvgs: List[SmcFvg],
    sweeps: List[SmcLiquiditySweep],
    current_price: float,
    cfg: SmcConfig,
):
    buy_score  = 0.0
    sell_score = 0.0

    # Structural trend
    if trend == "BULLISH":  buy_score  += 2
    elif trend == "BEARISH": sell_score += 2

    # BOS (most recent)
    if bos_signals:
        last = bos_signals[-1]
        if last.type == "BUY":  buy_score  += 2
        else:                    sell_score += 2

    # CHoCH (strongest signal)
    if choch_signals:
        last = choch_signals[-1]
        if last.type == "BUY":  buy_score  += 3
        else:                    sell_score += 3

    # Order Blocks near price (max 2)
    ob_hits = 0
    for ob in order_blocks:
        if ob_hits >= 2:
            break
        in_zone   = ob.low <= current_price <= ob.high
        near_zone = min(
            abs(current_price - ob.low),
            abs(current_price - ob.high),
        ) <= cfg.near_ob_threshold
        if in_zone or near_zone:
            if ob.type == "BULLISH": buy_score  += 2
            else:                     sell_score += 2
            ob_hits += 1

    # FVGs near price (max 2)
    fvg_hits = 0
    for fvg in fvgs:
        if fvg_hits >= 2:
            break
        near = (fvg.bottom - cfg.near_fvg_threshold <= current_price
                <= fvg.top + cfg.near_fvg_threshold)
        if near:
            if fvg.type == "BULLISH": buy_score  += 1
            else:                      sell_score += 1
            fvg_hits += 1

    # Liquidity sweep (most recent)
    if sweeps:
        last = sweeps[-1]
        if last.type == "BULLISH": buy_score  += 2
        else:                       sell_score += 2

    buy_score  = max(0.0, buy_score)
    sell_score = max(0.0, sell_score)

    # A stale BOS must not authorize a trade directly into the opposing live
    # zone.  Prefer neutrality over a structurally stale signal; the range
    # playbook can then wait for a directionally confirmed reversal.
    bullish_zone = any(
        ob.type == "BULLISH"
        and ob.low - cfg.near_ob_threshold <= current_price
        <= ob.high + cfg.near_ob_threshold
        for ob in order_blocks
    ) or any(
        fvg.type == "BULLISH"
        and fvg.bottom - cfg.near_fvg_threshold <= current_price
        <= fvg.top + cfg.near_fvg_threshold
        for fvg in fvgs
    )
    bearish_zone = any(
        ob.type == "BEARISH"
        and ob.low - cfg.near_ob_threshold <= current_price
        <= ob.high + cfg.near_ob_threshold
        for ob in order_blocks
    ) or any(
        fvg.type == "BEARISH"
        and fvg.bottom - cfg.near_fvg_threshold <= current_price
        <= fvg.top + cfg.near_fvg_threshold
        for fvg in fvgs
    )
    if bullish_zone and not bearish_zone:
        sell_score = 0.0
    elif bearish_zone and not bullish_zone:
        buy_score = 0.0

    max_possible = 12.0
    dominant     = max(buy_score, sell_score)
    net_score    = round(dominant / max_possible, 2)

    smc_signal = "NEUTRAL"
    if buy_score > sell_score and net_score >= 0.35:
        smc_signal = "BUY"
    elif sell_score > buy_score and net_score >= 0.35:
        smc_signal = "SELL"

    return smc_signal, min(1.0, net_score)


# ── Main entry point ──────────────────────────────────────────────────────────

def analyze_smc_structure(candles: List[OHLCV], timeframe: str = "M5") -> SmcResult:
    cfg = _config_for_timeframe(timeframe)
    normalized_timeframe = str(timeframe or "M5")
    min_required = cfg.swing_lookback * 2 + 10

    _empty = SmcResult(
        timeframe=normalized_timeframe,
        timestamp=candles[-1].time if candles else "",
        current_price=candles[-1].close if candles else 0.0,
        trend="NEUTRAL",
        bos_signals=[], choch_signals=[], order_blocks=[],
        fair_value_gaps=[], liquidity_sweeps=[],
        equal_highs=[], equal_lows=[], mitigation_blocks=[],
        smc_signal="NEUTRAL", smc_score=0.0,
    )

    if len(candles) < min_required:
        return _empty

    swing_high_idx = _detect_swing_highs(candles, cfg.swing_lookback)
    swing_low_idx  = _detect_swing_lows(candles,  cfg.swing_lookback)

    bos, choch, trend = _detect_bos_and_choch(
        candles, swing_high_idx, swing_low_idx, cfg)

    obs, mit_blocks = _detect_order_blocks(candles, bos, cfg)
    fvgs            = _detect_fvgs(candles, cfg)
    sweeps          = _detect_liquidity_sweeps(
        candles, swing_high_idx, swing_low_idx, cfg)
    eq_highs, eq_lows = _detect_equal_levels(
        candles, swing_high_idx, swing_low_idx, cfg)

    current_price = candles[-1].close
    smc_signal, smc_score = _compute_smc_signal(
        trend, bos, choch, obs, fvgs, sweeps, current_price, cfg)

    return SmcResult(
        timeframe=normalized_timeframe, timestamp=candles[-1].time,
        current_price=current_price,
        trend=trend,  # type: ignore
        bos_signals=bos, choch_signals=choch,
        order_blocks=obs, fair_value_gaps=fvgs,
        liquidity_sweeps=sweeps, equal_highs=eq_highs, equal_lows=eq_lows,
        mitigation_blocks=mit_blocks,
        smc_signal=smc_signal,  # type: ignore
        smc_score=smc_score,
    )

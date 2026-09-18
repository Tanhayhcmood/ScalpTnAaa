"""
Price Action Engine — Patterns, S/R Levels, Breakouts
Ported from priceActionEngine.ts — confirmation only.
"""
from dataclasses import dataclass, replace
from typing import List, Literal
from live_trading.signals.gold_engine import OHLCV
from live_trading.config import PA_MAX_BREAKOUT_EXTENSION_ATR


@dataclass
class PaConfig:
    pin_bar_wick_ratio: float
    pin_bar_body_max_ratio: float
    strong_body_ratio: float
    strong_body_atr_mult: float
    engulf_body_ratio: float
    level_lookback: int
    level_tolerance: float
    min_level_touches: int
    breakout_min_body: float
    breakout_body_ratio: float
    fake_retrace_bars: int
    pullback_zone_pct: float
    atr_period: int


CFG_M5 = PaConfig(
    pin_bar_wick_ratio=2.0, pin_bar_body_max_ratio=0.30,
    strong_body_ratio=0.60, strong_body_atr_mult=0.40,
    engulf_body_ratio=1.05,
    level_lookback=30, level_tolerance=0.35, min_level_touches=2,
    breakout_min_body=0.20, breakout_body_ratio=0.50,
    fake_retrace_bars=3, pullback_zone_pct=0.0015, atr_period=10,
)


@dataclass
class PriceActionResult:
    bullish_engulf: bool
    bearish_engulf: bool
    bullish_pin_bar: bool
    bearish_pin_bar: bool
    strong_bullish: bool
    strong_bearish: bool
    near_demand_zone: bool
    near_supply_zone: bool
    near_support: bool
    near_resistance: bool
    valid_bull_breakout: bool
    valid_bear_breakout: bool
    fake_bull_breakout: bool
    fake_bear_breakout: bool
    bullish_pullback: bool
    bearish_pullback: bool
    pa_signal: Literal["BUY", "SELL", "NEUTRAL"]
    pa_score: float
    # Additional continuation pattern flags are additive telemetry.  Defaults
    # keep older panel/test constructors source-compatible.
    bullish_inside_breakout: bool = False
    bearish_inside_breakout: bool = False
    inside_bar_detected: bool = False
    inside_bar_depth: int = 0
    inside_bar_breakout_level: float | None = None
    inside_bar_breakout_strength: float = 0.0
    # Entry-quality telemetry for breakout chasing protection.
    breakout_level: float | None = None
    breakout_extension_atr: float = 0.0
    breakout_overextended: bool = False


def _calc_atr(candles: List[OHLCV], period: int) -> float:
    sl = candles[-(period + 1):]
    total = 0.0
    for i in range(1, len(sl)):
        c, p = sl[i], sl[i - 1]
        total += max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
    return total / period if period > 0 else 0.0


def _body(c: OHLCV) -> float: return abs(c.close - c.open)
def _range(c: OHLCV) -> float: return c.high - c.low
def _body_ratio(c: OHLCV) -> float: r = _range(c); return _body(c) / r if r > 0 else 0.0
def _is_bull(c: OHLCV) -> bool: return c.close > c.open
def _is_bear(c: OHLCV) -> bool: return c.close < c.open
def _upper_wick(c: OHLCV) -> float: return c.high - max(c.open, c.close)
def _lower_wick(c: OHLCV) -> float: return min(c.open, c.close) - c.low


def _detect_patterns(candles: List[OHLCV], cfg: PaConfig, atr: float):
    n  = len(candles)
    c0 = candles[n - 1]
    c1 = candles[n - 2]

    c0b, c1b = _body(c0), _body(c1)

    bull_engulf = (_is_bull(c0) and _is_bear(c1) and
                   c0.open <= c1.close and c0.close >= c1.open and
                   c0b >= c1b * cfg.engulf_body_ratio)
    bear_engulf = (_is_bear(c0) and _is_bull(c1) and
                   c0.open >= c1.close and c0.close <= c1.open and
                   c0b >= c1b * cfg.engulf_body_ratio)

    r0      = _range(c0)
    min_sz  = atr * 0.25

    bull_pin = (r0 >= min_sz and
                _body_ratio(c0) <= cfg.pin_bar_body_max_ratio and
                _lower_wick(c0) >= c0b * cfg.pin_bar_wick_ratio and
                _upper_wick(c0) <= _lower_wick(c0) * 0.4)
    bear_pin = (r0 >= min_sz and
                _body_ratio(c0) <= cfg.pin_bar_body_max_ratio and
                _upper_wick(c0) >= c0b * cfg.pin_bar_wick_ratio and
                _lower_wick(c0) <= _upper_wick(c0) * 0.4)

    top_of_range    = c0.low + r0 * 0.80
    bottom_of_range = c0.low + r0 * 0.20

    strong_bull = (_is_bull(c0) and
                   _body_ratio(c0) >= cfg.strong_body_ratio and
                   c0b >= atr * cfg.strong_body_atr_mult and
                   c0.close >= top_of_range)
    strong_bear = (_is_bear(c0) and
                   _body_ratio(c0) >= cfg.strong_body_ratio and
                   c0b >= atr * cfg.strong_body_atr_mult and
                   c0.close <= bottom_of_range)

    return bull_engulf, bear_engulf, bull_pin, bear_pin, strong_bull, strong_bear


def _detect_sr_levels(candles: List[OHLCV], cfg: PaConfig, tolerance: float | None = None):
    sl = candles[-cfg.level_lookback:]
    n  = len(sl)
    tol = cfg.level_tolerance if tolerance is None else tolerance

    swing_highs, swing_lows = [], []
    for i in range(2, n - 2):
        if (sl[i].high > sl[i-1].high and sl[i].high > sl[i-2].high and
                sl[i].high > sl[i+1].high and sl[i].high > sl[i+2].high):
            swing_highs.append(sl[i].high)
        if (sl[i].low < sl[i-1].low and sl[i].low < sl[i-2].low and
                sl[i].low < sl[i+1].low and sl[i].low < sl[i+2].low):
            swing_lows.append(sl[i].low)

    def cluster(prices):
        confirmed, used = [], set()
        for i, p in enumerate(prices):
            if i in used: continue
            grp = [p]
            for j in range(i + 1, len(prices)):
                if j not in used and abs(prices[j] - p) <= tol:
                    grp.append(prices[j])
                    used.add(j)
            used.add(i)
            if len(grp) >= cfg.min_level_touches:
                confirmed.append(sum(grp) / len(grp))
        return confirmed

    return cluster(swing_lows), cluster(swing_highs)


def _detect_supply_demand(candles: List[OHLCV], cfg: PaConfig, atr: float):
    sl = candles[-cfg.level_lookback:]
    n  = len(sl)
    demand_zones, supply_zones = [], []

    for i in range(3, n - 1):
        impulse   = sl[i]
        base_cands = sl[i - 3:i]
        base_high  = max(c.high for c in base_cands)
        base_low   = min(c.low  for c in base_cands)
        base_range = base_high - base_low
        if base_range > atr * 1.5: continue

        imp_b = _body(impulse)
        imp_r = _range(impulse)
        is_strong = (imp_r > 0 and
                     imp_b / imp_r >= cfg.strong_body_ratio and
                     imp_b >= atr * 0.5)
        if not is_strong: continue

        if _is_bull(impulse):
            demand_zones.append({"top": round(base_high, 2), "bottom": round(base_low, 2)})
        elif _is_bear(impulse):
            supply_zones.append({"top": round(base_high, 2), "bottom": round(base_low, 2)})

    return demand_zones[-3:], supply_zones[-3:]


def _detect_inside_bar_breakout(
    candles: List[OHLCV], cfg: PaConfig, atr: float
) -> tuple[bool, int, bool, bool, float | None, float]:
    """Detect a closed breakout from a single or nested inside-bar structure.

    The previous implementation compared the breakout close with the
    *inside* candle's high/low.  That can fire while price is still inside the
    mother candle, which is not a breakout of the setup.  The breakout must
    close beyond the outermost mother candle and meet the same body-quality
    requirements used by the normal breakout detector.

    Returns:
        detected, nesting depth, bullish, bearish, broken level, strength
    """
    n = len(candles)
    if n < 3 or atr <= 0:
        return False, 0, False, False, None, 0.0

    breakout = candles[-1]
    inside_index = n - 2
    mother_index = n - 3
    outer_mother = None
    depth = 0

    # Walk backwards so a sequence such as A (mother), B (inside), C
    # (nested inside), D (breakout) uses A's boundaries rather than C's.
    while inside_index >= 1 and mother_index >= 0:
        inside = candles[inside_index]
        mother = candles[mother_index]
        if not (
            inside.high <= mother.high
            and inside.low >= mother.low
            and _range(inside) > 0
            and _range(mother) > 0
        ):
            break
        depth += 1
        outer_mother = mother
        inside_index -= 1
        mother_index -= 1

    if outer_mother is None:
        return False, 0, False, False, None, 0.0

    minimum_body = max(cfg.breakout_min_body, atr * 0.25)
    body = _body(breakout)
    quality = _body_ratio(breakout) >= cfg.breakout_body_ratio
    bullish = (
        _is_bull(breakout)
        and breakout.close > outer_mother.high
        and body >= minimum_body
        and quality
    )
    bearish = (
        _is_bear(breakout)
        and breakout.close < outer_mother.low
        and body >= minimum_body
        and quality
    )
    broken_level = (
        outer_mother.high if bullish else outer_mother.low if bearish else None
    )
    strength = round(body / atr, 3) if (bullish or bearish) else 0.0
    return True, depth, bullish, bearish, broken_level, strength


def _detect_breakout_pullback(
    candles, support_lvls, resistance_lvls, demand_zones, supply_zones,
    cfg: PaConfig, atr: float
):
    n = len(candles)
    curr = candles[n - 1]
    cp   = curr.close

    vbull = vbear = fbull = fbear = bull_pb = bear_pb = False

    for level in resistance_lvls:
        if (cp > level and _body(curr) >= cfg.breakout_min_body and
                _body_ratio(curr) >= cfg.breakout_body_ratio and _is_bull(curr)):
            vbull = True
        recent = candles[n - cfg.fake_retrace_bars - 1: n - 1]
        for rc in recent:
            if rc.close > level and cp < level: fbull = True

    for level in support_lvls:
        if (cp < level and _body(curr) >= cfg.breakout_min_body and
                _body_ratio(curr) >= cfg.breakout_body_ratio and _is_bear(curr)):
            vbear = True
        recent = candles[n - cfg.fake_retrace_bars - 1: n - 1]
        for rc in recent:
            if rc.close < level and cp > level: fbear = True

    look = min(10, n - 1)
    prior = candles[n - 1 - look].close
    trend_up   = cp > prior * 1.002
    trend_down = cp < prior * 0.998
    pull_zone  = cp * cfg.pullback_zone_pct
    previous = candles[n - 2]

    def touched_support(candle):
        return (
            any(
                candle.low <= level + pull_zone
                and candle.high >= level - pull_zone
                for level in support_lvls
            )
            or any(
                candle.low <= zone["top"] + pull_zone
                and candle.high >= zone["bottom"] - pull_zone
                for zone in demand_zones
            )
        )

    def touched_resistance(candle):
        return (
            any(
                candle.low <= level + pull_zone
                and candle.high >= level - pull_zone
                for level in resistance_lvls
            )
            or any(
                candle.low <= zone["top"] + pull_zone
                and candle.high >= zone["bottom"] - pull_zone
                for zone in supply_zones
            )
        )

    # A pullback is a retest followed by a closed-candle reclaim, not merely a
    # trend candle whose close happens to be near a stale level.
    bullish_reclaim = (
        _is_bull(curr)
        and (
            curr.close > previous.high
            or _body_ratio(curr) >= 0.45
        )
    )
    bearish_reclaim = (
        _is_bear(curr)
        and (
            curr.close < previous.low
            or _body_ratio(curr) >= 0.45
        )
    )

    if trend_up:
        retest = touched_support(previous) or touched_support(curr)
        if retest and bullish_reclaim:
            bull_pb = True

    if trend_down:
        retest = touched_resistance(previous) or touched_resistance(curr)
        if retest and bearish_reclaim:
            bear_pb = True

    (
        inside_detected,
        inside_depth,
        bull_inside,
        bear_inside,
        inside_level,
        inside_strength,
    ) = _detect_inside_bar_breakout(candles, cfg, atr)

    return (
        vbull, vbear, fbull, fbear, bull_pb, bear_pb,
        bull_inside, bear_inside,
        inside_detected, inside_depth, inside_level, inside_strength,
    )


def _compute_pa_signal(
    bull_engulf, bear_engulf, bull_pin, bear_pin, strong_bull, strong_bear,
    vbull, vbear, fbull, fbear, bull_pb, bear_pb,
    bull_inside, bear_inside,
    near_demand, near_supply, near_support, near_resist,
):
    buy = sell = 0.0

    if bull_engulf: buy  += 1.5
    if bear_engulf: sell += 1.5
    if bull_pin:    buy  += 1.5 if (near_support or near_demand) else 0.8
    if bear_pin:    sell += 1.5 if (near_resist or near_supply)  else 0.8
    if strong_bull: buy  += 1.0
    if strong_bear: sell += 1.0
    if vbull:       buy  += 1.0
    if vbear:       sell += 1.0
    if bull_pb:     buy  += 0.8
    if bear_pb:     sell += 0.8
    if bull_inside: buy  += 1.2
    if bear_inside: sell += 1.2
    if near_demand or near_support: buy  += 0.5
    if near_supply or near_resist:  sell += 0.5
    if fbull: buy  -= 1.5
    if fbear: sell -= 1.5

    buy  = max(0.0, buy)
    sell = max(0.0, sell)

    # 5.3 was the old pre-inside-bar ceiling.  Keep the score informative,
    # but do not use an impossible "all patterns must agree" denominator as a
    # hard gate.  A valid engulfing/breakout is itself actionable evidence.
    max_possible = 6.5
    dominant = max(buy, sell)
    score = round(dominant / max_possible, 2)

    # Context alone never creates a signal.  A directional trigger is needed:
    # engulfing, valid breakout, inside-bar breakout, a pullback into a level,
    # or a rejection/strong candle that is actually at a relevant level.
    buy_trigger = (
        bull_engulf or vbull or bull_inside or bull_pb
        or (bull_pin and (near_support or near_demand))
        or (strong_bull and (near_support or near_demand))
    )
    sell_trigger = (
        bear_engulf or vbear or bear_inside or bear_pb
        or (bear_pin and (near_resist or near_supply))
        or (strong_bear and (near_resist or near_supply))
    )

    # A one-point breakout/pullback trigger is allowed at ~15% of the
    # diagnostic ceiling; this is intentionally lower than the old 30% score
    # threshold, which made a standalone valid engulfing (1.5/5.3 = 28%) and
    # many legitimate continuation setups NEUTRAL.
    if buy > sell and buy_trigger and buy >= 1.0 and score >= 0.15:
        signal = "BUY"
    elif sell > buy and sell_trigger and sell >= 1.0 and score >= 0.15:
        signal = "SELL"
    else:
        signal = "NEUTRAL"
    return signal, min(1.0, score)


_NEUTRAL_PA = PriceActionResult(
    bullish_engulf=False, bearish_engulf=False,
    bullish_pin_bar=False, bearish_pin_bar=False,
    strong_bullish=False, strong_bearish=False,
    near_demand_zone=False, near_supply_zone=False,
    near_support=False, near_resistance=False,
    valid_bull_breakout=False, valid_bear_breakout=False,
    fake_bull_breakout=False, fake_bear_breakout=False,
    bullish_pullback=False, bearish_pullback=False,
    pa_signal="NEUTRAL", pa_score=0.0,
)


def _config_for_timeframe(timeframe: str) -> PaConfig:
    """Use a wider structural window on the higher entry timeframes.

    The live loop scans M20/M15/M10/M5, but the old Python port always used
    the M5 30-bar level window.  Pattern math is timeframe-agnostic; level
    context is not.  Higher timeframes need more bars and a slightly wider
    gold-price tolerance to avoid losing valid PA setups simply because the
    level was formed outside the short M5 window.
    """
    tf = str(timeframe or "M5").upper()
    if tf in {"M10", "10M"}:
        return replace(CFG_M5, level_lookback=40, level_tolerance=0.45)
    if tf in {"M15", "15M"}:
        return replace(CFG_M5, level_lookback=45, level_tolerance=0.55)
    if tf in {"M20", "20M"}:
        return replace(CFG_M5, level_lookback=50, level_tolerance=0.65)
    if tf in {"M30", "30M"}:
        return replace(CFG_M5, level_lookback=55, level_tolerance=0.75)
    return CFG_M5


def analyze_price_action(candles: List[OHLCV], timeframe: str = "M5") -> PriceActionResult:
    cfg = _config_for_timeframe(timeframe)
    if len(candles) < cfg.level_lookback + cfg.atr_period + 5:
        return _NEUTRAL_PA

    atr = _calc_atr(candles, cfg.atr_period)
    if atr <= 0:
        return _NEUTRAL_PA

    be, bae, bp, bap, sb, sbe = _detect_patterns(candles, cfg, atr)
    # Gold's intraday range changes materially across sessions.  A static
    # tolerance is too narrow during London/NY volatility, so allow a modest
    # ATR-relative expansion while keeping it bounded by the profile.
    adaptive_tolerance = max(
        cfg.level_tolerance,
        min(0.90, atr * 0.30),
    )
    support_lvls, resistance_lvls = _detect_sr_levels(
        candles, cfg, tolerance=adaptive_tolerance
    )
    demand_zones, supply_zones    = _detect_supply_demand(candles, cfg, atr)

    cp  = candles[-1].close
    tol = adaptive_tolerance
    near_sup  = any(abs(cp - l) <= tol for l in support_lvls)
    near_res  = any(abs(cp - l) <= tol for l in resistance_lvls)
    near_dem  = any(z["bottom"] - tol <= cp <= z["top"] + tol for z in demand_zones)
    near_supl = any(z["bottom"] - tol <= cp <= z["top"] + tol for z in supply_zones)

    (
        vbull, vbear, fbull, fbear, bull_pb, bear_pb,
        bull_inside, bear_inside, inside_detected, inside_depth,
        inside_level, inside_strength,
    ) = _detect_breakout_pullback(
        candles, support_lvls, resistance_lvls, demand_zones, supply_zones, cfg, atr)

    signal, score = _compute_pa_signal(
        be, bae, bp, bap, sb, sbe,
        vbull, vbear, fbull, fbear, bull_pb, bear_pb,
        bull_inside, bear_inside,
        near_dem, near_supl, near_sup, near_res,
    )

    # A breakout signal is only actionable while price is still reasonably
    # close to the broken level.  This does not reject the PA signal itself;
    # the decision engine uses the metadata to avoid chasing an exhausted
    # impulse at market and can wait for a retest instead.
    breakout_level = None
    if bull_inside or bear_inside:
        breakout_level = inside_level
    elif vbull:
        below = [level for level in resistance_lvls if level < cp]
        breakout_level = max(below) if below else None
    elif vbear:
        above = [level for level in support_lvls if level > cp]
        breakout_level = min(above) if above else None
    breakout_extension_atr = 0.0
    if breakout_level is not None and atr > 0:
        breakout_extension_atr = round(
            max(0.0, abs(cp - breakout_level) / atr), 3
        )

    return PriceActionResult(
        bullish_engulf=be, bearish_engulf=bae,
        bullish_pin_bar=bp, bearish_pin_bar=bap,
        strong_bullish=sb, strong_bearish=sbe,
        near_demand_zone=near_dem, near_supply_zone=near_supl,
        near_support=near_sup, near_resistance=near_res,
        valid_bull_breakout=vbull, valid_bear_breakout=vbear,
        fake_bull_breakout=fbull, fake_bear_breakout=fbear,
        bullish_pullback=bull_pb, bearish_pullback=bear_pb,
        pa_signal=signal,  # type: ignore
        pa_score=score,
        bullish_inside_breakout=bull_inside,
        bearish_inside_breakout=bear_inside,
        inside_bar_detected=inside_detected,
        inside_bar_depth=inside_depth,
        inside_bar_breakout_level=inside_level,
        inside_bar_breakout_strength=inside_strength,
        breakout_level=breakout_level,
        breakout_extension_atr=breakout_extension_atr,
        breakout_overextended=(
            breakout_extension_atr
            > PA_MAX_BREAKOUT_EXTENSION_ATR
        ),
    )

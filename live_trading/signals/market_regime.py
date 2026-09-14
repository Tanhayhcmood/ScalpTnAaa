"""
Market Regime Detector — 11 regimes with adaptive entry rules.
Ported from marketRegimeDetector.ts
"""
from dataclasses import dataclass
from typing import Literal
from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.trend_engine import TrendResult
from live_trading.signals.wyckoff_engine import WyckoffResult
from live_trading.config import NORMAL_MIN_CONFIDENCE, RANGE_MIN_CONFIDENCE
from typing import List

MarketRegime = Literal[
    "STRONG_TREND_BULL", "STRONG_TREND_BEAR",
    "WEAK_TREND_BULL",   "WEAK_TREND_BEAR",
    "PULLBACK_BULL",     "PULLBACK_BEAR",
    "RANGE", "ACCUMULATION", "DISTRIBUTION",
    "HIGH_VOLATILITY",   "LOW_VOLATILITY",
]


@dataclass
class RegimeEntryRules:
    min_confidence: float
    min_rr: float
    allow_long: bool
    allow_short: bool
    sl_atr_mult_adjust: float
    label: str


@dataclass
class RegimeResult:
    regime: str
    rules: RegimeEntryRules
    atr: float
    atr_mean: float
    atr_ratio: float
    adx: float
    description: str


# Confidence thresholds are operator-configurable in live_trading.config.
# All non-RANGE regimes use the normal threshold; RANGE uses its dedicated
# threshold.  Risk/reward and directional rules remain regime-specific.
# Marginal setups (conf ≥ CONF_HARD_MIN but < min_conf) can still trade
# via the CONF_MARGINAL_RR path in decision_engine.
REGIME_RULES = {
    #                                           min_conf  min_rr  long   short  sl_mult  label
    "STRONG_TREND_BULL": RegimeEntryRules(NORMAL_MIN_CONFIDENCE, 1.5,  True,  False, 1.0,  "Strong Bull Trend"),
    "STRONG_TREND_BEAR": RegimeEntryRules(NORMAL_MIN_CONFIDENCE, 1.5,  False, True,  1.0,  "Strong Bear Trend"),
    "WEAK_TREND_BULL":   RegimeEntryRules(NORMAL_MIN_CONFIDENCE, 1.5,  True,  False, 0.9,  "Weak Bull Trend"),
    "WEAK_TREND_BEAR":   RegimeEntryRules(NORMAL_MIN_CONFIDENCE, 1.5,  False, True,  0.9,  "Weak Bear Trend"),
    "PULLBACK_BULL":     RegimeEntryRules(NORMAL_MIN_CONFIDENCE, 1.8,  True,  False, 0.95, "Bull Pullback"),
    "PULLBACK_BEAR":     RegimeEntryRules(NORMAL_MIN_CONFIDENCE, 1.8,  False, True,  0.95, "Bear Pullback"),
    "RANGE":             RegimeEntryRules(RANGE_MIN_CONFIDENCE,  2.0,  True,  True,   0.8,  "Range / Choppy"),
    "ACCUMULATION":      RegimeEntryRules(NORMAL_MIN_CONFIDENCE, 1.8,  True,  False, 1.0,  "Wyckoff Accumulation"),
    "DISTRIBUTION":      RegimeEntryRules(NORMAL_MIN_CONFIDENCE, 1.8,  False, True,  1.0,  "Wyckoff Distribution"),
    "HIGH_VOLATILITY":   RegimeEntryRules(NORMAL_MIN_CONFIDENCE, 2.0,  True,  True,   1.3,  "High Volatility"),
    "LOW_VOLATILITY":    RegimeEntryRules(NORMAL_MIN_CONFIDENCE, 1.5,  True,  True,   0.7,  "Low Volatility / Squeeze"),
}


def _calc_atr_values(candles: List[OHLCV], period: int = 20):
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    if not trs:
        return 0.0, 0.0, 1.0
    # Use a 5-bar average for the "current" ATR instead of the single last TR.
    # A single-bar TR spike (e.g. from a news wick) would otherwise instantly
    # flip the regime to HIGH_VOLATILITY and block valid trade entries for the
    # entire bar.  A 5-bar average still reacts quickly to real volatility
    # expansions while filtering one-candle outliers.
    short_window = min(5, len(trs))
    atr      = sum(trs[-short_window:]) / short_window
    atr_mean = sum(trs[-period:]) / min(period, len(trs))
    atr_ratio = round(atr / atr_mean, 3) if atr_mean > 0 else 1.0
    return atr, atr_mean, atr_ratio


def calc_adx(candles: List[OHLCV], period: int = 14) -> float:
    if len(candles) < period * 2:
        return 20.0  # conservative non-trending default when data is insufficient
    n = len(candles)
    trs, dm_p, dm_m = [], [], []
    for i in range(1, n):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
        up = c.high - p.high
        dn = p.low  - c.low
        dm_p.append(up if up > dn and up > 0 else 0.0)
        dm_m.append(dn if dn > up and dn > 0 else 0.0)
    s_tr = sum(trs[:period])
    s_dp = sum(dm_p[:period])
    s_dm = sum(dm_m[:period])
    dx_arr = []
    for i in range(period, len(trs)):
        s_tr = s_tr - s_tr / period + trs[i]
        s_dp = s_dp - s_dp / period + dm_p[i]
        s_dm = s_dm - s_dm / period + dm_m[i]
        di_p = 100 * s_dp / s_tr if s_tr > 0 else 0
        di_m = 100 * s_dm / s_tr if s_tr > 0 else 0
        total = di_p + di_m
        dx_arr.append(100 * abs(di_p - di_m) / total if total > 0 else 0)
    if len(dx_arr) < period:
        return 20.0  # conservative non-trending default
    return round(sum(dx_arr[-period:]) / period, 2)


def _detect_pullback(candles: List[OHLCV], trend: TrendResult):
    if len(candles) < 10:
        return None
    now  = candles[-1].close
    prev = candles[-6].close
    st_bull = now > prev * 1.0003
    st_bear = now < prev * 0.9997
    if trend.trend == "BULLISH" and st_bear: return "BULL"
    if trend.trend == "BEARISH" and st_bull: return "BEAR"
    return None


def detect_market_regime(
    candles: List[OHLCV],
    trend: TrendResult,
    wyckoff: WyckoffResult,
    use_atr_high_vol: bool = False,
) -> RegimeResult:
    atr, atr_mean, atr_ratio = _calc_atr_values(candles, 20)
    adx = calc_adx(candles, 14)

    def make(regime: str, desc: str) -> RegimeResult:
        return RegimeResult(
            regime=regime, rules=REGIME_RULES[regime],
            atr=atr, atr_mean=atr_mean, atr_ratio=atr_ratio,
            adx=adx, description=desc,
        )

    if use_atr_high_vol and atr_ratio > 1.8:
        return make("HIGH_VOLATILITY", f"ATR {atr_ratio:.2f}× above mean")

    if atr_ratio < 0.60 and adx < 20:
        return make("LOW_VOLATILITY", f"ATR at {atr_ratio*100:.0f}% of mean + ADX {adx}")

    if adx >= 30 and trend.strength == "STRONG":
        if trend.trend == "BULLISH":
            return make("STRONG_TREND_BULL", f"ADX {adx} — all EMAs aligned bull")
        if trend.trend == "BEARISH":
            return make("STRONG_TREND_BEAR", f"ADX {adx} — all EMAs aligned bear")

    pull = _detect_pullback(candles, trend)
    if pull == "BULL" and adx >= 20:
        return make("PULLBACK_BULL", "Bear retracement within bull trend")
    if pull == "BEAR" and adx >= 20:
        return make("PULLBACK_BEAR", "Bull retracement within bear trend")

    if adx >= 20 and trend.trend != "NEUTRAL":
        if trend.trend == "BULLISH":
            return make("WEAK_TREND_BULL", f"ADX {adx} — developing bull trend")
        return make("WEAK_TREND_BEAR", f"ADX {adx} — developing bear trend")

    if wyckoff.phase == "ACCUMULATION":
        return make("ACCUMULATION", "Wyckoff Accumulation" +
                    (" + Spring" if wyckoff.spring else ""))
    if wyckoff.phase == "DISTRIBUTION":
        return make("DISTRIBUTION", "Wyckoff Distribution" +
                    (" + Upthrust" if wyckoff.upthrust else ""))

    return make("RANGE", f"ADX {adx} < 20 — ranging / choppy")

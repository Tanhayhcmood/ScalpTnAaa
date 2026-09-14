// Market Regime Detector — XAUUSD Scalping (v4.0)
//
// Classifies current market state into one of 10 regimes.
// Each regime carries its own adaptive entry rules:
//   • minConfidence — Confidence % floor (replaces fixed score thresholds)
//   • minRR         — Minimum risk:reward ratio for the regime
//   • allowLong/allowShort — Direction bias (e.g. ACCUMULATION → long only)
//   • slAtrMultAdjust — ATR multiplier for SL width adjustment
//
// Regime priority (highest → lowest):
//   1. HIGH_VOLATILITY   ATR spike > 1.8× mean (overrides trend classification)
//   2. LOW_VOLATILITY    ATR < 0.60× mean + ADX < 20 (pre-breakout squeeze)
//   3. STRONG_TREND_*    ADX ≥ 30 + all three EMAs aligned
//   4. PULLBACK_*        Short-term retracement within established trend
//   5. WEAK_TREND_*      ADX 20–30, directional EMA alignment
//   6. ACCUMULATION      Wyckoff demand phase (BUY signal from Wyckoff engine)
//   7. DISTRIBUTION      Wyckoff supply phase (SELL signal from Wyckoff engine)
//   8. RANGE             Default: ADX < 20, no directional signal

import { OHLCV } from './goldEngine.js';
import type { TrendResult } from './trendEngine.js';
import type { WyckoffResult } from './wyckoffEngine.js';

// ===== TYPES =====

export type MarketRegime =
  | 'STRONG_TREND_BULL'
  | 'STRONG_TREND_BEAR'
  | 'WEAK_TREND_BULL'
  | 'WEAK_TREND_BEAR'
  | 'PULLBACK_BULL'
  | 'PULLBACK_BEAR'
  | 'RANGE'
  | 'ACCUMULATION'
  | 'DISTRIBUTION'
  | 'HIGH_VOLATILITY'
  | 'LOW_VOLATILITY';

export interface RegimeEntryRules {
  /** Minimum Confidence % required to open a trade in this regime */
  minConfidence: number;
  /**
   * Minimum R:R ratio.  Trades with R:R below this are skipped even when
   * confidence is above the floor — the payoff doesn't justify the risk.
   */
  minRR: number;
  allowLong:        boolean;
  allowShort:       boolean;
  /**
   * Multiplier applied to the Capital Manager's ATR-derived SL distance.
   * > 1 = wider stop (needed in volatile regimes); < 1 = tighter (range).
   */
  slAtrMultAdjust: number;
  /** Human-readable label for logging / UI */
  label: string;
}

export interface RegimeResult {
  regime:       MarketRegime;
  rules:        RegimeEntryRules;
  atr:          number;   // most-recent bar ATR (True Range)
  atrMean:      number;   // 20-bar average ATR
  atrRatio:     number;   // atr / atrMean  (1.0 = normal)
  adx:          number;   // Wilder ADX (14-period)
  description:  string;
}

// ===== REGIME RULES TABLE =====
//
// Rationale:
//   STRONG_TREND_* — institutional momentum is clear; lower bar to enter with flow.
//   WEAK_TREND_*   — directional but fragile; need higher confidence to avoid traps.
//   PULLBACK_*     — continuation entry; direction bias enforced (no counter-trend).
//   RANGE          — no directional edge; very high bar and tight SL for mean-reversion.
//   ACCUMULATION   — Wyckoff demand absorption; only BUY (institutions loading).
//   DISTRIBUTION   — Wyckoff supply absorption; only SELL (institutions distributing).
//   HIGH_VOLATILITY — erratic price; allow both sides but demand superior quality.
//   LOW_VOLATILITY  — pre-breakout squeeze; worst trade location, maximum bar.

// ── Calibrated minConfidence values ─────────────────────────────────────────
//
// Original design assumed synthetic/tick data where the confidence engine
// routinely reaches 85–96%.  On real XAUUSD M5 data the empirical maximum
// is 84.4%.  All minConfidence values are scaled by the factor 70/85 ≈ 0.824
// (70 = new hard minimum, 85 = original hard minimum), preserving the relative
// selectivity ordering between regimes while working within the real data range.
//
// Original → Scaled:
//   STRONG_TREND 88 → 73  |  WEAK_TREND  92 → 76  |  PULLBACK  90 → 74
//   ACCUMULATION 91 → 75  |  DISTRIBUTION 91 → 75 |  RANGE    96 → 79
//   HIGH_VOL     93 → 77  |  LOW_VOL      96 → 79
//
// The marginal R:R path (conf ≥ CONF_HARD_MIN but < regime.minConfidence,
// requires R:R ≥ 2.0) is preserved for bars that clear the hard minimum but
// fall short of the regime's ideal threshold.

const REGIME_RULES: Record<MarketRegime, RegimeEntryRules> = {
  STRONG_TREND_BULL: { minConfidence: 73, minRR: 1.5, allowLong: true,  allowShort: false, slAtrMultAdjust: 1.0,  label: 'Strong Bull Trend'         },
  STRONG_TREND_BEAR: { minConfidence: 73, minRR: 1.5, allowLong: false, allowShort: true,  slAtrMultAdjust: 1.0,  label: 'Strong Bear Trend'         },
  WEAK_TREND_BULL:   { minConfidence: 76, minRR: 2.0, allowLong: true,  allowShort: false, slAtrMultAdjust: 0.9,  label: 'Weak Bull Trend'           },
  WEAK_TREND_BEAR:   { minConfidence: 76, minRR: 2.0, allowLong: false, allowShort: true,  slAtrMultAdjust: 0.9,  label: 'Weak Bear Trend'           },
  PULLBACK_BULL:     { minConfidence: 74, minRR: 1.8, allowLong: true,  allowShort: false, slAtrMultAdjust: 0.95, label: 'Bull Pullback'             },
  PULLBACK_BEAR:     { minConfidence: 74, minRR: 1.8, allowLong: false, allowShort: true,  slAtrMultAdjust: 0.95, label: 'Bear Pullback'             },
  RANGE:             { minConfidence: 79, minRR: 2.5, allowLong: true,  allowShort: true,  slAtrMultAdjust: 0.8,  label: 'Range / Choppy'           },
  ACCUMULATION:      { minConfidence: 75, minRR: 1.5, allowLong: true,  allowShort: false, slAtrMultAdjust: 1.0,  label: 'Wyckoff Accumulation'      },
  DISTRIBUTION:      { minConfidence: 75, minRR: 1.5, allowLong: false, allowShort: true,  slAtrMultAdjust: 1.0,  label: 'Wyckoff Distribution'      },
  HIGH_VOLATILITY:   { minConfidence: 77, minRR: 2.0, allowLong: true,  allowShort: true,  slAtrMultAdjust: 1.3,  label: 'High Volatility'          },
  LOW_VOLATILITY:    { minConfidence: 79, minRR: 2.5, allowLong: true,  allowShort: true,  slAtrMultAdjust: 0.7,  label: 'Low Volatility / Squeeze' },
};

// ===== HELPERS =====

function calcATRValues(
  candles: OHLCV[],
  period = 20,
): { atr: number; atrMean: number; atrRatio: number } {
  const trs: number[] = [];
  for (let i = 1; i < candles.length; i++) {
    const c = candles[i], p = candles[i - 1];
    trs.push(Math.max(c.high - c.low, Math.abs(c.high - p.close), Math.abs(c.low - p.close)));
  }
  if (trs.length === 0) return { atr: 0, atrMean: 0, atrRatio: 1 };

  const atr     = trs[trs.length - 1];
  const slice   = trs.slice(-period);
  const atrMean = slice.reduce((a, b) => a + b, 0) / slice.length;
  const atrRatio = atrMean > 0 ? +(atr / atrMean).toFixed(3) : 1;
  return { atr, atrMean, atrRatio };
}

// Wilder's ADX (14-period) — matches qualityFilter.ts calcADX exactly.
function calcADX(candles: OHLCV[], period = 14): number {
  if (candles.length < period * 2) return 25;
  const n = candles.length;
  const tr: number[] = [], dmP: number[] = [], dmM: number[] = [];
  for (let i = 1; i < n; i++) {
    const c = candles[i], p = candles[i - 1];
    tr.push(Math.max(c.high - c.low, Math.abs(c.high - p.close), Math.abs(c.low - p.close)));
    const up = c.high - p.high, dn = p.low - c.low;
    dmP.push(up > dn && up > 0 ? up : 0);
    dmM.push(dn > up && dn > 0 ? dn : 0);
  }
  let sTR = tr.slice(0, period).reduce((a, b) => a + b, 0);
  let sDP = dmP.slice(0, period).reduce((a, b) => a + b, 0);
  let sDM = dmM.slice(0, period).reduce((a, b) => a + b, 0);
  const dxArr: number[] = [];
  for (let i = period; i < tr.length; i++) {
    sTR = sTR - sTR / period + tr[i];
    sDP = sDP - sDP / period + dmP[i];
    sDM = sDM - sDM / period + dmM[i];
    const diP = sTR > 0 ? 100 * sDP / sTR : 0;
    const diM = sTR > 0 ? 100 * sDM / sTR : 0;
    const sum = diP + diM;
    dxArr.push(sum > 0 ? 100 * Math.abs(diP - diM) / sum : 0);
  }
  if (dxArr.length < period) return 25;
  return +(dxArr.slice(-period).reduce((a, b) => a + b, 0) / period).toFixed(2);
}

// Pullback: short-term retracement within an established trend.
//   Bull pullback → trend BULLISH but last 5 bars moved down
//   Bear pullback → trend BEARISH but last 5 bars moved up
function detectPullback(candles: OHLCV[], trend: TrendResult): 'BULL' | 'BEAR' | null {
  if (candles.length < 10) return null;
  const n    = candles.length;
  const now  = candles[n - 1].close;
  const prev = candles[n - 6].close; // 5 bars ago

  const stBull = now > prev * 1.0003; // +0.03% short-term up
  const stBear = now < prev * 0.9997; // -0.03% short-term down

  if (trend.trend === 'BULLISH' && stBear) return 'BULL';
  if (trend.trend === 'BEARISH' && stBull) return 'BEAR';
  return null;
}

// ===== MAIN ENTRY POINT =====

// ===== ATR HIGH VOLATILITY FILTER FLAG =====
// Set to false by default: in backtests this filter reduced profits without
// improving drawdown.  Code is preserved; set useAtrHighVolFilter=true to re-enable.
export const USE_ATR_HIGH_VOL_FILTER_DEFAULT = false;

export function detectMarketRegime(
  candles: OHLCV[],
  trend:   TrendResult,
  wyckoff: WyckoffResult,
  useAtrHighVolFilter = USE_ATR_HIGH_VOL_FILTER_DEFAULT,
): RegimeResult {
  const { atr, atrMean, atrRatio } = calcATRValues(candles, 20);
  const adx = calcADX(candles, 14);

  const make = (regime: MarketRegime, description: string): RegimeResult => ({
    regime, rules: REGIME_RULES[regime], atr, atrMean, atrRatio, adx, description,
  });

  // ── 1. HIGH VOLATILITY ─────────────────────────────────────────────
  // Controlled by useAtrHighVolFilter flag (default: false — disabled).
  // In backtests this regime reduced profit without improving drawdown.
  // Code is preserved; pass useAtrHighVolFilter=true to re-enable.
  if (useAtrHighVolFilter && atrRatio > 1.8) {
    return make('HIGH_VOLATILITY',
      `ATR ${(atrRatio).toFixed(2)}× above mean — erratic, spike risk elevated`);
  }

  // ── 2. LOW VOLATILITY ──────────────────────────────────────────────
  if (atrRatio < 0.60 && adx < 20) {
    return make('LOW_VOLATILITY',
      `ATR at ${(atrRatio * 100).toFixed(0)}% of mean + ADX ${adx} — squeeze / dead zone`);
  }

  // ── 3. STRONG TREND (ADX ≥ 30 + STRONG EMA alignment) ────────────
  if (adx >= 30 && trend.strength === 'STRONG') {
    if (trend.trend === 'BULLISH') {
      return make('STRONG_TREND_BULL', `ADX ${adx} — all EMAs aligned bull, institutional flow`);
    }
    if (trend.trend === 'BEARISH') {
      return make('STRONG_TREND_BEAR', `ADX ${adx} — all EMAs aligned bear, institutional flow`);
    }
  }

  // ── 4. PULLBACK (retracement within established trend) ────────────
  const pullDir = detectPullback(candles, trend);
  if (pullDir === 'BULL' && adx >= 20) {
    return make('PULLBACK_BULL', 'Bear retracement within bull trend — high-probability long zone');
  }
  if (pullDir === 'BEAR' && adx >= 20) {
    return make('PULLBACK_BEAR', 'Bull retracement within bear trend — high-probability short zone');
  }

  // ── 5. WEAK TREND (ADX 20–30 or MODERATE EMA strength) ───────────
  if (adx >= 20 && trend.trend !== 'NEUTRAL') {
    if (trend.trend === 'BULLISH') {
      return make('WEAK_TREND_BULL', `ADX ${adx} — developing bullish trend, EMA ${trend.strength}`);
    }
    return make('WEAK_TREND_BEAR', `ADX ${adx} — developing bearish trend, EMA ${trend.strength}`);
  }

  // ── 6. WYCKOFF PHASES ────────────────────────────────────────────
  if (wyckoff.phase === 'ACCUMULATION') {
    return make('ACCUMULATION',
      `Wyckoff Accumulation — institutional demand absorption${wyckoff.spring ? ' + Spring confirmed' : ''}`);
  }
  if (wyckoff.phase === 'DISTRIBUTION') {
    return make('DISTRIBUTION',
      `Wyckoff Distribution — institutional supply absorption${wyckoff.upthrust ? ' + Upthrust confirmed' : ''}`);
  }

  // ── 7. RANGE (default) ────────────────────────────────────────────
  return make('RANGE', `ADX ${adx} < 20 — ranging / choppy, no directional edge`);
}

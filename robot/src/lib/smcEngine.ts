// Smart Money Concepts Engine
// Optimized for XAUUSD scalping on M1 and M5 timeframes
//
// Quality improvements applied:
//   • Fake BOS filter  — break candle must clear the level by a minimum margin
//     AND its body must be ≥35% of its total range (wick-dominated breaks rejected)
//   • Weak Order Block filter — OB candle body must be ≥30% of its range AND
//     meet a minimum absolute body size; tiny indecision candles are discarded
//   • Real Liquidity Sweep confirmation — sweep candle must close a meaningful
//     distance beyond the swept level AND carry a directional body (close > open
//     for bullish, close < open for bearish); wick-only touches are rejected
//   • Tighter thresholds on M1 and M5 to match XAUUSD intraday noise levels

import { OHLCV } from './goldEngine.js';
import { analyzeWyckoff, WyckoffResult } from './wyckoffEngine.js';
import { analyzePriceAction, PriceActionResult } from './priceActionEngine.js';
import { analyzeTrend } from './trendEngine.js';
import { applyEntryFilter } from './entryFilter.js';
import { applyQualityFilter } from './qualityFilter.js';

// ===== PER-TIMEFRAME CONFIGURATION =====

interface SmcConfig {
  swingLookback: number;
  fvgMinSize: number;
  equalLevelTolerance: number;
  // Liquidity sweep
  liquiditySweepMin: number;    // minimum wick extension beyond swept level
  minSweepCloseMargin: number;  // minimum close distance back from swept level
  // Order Block quality
  nearObThreshold: number;
  nearFvgThreshold: number;
  minObBodySize: number;        // minimum absolute body size for a valid OB candle
  minObBodyRatio: number;       // minimum body / (high-low) ratio for a valid OB candle
  // BOS / CHoCH quality
  minBreakDistance: number;     // close must clear swing level by at least this amount
  minBosBodyRatio: number;      // break candle body / range — rejects wick-dominated breaks
  // Limits
  maxOrderBlocks: number;
  maxFvgs: number;
  maxBos: number;
  maxChoch: number;
  maxSweeps: number;
}

// M1 — 1-minute candles, tight thresholds
const CFG_M1: SmcConfig = {
  swingLookback: 3,
  fvgMinSize: 0.05,             // 5 cents minimum gap (raised from 3)
  equalLevelTolerance: 0.10,
  liquiditySweepMin: 0.08,      // 8 cents minimum wick (raised from 3)
  minSweepCloseMargin: 0.05,    // close must be 5 cents past swept level
  nearObThreshold: 0.30,
  nearFvgThreshold: 0.20,
  minObBodySize: 0.10,          // OB candle body at least 10 cents
  minObBodyRatio: 0.30,         // OB body ≥ 30% of candle range
  minBreakDistance: 0.10,       // BOS close must clear level by 10 cents
  minBosBodyRatio: 0.35,        // break candle body ≥ 35% of range
  maxOrderBlocks: 6,
  maxFvgs: 6,
  maxBos: 5,
  maxChoch: 3,
  maxSweeps: 5,
};

// M5 — 5-minute candles, wider thresholds
const CFG_M5: SmcConfig = {
  swingLookback: 5,
  fvgMinSize: 0.10,             // 10 cents minimum gap (raised from 5)
  equalLevelTolerance: 0.15,
  liquiditySweepMin: 0.15,      // 15 cents minimum wick (raised from 5)
  minSweepCloseMargin: 0.10,    // close must be 10 cents past swept level
  nearObThreshold: 0.50,
  nearFvgThreshold: 0.30,
  minObBodySize: 0.15,          // OB candle body at least 15 cents
  minObBodyRatio: 0.30,         // OB body ≥ 30% of candle range
  minBreakDistance: 0.20,       // BOS close must clear level by 20 cents
  minBosBodyRatio: 0.35,        // break candle body ≥ 35% of range
  maxOrderBlocks: 6,
  maxFvgs: 6,
  maxBos: 5,
  maxChoch: 3,
  maxSweeps: 5,
};

// ===== TYPE DEFINITIONS =====

export interface SmcBos {
  type: 'BUY' | 'SELL';
  price: number;
  barIndex: number;
  time: string;
}

export interface SmcChoch {
  type: 'BUY' | 'SELL';
  price: number;
  barIndex: number;
  time: string;
}

export interface SmcOrderBlock {
  type: 'BULLISH' | 'BEARISH';
  high: number;
  low: number;
  open: number;
  close: number;
  barIndex: number;
  time: string;
  mitigated: boolean;
}

export interface SmcFvg {
  type: 'BULLISH' | 'BEARISH';
  top: number;
  bottom: number;
  barIndex: number;
  time: string;
  filled: boolean;
}

export interface SmcLiquiditySweep {
  type: 'BULLISH' | 'BEARISH';
  sweptLevel: number;
  wickExtreme: number;
  barIndex: number;
  time: string;
}

export interface SmcEqualLevel {
  type: 'HIGH' | 'LOW';
  price: number;
  barIndices: number[];
  time: string;
}

export interface SmcMitigationBlock {
  originalOb: SmcOrderBlock;
  mitigatedAtBarIndex: number;
  mitigatedAtTime: string;
}

export interface SmcResult {
  timeframe: 'M1' | 'M5';
  timestamp: string;
  currentPrice: number;
  trend: 'BULLISH' | 'BEARISH' | 'NEUTRAL';
  bosSignals: SmcBos[];
  chochSignals: SmcChoch[];
  orderBlocks: SmcOrderBlock[];
  fairValueGaps: SmcFvg[];
  liquiditySweeps: SmcLiquiditySweep[];
  equalHighs: SmcEqualLevel[];
  equalLows: SmcEqualLevel[];
  mitigationBlocks: SmcMitigationBlock[];
  smcSignal: 'BUY' | 'SELL' | 'NEUTRAL';
  smcScore: number;
}

// ===== HELPERS =====

// Body size and ratio of a single candle
function candleBody(c: OHLCV): number {
  return Math.abs(c.close - c.open);
}
function candleRange(c: OHLCV): number {
  return c.high - c.low;
}
// Body/range ratio — 0 when range is zero (doji edge case)
function bodyRatio(c: OHLCV): number {
  const r = candleRange(c);
  return r > 0 ? candleBody(c) / r : 0;
}

// ===== SWING POINT DETECTION =====
// Confirmed swing: strictly the extremum within ±lookback bars on both sides.

function detectSwingHighs(candles: OHLCV[], lookback: number): number[] {
  const result: number[] = [];
  const end = candles.length - lookback;
  for (let i = lookback; i < end; i++) {
    const h = candles[i].high;
    let valid = true;
    for (let j = 1; j <= lookback; j++) {
      if (candles[i - j].high >= h || candles[i + j].high >= h) { valid = false; break; }
    }
    if (valid) result.push(i);
  }
  return result;
}

function detectSwingLows(candles: OHLCV[], lookback: number): number[] {
  const result: number[] = [];
  const end = candles.length - lookback;
  for (let i = lookback; i < end; i++) {
    const l = candles[i].low;
    let valid = true;
    for (let j = 1; j <= lookback; j++) {
      if (candles[i - j].low <= l || candles[i + j].low <= l) { valid = false; break; }
    }
    if (valid) result.push(i);
  }
  return result;
}

// ===== STATEFUL BOS + CHoCH DETECTION WITH FAKE-BOS FILTER =====
//
// Scans bars chronologically using the most recent unbroken swing high/low.
// Each break is validated against two quality gates before being accepted:
//
//   1. BREAK DISTANCE — the closing price must exceed the swing level by at
//      least cfg.minBreakDistance.  A micro-break that barely ticks through
//      the level and reverses is almost always a fake; the distance filter
//      eliminates these without requiring forward-looking lookahead.
//
//   2. BREAK CANDLE BODY RATIO — the body of the break candle must be at
//      least cfg.minBosBodyRatio of the candle's total range.  A wick-
//      dominated candle (spike through the level, close near the open)
//      signals rejection rather than genuine institutional momentum.
//      CHoCH applies a slightly relaxed ratio (×0.8) because reversals
//      often start with a wider-wick engulfing bar.
//
// Both conditions must hold for the break to be registered.

function detectBosAndChoch(
  candles: OHLCV[],
  swingHighIdx: number[],
  swingLowIdx: number[],
  cfg: SmcConfig,
): {
  bosSignals: SmcBos[];
  chochSignals: SmcChoch[];
  finalTrend: 'BULLISH' | 'BEARISH' | 'NEUTRAL';
} {
  const bosSignals: SmcBos[] = [];
  const chochSignals: SmcChoch[] = [];

  const usedHighs = new Set<number>();
  const usedLows  = new Set<number>();

  let localTrend: 'BULLISH' | 'BEARISH' | 'NEUTRAL' = 'NEUTRAL';
  const recentBosDir: Array<'BUY' | 'SELL'> = [];

  // CHoCH body threshold is slightly relaxed — reversals can be wide-wick bars
  const chochBodyRatio = cfg.minBosBodyRatio * 0.8;

  for (let i = cfg.swingLookback; i < candles.length; i++) {
    const c = candles[i];
    const br = bodyRatio(c); // break candle body ratio

    // ── Most recent UNBROKEN swing high before bar i ──
    let recentShPos = -1;
    for (let k = swingHighIdx.length - 1; k >= 0; k--) {
      if (swingHighIdx[k] < i && !usedHighs.has(k)) { recentShPos = k; break; }
    }

    // ── Most recent UNBROKEN swing low before bar i ──
    let recentSlPos = -1;
    for (let k = swingLowIdx.length - 1; k >= 0; k--) {
      if (swingLowIdx[k] < i && !usedLows.has(k)) { recentSlPos = k; break; }
    }

    // ── Bullish break ──────────────────────────────────────────────────────
    if (recentShPos >= 0) {
      const shPrice = candles[swingHighIdx[recentShPos]].high;
      const breakDist = c.close - shPrice;

      // FAKE BOS GATE: must clear level by minBreakDistance AND have enough body
      if (breakDist >= cfg.minBreakDistance && br >= (localTrend === 'BEARISH' ? chochBodyRatio : cfg.minBosBodyRatio)) {
        if (localTrend === 'BEARISH') {
          chochSignals.push({ type: 'BUY', price: +shPrice.toFixed(2), barIndex: i, time: c.time });
          localTrend = 'NEUTRAL';
          recentBosDir.length = 0;
        } else {
          bosSignals.push({ type: 'BUY', price: +shPrice.toFixed(2), barIndex: i, time: c.time });
          recentBosDir.push('BUY');
          if (recentBosDir.slice(-2).every(d => d === 'BUY')) localTrend = 'BULLISH';
        }
        usedHighs.add(recentShPos);
      }
    }

    // ── Bearish break ──────────────────────────────────────────────────────
    if (recentSlPos >= 0) {
      const slPrice = candles[swingLowIdx[recentSlPos]].low;
      const breakDist = slPrice - c.close;

      // FAKE BOS GATE: must clear level by minBreakDistance AND have enough body
      if (breakDist >= cfg.minBreakDistance && br >= (localTrend === 'BULLISH' ? chochBodyRatio : cfg.minBosBodyRatio)) {
        if (localTrend === 'BULLISH') {
          chochSignals.push({ type: 'SELL', price: +slPrice.toFixed(2), barIndex: i, time: c.time });
          localTrend = 'NEUTRAL';
          recentBosDir.length = 0;
        } else {
          bosSignals.push({ type: 'SELL', price: +slPrice.toFixed(2), barIndex: i, time: c.time });
          recentBosDir.push('SELL');
          if (recentBosDir.slice(-2).every(d => d === 'SELL')) localTrend = 'BEARISH';
        }
        usedLows.add(recentSlPos);
      }
    }
  }

  return {
    bosSignals:   bosSignals.slice(-cfg.maxBos),
    chochSignals: chochSignals.slice(-cfg.maxChoch),
    finalTrend:   localTrend,
  };
}

// ===== ORDER BLOCK DETECTION WITH WEAK-OB FILTER =====
//
// Bullish OB: last bearish candle before a BUY BOS impulse.
// Bearish OB: last bullish candle before a SELL BOS impulse.
//
// A weak OB candle is discarded when either condition fails:
//   • BODY SIZE  — candleBody(ob) < cfg.minObBodySize
//     Tiny candles (dojis, spinning tops) do not represent meaningful
//     institutional order flow; they are far more likely to be noise.
//   • BODY RATIO — bodyRatio(ob) < cfg.minObBodyRatio
//     The body must be at least 30% of the candle's range.  If the body
//     is dwarfed by wicks the candle is an indecision bar, not an OB.
//
// Mitigation check is unchanged: price re-enters the OB zone → mitigated.

function detectOrderBlocks(
  candles: OHLCV[],
  bosSignals: SmcBos[],
  cfg: SmcConfig,
): { orderBlocks: SmcOrderBlock[]; mitigationBlocks: SmcMitigationBlock[] } {
  const orderBlocks: SmcOrderBlock[] = [];
  const mitigationBlocks: SmcMitigationBlock[] = [];
  const usedObIdx = new Set<number>();

  for (const bos of bosSignals) {
    const maxLook = Math.min(8, bos.barIndex);
    let obIdx = -1;

    if (bos.type === 'BUY') {
      for (let k = bos.barIndex - 1; k >= bos.barIndex - maxLook; k--) {
        if (k < 0) break;
        const candidate = candles[k];
        // Must be bearish candle with sufficient body quality
        if (
          candidate.close < candidate.open &&
          candleBody(candidate) >= cfg.minObBodySize &&
          bodyRatio(candidate) >= cfg.minObBodyRatio
        ) { obIdx = k; break; }
      }
    } else {
      for (let k = bos.barIndex - 1; k >= bos.barIndex - maxLook; k--) {
        if (k < 0) break;
        const candidate = candles[k];
        // Must be bullish candle with sufficient body quality
        if (
          candidate.close > candidate.open &&
          candleBody(candidate) >= cfg.minObBodySize &&
          bodyRatio(candidate) >= cfg.minObBodyRatio
        ) { obIdx = k; break; }
      }
    }

    if (obIdx < 0 || usedObIdx.has(obIdx)) continue;
    usedObIdx.add(obIdx);

    const ob: SmcOrderBlock = {
      type:     bos.type === 'BUY' ? 'BULLISH' : 'BEARISH',
      high:     +candles[obIdx].high.toFixed(2),
      low:      +candles[obIdx].low.toFixed(2),
      open:     +candles[obIdx].open.toFixed(2),
      close:    +candles[obIdx].close.toFixed(2),
      barIndex: obIdx,
      time:     candles[obIdx].time,
      mitigated: false,
    };

    // Mitigation: price re-enters OB zone after the BOS
    for (let j = bos.barIndex + 1; j < candles.length; j++) {
      if (candles[j].low <= ob.high && candles[j].high >= ob.low) {
        ob.mitigated = true;
        mitigationBlocks.push({
          originalOb: { ...ob },
          mitigatedAtBarIndex: j,
          mitigatedAtTime: candles[j].time,
        });
        break;
      }
    }
    orderBlocks.push(ob);
  }

  const activeObs = orderBlocks
    .filter(ob => !ob.mitigated)
    .slice(-cfg.maxOrderBlocks);

  return {
    orderBlocks: activeObs,
    mitigationBlocks: mitigationBlocks.slice(-cfg.maxOrderBlocks),
  };
}

// ===== FAIR VALUE GAP (FVG) =====
// 3-candle pattern:
//   Bullish FVG: candles[i-2].high < candles[i].low  (gap between C1 high and C3 low)
//   Bearish FVG: candles[i-2].low  > candles[i].high (gap between C1 low and C3 high)
// Minimum gap size enforced by cfg.fvgMinSize (raised from earlier defaults).
// FVG is "filled" when price returns fully into the gap zone.

function detectFairValueGaps(candles: OHLCV[], cfg: SmcConfig): SmcFvg[] {
  const fvgs: SmcFvg[] = [];
  const currentHigh = candles[candles.length - 1].high;
  const currentLow  = candles[candles.length - 1].low;

  for (let i = 2; i < candles.length; i++) {
    const c1 = candles[i - 2];
    const c3 = candles[i];

    // Bullish FVG
    if (c1.high < c3.low && (c3.low - c1.high) >= cfg.fvgMinSize) {
      let filled = false;
      for (let j = i + 1; j < candles.length; j++) {
        if (candles[j].low <= c3.low && candles[j].high >= c1.high) { filled = true; break; }
      }
      if (!filled && currentLow <= c3.low && currentHigh >= c1.high) filled = true;
      fvgs.push({ type: 'BULLISH', top: +c3.low.toFixed(2), bottom: +c1.high.toFixed(2), barIndex: i - 1, time: candles[i - 1].time, filled });
    }

    // Bearish FVG
    if (c1.low > c3.high && (c1.low - c3.high) >= cfg.fvgMinSize) {
      let filled = false;
      for (let j = i + 1; j < candles.length; j++) {
        if (candles[j].high >= c3.high && candles[j].low <= c1.low) { filled = true; break; }
      }
      if (!filled && currentHigh >= c3.high && currentLow <= c1.low) filled = true;
      fvgs.push({ type: 'BEARISH', top: +c1.low.toFixed(2), bottom: +c3.high.toFixed(2), barIndex: i - 1, time: candles[i - 1].time, filled });
    }
  }

  return fvgs.filter(g => !g.filled).slice(-cfg.maxFvgs);
}

// ===== LIQUIDITY SWEEP DETECTION WITH REAL-SWEEP CONFIRMATION =====
//
// A genuine liquidity sweep requires ALL THREE conditions to hold:
//
//   1. WICK EXTENSION — the wick must extend beyond the swing level by at
//      least cfg.liquiditySweepMin.  Tiny pokes are noise, not sweeps.
//
//   2. CLOSE MARGIN — the close must be back on the correct side of the
//      swept level by at least cfg.minSweepCloseMargin.  A candle that
//      dips below a swing low but barely closes above it (less than the
//      margin) has not demonstrated genuine institutional rejection.
//
//   3. DIRECTIONAL BODY — after sweeping, the candle must show commitment:
//      • Bullish sweep (swept lows → reversal up): close > open
//      • Bearish sweep (swept highs → reversal down): close < open
//      A doji or inside-body candle after the sweep is treated as
//      inconclusive and is rejected.
//
// De-duplication: sweeps within 0.05 of an already-recorded level are
// collapsed (one event per level, most recent wins).

function detectLiquiditySweeps(
  candles: OHLCV[],
  swingHighIdx: number[],
  swingLowIdx: number[],
  cfg: SmcConfig,
): SmcLiquiditySweep[] {
  const sweeps: SmcLiquiditySweep[] = [];

  for (let i = cfg.swingLookback; i < candles.length; i++) {
    const c = candles[i];

    // ── Bullish sweep: wick below swing low, close meaningfully above ──────
    for (const slIdx of swingLowIdx.filter(idx => idx < i && idx >= i - 20).reverse()) {
      const level = candles[slIdx].low;

      const wickOk  = (level - c.low)   >= cfg.liquiditySweepMin;
      const closeOk = (c.close - level) >= cfg.minSweepCloseMargin;
      const bodyOk  = c.close > c.open; // buyers took control after the sweep

      if (wickOk && closeOk && bodyOk) {
        if (!sweeps.some(s => s.type === 'BULLISH' && Math.abs(s.sweptLevel - level) < 0.05)) {
          sweeps.push({
            type:        'BULLISH',
            sweptLevel:  +level.toFixed(2),
            wickExtreme: +c.low.toFixed(2),
            barIndex:    i,
            time:        c.time,
          });
        }
        break;
      }
    }

    // ── Bearish sweep: wick above swing high, close meaningfully below ──────
    for (const shIdx of swingHighIdx.filter(idx => idx < i && idx >= i - 20).reverse()) {
      const level = candles[shIdx].high;

      const wickOk  = (c.high - level)  >= cfg.liquiditySweepMin;
      const closeOk = (level - c.close) >= cfg.minSweepCloseMargin;
      const bodyOk  = c.close < c.open; // sellers took control after the sweep

      if (wickOk && closeOk && bodyOk) {
        if (!sweeps.some(s => s.type === 'BEARISH' && Math.abs(s.sweptLevel - level) < 0.05)) {
          sweeps.push({
            type:        'BEARISH',
            sweptLevel:  +level.toFixed(2),
            wickExtreme: +c.high.toFixed(2),
            barIndex:    i,
            time:        c.time,
          });
        }
        break;
      }
    }
  }

  return sweeps.slice(-cfg.maxSweeps);
}

// ===== EQUAL HIGHS / EQUAL LOWS =====
// Groups confirmed swing points within the tolerance band.
// Each group of ≥2 → one Equal High or Equal Low level (liquidity pool).

function detectEqualLevels(
  candles: OHLCV[],
  swingHighIdx: number[],
  swingLowIdx: number[],
  cfg: SmcConfig,
): { equalHighs: SmcEqualLevel[]; equalLows: SmcEqualLevel[] } {
  const tol = cfg.equalLevelTolerance;

  function groupLevels(indices: number[], getValue: (idx: number) => number): SmcEqualLevel[] {
    const levels: SmcEqualLevel[] = [];
    const processed = new Set<number>();

    for (let i = 0; i < indices.length; i++) {
      if (processed.has(i)) continue;
      const basePrice = getValue(indices[i]);
      const group = [i];
      for (let j = i + 1; j < indices.length; j++) {
        if (!processed.has(j) && Math.abs(getValue(indices[j]) - basePrice) <= tol) {
          group.push(j);
          processed.add(j);
        }
      }
      if (group.length >= 2) {
        const avgPrice = group.reduce((s, k) => s + getValue(indices[k]), 0) / group.length;
        const lastK = group[group.length - 1];
        levels.push({
          type:       indices === swingHighIdx ? 'HIGH' : 'LOW',
          price:      +avgPrice.toFixed(2),
          barIndices: group.map(k => indices[k]),
          time:       candles[indices[lastK]].time,
        });
      }
      processed.add(i);
    }
    return levels;
  }

  return {
    equalHighs: groupLevels(swingHighIdx, i => candles[i].high),
    equalLows:  groupLevels(swingLowIdx,  i => candles[i].low),
  };
}

// ===== COMPOSITE SMC SIGNAL =====
// Quality filtering is upstream; every signal here has passed the quality gates.
// Wyckoff and Price Action are confirmation-only layers — they add weight but
// cannot flip direction or initiate a signal on their own.

function computeSmcSignal(
  trend: 'BULLISH' | 'BEARISH' | 'NEUTRAL',
  bosSignals: SmcBos[],
  chochSignals: SmcChoch[],
  orderBlocks: SmcOrderBlock[],
  fvgs: SmcFvg[],
  sweeps: SmcLiquiditySweep[],
  currentPrice: number,
  cfg: SmcConfig,
  wyckoff: WyckoffResult,
  pa: PriceActionResult,
): { smcSignal: 'BUY' | 'SELL' | 'NEUTRAL'; smcScore: number } {
  let buyScore  = 0;
  let sellScore = 0;

  // Established structural trend — weight 2
  if (trend === 'BULLISH') buyScore  += 2;
  else if (trend === 'BEARISH') sellScore += 2;

  // Most recent BOS — weight 2
  const lastBos = bosSignals[bosSignals.length - 1];
  if (lastBos?.type === 'BUY')  buyScore  += 2;
  else if (lastBos?.type === 'SELL') sellScore += 2;

  // Most recent CHoCH — weight 3 (strongest structural signal)
  const lastChoch = chochSignals[chochSignals.length - 1];
  if (lastChoch?.type === 'BUY')  buyScore  += 3;
  else if (lastChoch?.type === 'SELL') sellScore += 3;

  // Active Order Blocks near current price — weight 2 each, max 2 OBs
  let obHits = 0;
  for (const ob of orderBlocks) {
    if (obHits >= 2) break;
    const inZone   = currentPrice >= ob.low && currentPrice <= ob.high;
    const nearZone = Math.abs(currentPrice - (ob.type === 'BULLISH' ? ob.low : ob.high)) <= cfg.nearObThreshold;
    if (inZone || nearZone) {
      if (ob.type === 'BULLISH') buyScore  += 2;
      else                       sellScore += 2;
      obHits++;
    }
  }

  // Active FVGs near current price — weight 1 each, max 2 FVGs
  let fvgHits = 0;
  for (const fvg of fvgs) {
    if (fvgHits >= 2) break;
    const nearFvg = currentPrice >= fvg.bottom - cfg.nearFvgThreshold &&
                    currentPrice <= fvg.top    + cfg.nearFvgThreshold;
    if (nearFvg) {
      if (fvg.type === 'BULLISH') buyScore  += 1;
      else                         sellScore += 1;
      fvgHits++;
    }
  }

  // Most recent confirmed liquidity sweep — weight 2
  const lastSweep = sweeps[sweeps.length - 1];
  if (lastSweep?.type === 'BULLISH') buyScore  += 2;
  else if (lastSweep?.type === 'BEARISH') sellScore += 2;

  // ── Wyckoff confirmation — weight up to 2 (confirmation only) ────────────
  // +1 when Wyckoff phase aligns with the candidate direction (Accumulation→BUY,
  //    Distribution→SELL), weighted by Wyckoff score so weak phases add less.
  // +1 additional when a Spring (bullish) or Upthrust (bearish) is confirmed,
  //    indicating a high-conviction structural event that matches the SMC read.
  // Wyckoff can only ADD to the dominant side — it never flips direction.
  if (wyckoff.wyckoffSignal === 'BUY') {
    buyScore  += wyckoff.wyckoffScore;          // fractional phase weight
    if (wyckoff.spring) buyScore  += 1;         // Spring is a discrete event bonus
  } else if (wyckoff.wyckoffSignal === 'SELL') {
    sellScore += wyckoff.wyckoffScore;
    if (wyckoff.upthrust) sellScore += 1;       // Upthrust bonus
  }

  // ── Price Action confirmation — weight up to 2 (confirmation only) ──────────
  // PA score (0–1) is scaled to add at most 1.5 points; an additional 0.5 is
  // added when a high-conviction PA event (Engulf, Pin Bar, Valid Breakout)
  // aligns with the SMC direction.  Fake Breakout in the SMC direction subtracts
  // 0.5 as a mild warning — SMC still leads, PA just lowers confidence slightly.
  if (pa.paSignal === 'BUY') {
    buyScore  += pa.paScore * 1.5;
    if (pa.bullishEngulf || pa.bullishPinBar || pa.validBullBreakout) buyScore  += 0.5;
    if (pa.fakeBullBreakout) buyScore  -= 0.5;
  } else if (pa.paSignal === 'SELL') {
    sellScore += pa.paScore * 1.5;
    if (pa.bearishEngulf || pa.bearishPinBar || pa.validBearBreakout) sellScore += 0.5;
    if (pa.fakeBearBreakout) sellScore -= 0.5;
  }

  // Floor at zero — penalty cannot push score negative
  buyScore  = Math.max(0, buyScore);
  sellScore = Math.max(0, sellScore);

  // maxPossible: 14 (SMC) + 2 (Wyckoff) + 2 (PA) = 18
  const maxPossible   = 18;
  const dominantScore = Math.max(buyScore, sellScore);
  const netScore      = +(dominantScore / maxPossible).toFixed(2);

  let smcSignal: 'BUY' | 'SELL' | 'NEUTRAL' = 'NEUTRAL';
  if      (buyScore  > sellScore && netScore >= 0.35) smcSignal = 'BUY';
  else if (sellScore > buyScore  && netScore >= 0.35) smcSignal = 'SELL';

  return { smcSignal, smcScore: Math.min(1, netScore) };
}

// ===== PER-BAR SMC STATE (for backtest integration) =====
//
// Records structural trend, last BOS dir, and last CHoCH dir at every bar.
// Applies the same fake-BOS quality gates used in detectBosAndChoch so
// the backtest engine sees only confirmed structural events.

export interface SmcBarState {
  trend:        'BULLISH' | 'BEARISH' | 'NEUTRAL';
  lastBosDir:   'BUY' | 'SELL' | null;
  lastChochDir: 'BUY' | 'SELL' | null;
}

export function computeSmcStatePerBar(
  candles: OHLCV[],
  timeframe: 'M1' | 'M5' | 'M15',
): SmcBarState[] {
  const cfg = timeframe === 'M1' ? CFG_M1 : CFG_M5;

  const states: SmcBarState[] = candles.map(() => ({
    trend:        'NEUTRAL' as const,
    lastBosDir:   null,
    lastChochDir: null,
  }));

  const swingHighIdx = detectSwingHighs(candles, cfg.swingLookback);
  const swingLowIdx  = detectSwingLows(candles,  cfg.swingLookback);

  const usedHighs = new Set<number>();
  const usedLows  = new Set<number>();

  let localTrend: 'BULLISH' | 'BEARISH' | 'NEUTRAL' = 'NEUTRAL';
  const recentBosDir: Array<'BUY' | 'SELL'> = [];
  let lastBosDir:   'BUY' | 'SELL' | null = null;
  let lastChochDir: 'BUY' | 'SELL' | null = null;

  const chochBodyRatio = cfg.minBosBodyRatio * 0.8;

  for (let i = cfg.swingLookback; i < candles.length; i++) {
    const c  = candles[i];
    const br = bodyRatio(c);

    // Swing high must be confirmed (no lookahead past the confirmation window)
    let recentShPos = -1;
    for (let k = swingHighIdx.length - 1; k >= 0; k--) {
      if (swingHighIdx[k] + cfg.swingLookback < i && !usedHighs.has(k)) { recentShPos = k; break; }
    }

    let recentSlPos = -1;
    for (let k = swingLowIdx.length - 1; k >= 0; k--) {
      if (swingLowIdx[k] + cfg.swingLookback < i && !usedLows.has(k)) { recentSlPos = k; break; }
    }

    // ── Bullish break — apply same fake-BOS gate ──────────────────────────
    if (recentShPos >= 0) {
      const shPrice   = candles[swingHighIdx[recentShPos]].high;
      const breakDist = c.close - shPrice;
      const minBody   = localTrend === 'BEARISH' ? chochBodyRatio : cfg.minBosBodyRatio;

      if (breakDist >= cfg.minBreakDistance && br >= minBody) {
        if (localTrend === 'BEARISH') {
          lastChochDir = 'BUY';
          localTrend   = 'NEUTRAL';
          recentBosDir.length = 0;
        } else {
          lastBosDir = 'BUY';
          recentBosDir.push('BUY');
          if (recentBosDir.slice(-2).every(d => d === 'BUY')) localTrend = 'BULLISH';
        }
        usedHighs.add(recentShPos);
      }
    }

    // ── Bearish break — apply same fake-BOS gate ──────────────────────────
    if (recentSlPos >= 0) {
      const slPrice   = candles[swingLowIdx[recentSlPos]].low;
      const breakDist = slPrice - c.close;
      const minBody   = localTrend === 'BULLISH' ? chochBodyRatio : cfg.minBosBodyRatio;

      if (breakDist >= cfg.minBreakDistance && br >= minBody) {
        if (localTrend === 'BULLISH') {
          lastChochDir = 'SELL';
          localTrend   = 'NEUTRAL';
          recentBosDir.length = 0;
        } else {
          lastBosDir = 'SELL';
          recentBosDir.push('SELL');
          if (recentBosDir.slice(-2).every(d => d === 'SELL')) localTrend = 'BEARISH';
        }
        usedLows.add(recentSlPos);
      }
    }

    states[i] = { trend: localTrend, lastBosDir, lastChochDir };
  }

  return states;
}

// ===== MAIN ENTRY POINT =====

export function analyzeSmcStructure(
  candles: OHLCV[],
  timeframe: 'M1' | 'M5',
  minConfirmations = 3,
): SmcResult {
  const cfg = timeframe === 'M1' ? CFG_M1 : CFG_M5;
  const minRequired = cfg.swingLookback * 2 + 10;

  if (candles.length < minRequired) {
    const last = candles[candles.length - 1];
    return {
      timeframe,
      timestamp:       last?.time ?? new Date().toISOString(),
      currentPrice:    last?.close ?? 0,
      trend:           'NEUTRAL',
      bosSignals:      [],
      chochSignals:    [],
      orderBlocks:     [],
      fairValueGaps:   [],
      liquiditySweeps: [],
      equalHighs:      [],
      equalLows:       [],
      mitigationBlocks: [],
      smcSignal:       'NEUTRAL',
      smcScore:        0,
    };
  }

  const swingHighIdx = detectSwingHighs(candles, cfg.swingLookback);
  const swingLowIdx  = detectSwingLows(candles,  cfg.swingLookback);

  const { bosSignals, chochSignals, finalTrend } = detectBosAndChoch(
    candles, swingHighIdx, swingLowIdx, cfg,
  );

  const { orderBlocks, mitigationBlocks } = detectOrderBlocks(candles, bosSignals, cfg);
  const fairValueGaps   = detectFairValueGaps(candles, cfg);
  const liquiditySweeps = detectLiquiditySweeps(candles, swingHighIdx, swingLowIdx, cfg);
  const { equalHighs, equalLows } = detectEqualLevels(candles, swingHighIdx, swingLowIdx, cfg);

  const currentPrice = candles[candles.length - 1].close;

  // Wyckoff and Price Action run on the same candle array — confirmation only
  const wyckoff = analyzeWyckoff(candles, timeframe);
  const pa      = analyzePriceAction(candles, timeframe);

  let { smcSignal, smcScore } = computeSmcSignal(
    finalTrend, bosSignals, chochSignals, orderBlocks, fairValueGaps, liquiditySweeps,
    currentPrice, cfg, wyckoff, pa,
  );

  // ── EMA Trend Gate — hard filter, applied after scoring ───────────────
  // A trade is only allowed when the EMA trend aligns with the SMC signal.
  // Rules:
  //   • BULLISH trend (price > EMA50 > EMA100) → only BUY signals pass
  //   • BEARISH trend (price < EMA50 < EMA100) → only SELL signals pass
  //   • NEUTRAL trend → signal passes but score is reduced by 30%
  //     (choppy market; SMC+PA evidence still valid but lower conviction)
  //   • MODERATE strength → score reduced by 10% (EMA200 not yet aligned)
  //   • STRONG strength   → score unchanged (all three EMAs confirm direction)
  const emaResult = analyzeTrend(candles);

  if (
    (smcSignal === 'BUY'  && emaResult.trend === 'BEARISH') ||
    (smcSignal === 'SELL' && emaResult.trend === 'BULLISH')
  ) {
    // Trend directly opposes the signal — block the trade
    smcSignal = 'NEUTRAL';
    smcScore  = 0;
  } else if (smcSignal !== 'NEUTRAL') {
    if (emaResult.trend === 'NEUTRAL') {
      smcScore = +(smcScore * 0.70).toFixed(2); // choppy market penalty
    } else if (emaResult.strength === 'MODERATE') {
      smcScore = +(smcScore * 0.90).toFixed(2); // EMA200 not yet aligned
    }
    // STRONG: no penalty — full conviction
  }

  // ── Entry Filter Gate — minimum N independent confirmations ──────────
  // Counts how many of the 4 systems (SMC, Trend, PA, Wyckoff) vote for
  // the same direction.  If fewer than minConfirmations agree, no trade opens.
  const filter = applyEntryFilter(
    smcSignal,
    emaResult.trend,
    pa.paSignal,
    wyckoff.wyckoffSignal,
    minConfirmations,
  );

  if (!filter.allowed) {
    smcSignal = 'NEUTRAL';
    smcScore  = 0;
  }

  // ── Quality Filter Gate — session, range, late entry, probability ─────
  // Only runs when the signal survived all previous gates (saves CPU).
  if (smcSignal !== 'NEUTRAL') {
    const lastBosBarIndex = bosSignals.length > 0
      ? bosSignals[bosSignals.length - 1].barIndex
      : null;

    const quality = applyQualityFilter(candles, smcSignal, smcScore, lastBosBarIndex);

    if (!quality.allowed) {
      smcSignal = 'NEUTRAL';
      smcScore  = 0;
    }
  }

  return {
    timeframe,
    timestamp:        candles[candles.length - 1].time,
    currentPrice:     +currentPrice.toFixed(2),
    trend:            finalTrend,
    bosSignals,
    chochSignals,
    orderBlocks,
    fairValueGaps,
    liquiditySweeps,
    equalHighs,
    equalLows,
    mitigationBlocks,
    smcSignal,
    smcScore,
  };
}

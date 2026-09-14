// Price Action Engine — XAUUSD scalping optimized (M1 / M5)
// Role: CONFIRMATION ONLY — never triggers a trade on its own.
//       Results feed into smcEngine.computeSmcSignal as a bonus weight.
//
// Detects on the most recent candles:
//   Patterns  — Engulf, Pin Bar, Strong Candle
//   Levels    — Supply/Demand zones, Support/Resistance
//   Context   — Valid Breakout, Fake Breakout, Pullback

import { OHLCV } from './goldEngine.js';

// ===== CONFIG =====

interface PaConfig {
  // Candle pattern thresholds
  pinBarWickRatio: number;   // lower/upper wick must be ≥ X × body (pin bar)
  pinBarBodyMaxRatio: number;// body / range ≤ this for pin bar (small body)
  strongBodyRatio: number;   // body / range ≥ this for "strong" candle
  strongBodyAtrMult: number; // body must also be ≥ this × ATR (size filter)
  engulfBodyRatio: number;   // engulfing body must exceed prior body by this ratio

  // Level detection
  levelLookback: number;     // bars back to scan for S/R and supply/demand
  levelTolerance: number;    // price distance (cents) to qualify as "near" a level
  minLevelTouches: number;   // min touches to confirm a S/R level

  // Breakout / pullback
  breakoutMinBody: number;   // breakout candle body ≥ this (absolute cents)
  breakoutBodyRatio: number; // breakout candle body / range ≥ this
  fakeRetraceBars: number;   // bars to look back for fake-breakout reversal
  pullbackZonePct: number;   // pullback: price within this % of a key level
  atrPeriod: number;
}

const CFG_M1: PaConfig = {
  pinBarWickRatio:    2.0,   // wick ≥ 2× body
  pinBarBodyMaxRatio: 0.30,  // body ≤ 30% of range
  strongBodyRatio:    0.60,  // body ≥ 60% of range
  strongBodyAtrMult:  0.40,  // body ≥ 40% of ATR
  engulfBodyRatio:    1.05,  // must exceed prior body by 5%

  levelLookback:      40,    // 40 minutes of M1 data
  levelTolerance:     0.20,  // 20 cents = "near" a level
  minLevelTouches:    2,

  breakoutMinBody:    0.12,
  breakoutBodyRatio:  0.50,
  fakeRetraceBars:    3,
  pullbackZonePct:    0.0015,// 0.15% of price
  atrPeriod:          10,
};

const CFG_M5: PaConfig = {
  pinBarWickRatio:    2.0,
  pinBarBodyMaxRatio: 0.30,
  strongBodyRatio:    0.60,
  strongBodyAtrMult:  0.40,
  engulfBodyRatio:    1.05,

  levelLookback:      30,    // 150 minutes of M5 data
  levelTolerance:     0.35,
  minLevelTouches:    2,

  breakoutMinBody:    0.20,
  breakoutBodyRatio:  0.50,
  fakeRetraceBars:    3,
  pullbackZonePct:    0.0015,
  atrPeriod:          10,
};

// ===== OUTPUT =====

export interface PriceActionResult {
  // ── Candle patterns (last 2 bars) ─────────────────────────────────────
  bullishEngulf:   boolean;  // bullish candle fully engulfs prior bearish body
  bearishEngulf:   boolean;  // bearish candle fully engulfs prior bullish body
  bullishPinBar:   boolean;  // long lower wick, small body at top → rejection of lows
  bearishPinBar:   boolean;  // long upper wick, small body at bottom → rejection of highs
  strongBullish:   boolean;  // large-body bullish candle closing near its high
  strongBearish:   boolean;  // large-body bearish candle closing near its low

  // ── Level context ──────────────────────────────────────────────────────
  nearDemandZone:  boolean;  // current price is near a demand zone
  nearSupplyZone:  boolean;  // current price is near a supply zone
  nearSupport:     boolean;  // current price is near a confirmed support level
  nearResistance:  boolean;  // current price is near a confirmed resistance level

  // ── Breakout / Pullback ────────────────────────────────────────────────
  validBullBreakout:  boolean; // clean upside break of resistance with body confirmation
  validBearBreakout:  boolean; // clean downside break of support with body confirmation
  fakeBullBreakout:   boolean; // price spiked above resistance then reversed back inside
  fakeBearBreakout:   boolean; // price spiked below support then reversed back inside
  bullishPullback:    boolean; // price pulled back to a demand zone / support after rally
  bearishPullback:    boolean; // price pulled back to a supply zone / resistance after drop

  // ── Composite ─────────────────────────────────────────────────────────
  paSignal: 'BUY' | 'SELL' | 'NEUTRAL';
  paScore:  number; // 0–1 confirmation strength
}

// ===== HELPERS =====

function calcATR(candles: OHLCV[], period: number): number {
  const slice = candles.slice(-period - 1);
  let atr = 0;
  for (let i = 1; i < slice.length; i++) {
    const c = slice[i], p = slice[i - 1];
    atr += Math.max(c.high - c.low, Math.abs(c.high - p.close), Math.abs(c.low - p.close));
  }
  return atr / period;
}

function body(c: OHLCV): number { return Math.abs(c.close - c.open); }
function range(c: OHLCV): number { return c.high - c.low; }
function bodyRatio(c: OHLCV): number { return range(c) > 0 ? body(c) / range(c) : 0; }
function isBull(c: OHLCV): boolean { return c.close > c.open; }
function isBear(c: OHLCV): boolean { return c.close < c.open; }
function upperWick(c: OHLCV): number { return c.high - Math.max(c.open, c.close); }
function lowerWick(c: OHLCV): number { return Math.min(c.open, c.close) - c.low; }

// ===== CANDLE PATTERNS =====
// Evaluated on the most recent 1–2 candles only.

function detectPatterns(candles: OHLCV[], cfg: PaConfig, atr: number): {
  bullishEngulf: boolean; bearishEngulf: boolean;
  bullishPinBar: boolean; bearishPinBar: boolean;
  strongBullish: boolean; strongBearish: boolean;
} {
  const n  = candles.length;
  const c0 = candles[n - 1]; // current (last) candle
  const c1 = candles[n - 2]; // previous candle

  // ── Engulf ────────────────────────────────────────────────────────────
  // Current candle's body completely contains the previous candle's body.
  // Minimum size: engulfing body > prior body × engulfBodyRatio.
  const c0Body = body(c0), c1Body = body(c1);
  const bullEngulf =
    isBull(c0) && isBear(c1) &&
    c0.open <= c1.close && c0.close >= c1.open &&
    c0Body >= c1Body * cfg.engulfBodyRatio;

  const bearEngulf =
    isBear(c0) && isBull(c1) &&
    c0.open >= c1.close && c0.close <= c1.open &&
    c0Body >= c1Body * cfg.engulfBodyRatio;

  // ── Pin Bar ───────────────────────────────────────────────────────────
  // Small body (≤ 30% of range) with a wick ≥ 2× the body on one side.
  // The small body must sit at the opposite end of the wick.
  // Also require minimum total candle size to filter out micro-dojis.
  const r0 = range(c0);
  const minSize = atr * 0.25; // at least 25% of ATR — avoids noise candles

  const bullPin =
    r0 >= minSize &&
    bodyRatio(c0) <= cfg.pinBarBodyMaxRatio &&
    lowerWick(c0) >= body(c0) * cfg.pinBarWickRatio && // long lower wick
    upperWick(c0) <= lowerWick(c0) * 0.4;              // minimal upper wick

  const bearPin =
    r0 >= minSize &&
    bodyRatio(c0) <= cfg.pinBarBodyMaxRatio &&
    upperWick(c0) >= body(c0) * cfg.pinBarWickRatio && // long upper wick
    lowerWick(c0) <= upperWick(c0) * 0.4;              // minimal lower wick

  // ── Strong Candle ─────────────────────────────────────────────────────
  // Body dominates the range AND body is meaningful relative to ATR.
  // Bullish: closes in top 20% of range; Bearish: closes in bottom 20%.
  const topOfRange    = c0.low + r0 * 0.80;
  const bottomOfRange = c0.low + r0 * 0.20;

  const strongBull =
    isBull(c0) &&
    bodyRatio(c0) >= cfg.strongBodyRatio &&
    body(c0) >= atr * cfg.strongBodyAtrMult &&
    c0.close >= topOfRange;

  const strongBear =
    isBear(c0) &&
    bodyRatio(c0) >= cfg.strongBodyRatio &&
    body(c0) >= atr * cfg.strongBodyAtrMult &&
    c0.close <= bottomOfRange;

  return {
    bullishEngulf: bullEngulf,
    bearishEngulf: bearEngulf,
    bullishPinBar: bullPin,
    bearishPinBar: bearPin,
    strongBullish: strongBull,
    strongBearish: strongBear,
  };
}

// ===== SUPPORT / RESISTANCE LEVELS =====
// Scans the lookback window for swing highs (resistance) and swing lows (support)
// that have been tested at least minLevelTouches times within the tolerance band.
// Returns confirmed levels as price values.

function detectSRLevels(
  candles: OHLCV[],
  cfg: PaConfig,
): { supportLevels: number[]; resistanceLevels: number[] } {
  const slice = candles.slice(-cfg.levelLookback);
  const n = slice.length;
  const tol = cfg.levelTolerance;

  // Collect all swing highs and lows within the slice
  const swingHighs: number[] = [];
  const swingLows:  number[] = [];
  for (let i = 2; i < n - 2; i++) {
    if (
      slice[i].high > slice[i - 1].high && slice[i].high > slice[i - 2].high &&
      slice[i].high > slice[i + 1].high && slice[i].high > slice[i + 2].high
    ) swingHighs.push(slice[i].high);
    if (
      slice[i].low < slice[i - 1].low && slice[i].low < slice[i - 2].low &&
      slice[i].low < slice[i + 1].low && slice[i].low < slice[i + 2].low
    ) swingLows.push(slice[i].low);
  }

  // Group swing points that fall within the tolerance band → S/R clusters
  function clusterLevels(prices: number[]): number[] {
    const confirmed: number[] = [];
    const used = new Set<number>();
    for (let i = 0; i < prices.length; i++) {
      if (used.has(i)) continue;
      const group = [prices[i]];
      for (let j = i + 1; j < prices.length; j++) {
        if (!used.has(j) && Math.abs(prices[j] - prices[i]) <= tol) {
          group.push(prices[j]);
          used.add(j);
        }
      }
      used.add(i);
      if (group.length >= cfg.minLevelTouches) {
        confirmed.push(group.reduce((a, b) => a + b, 0) / group.length);
      }
    }
    return confirmed;
  }

  return {
    resistanceLevels: clusterLevels(swingHighs),
    supportLevels:    clusterLevels(swingLows),
  };
}

// ===== SUPPLY / DEMAND ZONES =====
// A demand zone is the consolidation base immediately before a strong bullish impulse.
// A supply zone is the consolidation base immediately before a strong bearish impulse.
//
// Detection:
//   Scan recent candles for a "base" (2–4 tight candles) followed by a strong-body
//   impulse candle (≥ cfg.strongBodyRatio body/range AND body ≥ atr × 0.5).
//   The zone boundaries are [base_low, base_high].
//
// Optimized for XAUUSD M1/M5: uses a short lookback to stay relevant to
// recent institutional activity only.

function detectSupplyDemandZones(
  candles: OHLCV[],
  cfg: PaConfig,
  atr: number,
): { demandZones: Array<{ top: number; bottom: number }>; supplyZones: Array<{ top: number; bottom: number }> } {
  const slice = candles.slice(-cfg.levelLookback);
  const n = slice.length;
  const demandZones: Array<{ top: number; bottom: number }> = [];
  const supplyZones: Array<{ top: number; bottom: number }> = [];

  for (let i = 3; i < n - 1; i++) {
    const impulse = slice[i];
    const baseCandles = slice.slice(i - 3, i); // 3-bar base before impulse

    // Base quality: tight range (consolidated)
    const baseHigh = Math.max(...baseCandles.map(c => c.high));
    const baseLow  = Math.min(...baseCandles.map(c => c.low));
    const baseRange = baseHigh - baseLow;
    if (baseRange > atr * 1.5) continue; // base too wide — not a real zone

    // Strong impulse candle
    const impBody  = body(impulse);
    const impRange = range(impulse);
    const isStrongImpulse =
      impRange > 0 &&
      (impBody / impRange) >= cfg.strongBodyRatio &&
      impBody >= atr * 0.5;

    if (!isStrongImpulse) continue;

    if (isBull(impulse)) {
      // Demand zone: base before bullish impulse
      demandZones.push({ top: +baseHigh.toFixed(2), bottom: +baseLow.toFixed(2) });
    } else if (isBear(impulse)) {
      // Supply zone: base before bearish impulse
      supplyZones.push({ top: +baseHigh.toFixed(2), bottom: +baseLow.toFixed(2) });
    }
  }

  // Keep only the most recent 3 zones of each type
  return {
    demandZones: demandZones.slice(-3),
    supplyZones: supplyZones.slice(-3),
  };
}

// ===== BREAKOUT / FAKE BREAKOUT / PULLBACK =====
//
// VALID BREAKOUT — requires all of:
//   1. Current close is beyond a confirmed S/R level
//   2. Breakout candle has a strong body (≥ cfg.breakoutBodyRatio)
//   3. Body size meets minimum (cfg.breakoutMinBody) — filters micro-breaks
//
// FAKE BREAKOUT — the inverse:
//   In the last cfg.fakeRetraceBars candles, price CLOSED beyond a level
//   but then CLOSED back inside it.  This is the price-action equivalent of
//   SMC's Fake BOS and Wyckoff's Spring / Upthrust — now detected at the PA layer.
//
// PULLBACK — price is returning toward a key level after a directional move:
//   • Bull pullback: recent high significantly above current price AND current
//     price is within cfg.pullbackZonePct of a support or demand zone
//   • Bear pullback: recent low significantly below current price AND current
//     price is within pullbackZonePct of a resistance or supply zone

function detectBreakoutAndPullback(
  candles: OHLCV[],
  supportLevels: number[],
  resistanceLevels: number[],
  demandZones: Array<{ top: number; bottom: number }>,
  supplyZones:  Array<{ top: number; bottom: number }>,
  cfg: PaConfig,
  atr: number,
): {
  validBullBreakout:  boolean;
  validBearBreakout:  boolean;
  fakeBullBreakout:   boolean;
  fakeBearBreakout:   boolean;
  bullishPullback:    boolean;
  bearishPullback:    boolean;
} {
  const n = candles.length;
  const current = candles[n - 1];
  const currentPrice = current.close;

  let validBullBreakout  = false;
  let validBearBreakout  = false;
  let fakeBullBreakout   = false;
  let fakeBearBreakout   = false;
  let bullishPullback    = false;
  let bearishPullback    = false;

  // ── Valid / Fake Breakout ──────────────────────────────────────────────
  for (const level of resistanceLevels) {
    // Valid bullish breakout: current close > resistance with strong body
    if (
      currentPrice > level &&
      body(current) >= cfg.breakoutMinBody &&
      bodyRatio(current) >= cfg.breakoutBodyRatio &&
      isBull(current)
    ) { validBullBreakout = true; }

    // Fake bullish breakout: any recent candle closed above resistance then came back
    const recentSlice = candles.slice(n - cfg.fakeRetraceBars - 1, n - 1);
    for (const rc of recentSlice) {
      if (rc.close > level && currentPrice < level) { fakeBullBreakout = true; }
    }
  }

  for (const level of supportLevels) {
    // Valid bearish breakout: current close < support with strong body
    if (
      currentPrice < level &&
      body(current) >= cfg.breakoutMinBody &&
      bodyRatio(current) >= cfg.breakoutBodyRatio &&
      isBear(current)
    ) { validBearBreakout = true; }

    // Fake bearish breakout: recent candle closed below support then recovered
    const recentSlice = candles.slice(n - cfg.fakeRetraceBars - 1, n - 1);
    for (const rc of recentSlice) {
      if (rc.close < level && currentPrice > level) { fakeBearBreakout = true; }
    }
  }

  // ── Pullback ─────────────────────────────────────────────────────────
  // Recent trend context: compare close N bars ago vs now
  const trendLook = Math.min(10, n - 1);
  const priorClose = candles[n - 1 - trendLook].close;
  const trendUp   = currentPrice > priorClose * 1.002; // at least 0.2% up
  const trendDown = currentPrice < priorClose * 0.998;

  const pullZone = currentPrice * cfg.pullbackZonePct;

  // Bull pullback: uptrend but price has retraced near support/demand
  if (trendUp) {
    const nearSupport = supportLevels.some(l => Math.abs(currentPrice - l) <= pullZone);
    const nearDemand  = demandZones.some(z => currentPrice >= z.bottom - pullZone && currentPrice <= z.top + pullZone);
    if (nearSupport || nearDemand) bullishPullback = true;
  }

  // Bear pullback: downtrend but price has retraced near resistance/supply
  if (trendDown) {
    const nearResist = resistanceLevels.some(l => Math.abs(currentPrice - l) <= pullZone);
    const nearSupply = supplyZones.some(z => currentPrice >= z.bottom - pullZone && currentPrice <= z.top + pullZone);
    if (nearResist || nearSupply) bearishPullback = true;
  }

  return {
    validBullBreakout,
    validBearBreakout,
    fakeBullBreakout,
    fakeBearBreakout,
    bullishPullback,
    bearishPullback,
  };
}

// ===== COMPOSITE PA SIGNAL =====
//
// Tallies weighted evidence from all PA signals.
// Weights are calibrated for XAUUSD scalping where candle quality matters most:
//
//   Engulf           +1.5  (strong reversal evidence)
//   Pin Bar at level +1.5  (rejection signal with level confluence)
//   Pin Bar (no lvl) +0.8  (still relevant but less precise)
//   Strong candle    +1.0  (momentum confirmation)
//   Valid breakout   +1.0  (trend continuation with PA quality)
//   Pullback to zone +0.8  (high-probability entry location)
//   Near S/R or zone +0.5  (additional confluence)
//   Fake breakout   −1.5  (strong warning against the apparent direction)
//
// paScore is the normalised dominant score (0–1).
// paSignal only fires when paScore ≥ 0.30 — a low hurdle since PA is a
// confirmation layer; the SMC engine holds the primary threshold.

function computePaSignal(
  patterns: ReturnType<typeof detectPatterns>,
  breakouts: ReturnType<typeof detectBreakoutAndPullback>,
  nearDemandZone: boolean,
  nearSupplyZone: boolean,
  nearSupport: boolean,
  nearResistance: boolean,
): { paSignal: 'BUY' | 'SELL' | 'NEUTRAL'; paScore: number } {
  let buyScore  = 0;
  let sellScore = 0;

  // Patterns
  const { bullishEngulf, bearishEngulf, bullishPinBar, bearishPinBar, strongBullish, strongBearish } = patterns;

  if (bullishEngulf) buyScore  += 1.5;
  if (bearishEngulf) sellScore += 1.5;

  // Pin bar gets bonus when near a relevant level
  if (bullishPinBar) buyScore  += (nearSupport || nearDemandZone) ? 1.5 : 0.8;
  if (bearishPinBar) sellScore += (nearResistance || nearSupplyZone) ? 1.5 : 0.8;

  if (strongBullish) buyScore  += 1.0;
  if (strongBearish) sellScore += 1.0;

  // Breakout / Pullback
  const { validBullBreakout, validBearBreakout, fakeBullBreakout, fakeBearBreakout, bullishPullback, bearishPullback } = breakouts;

  if (validBullBreakout)  buyScore  += 1.0;
  if (validBearBreakout)  sellScore += 1.0;
  if (bullishPullback)    buyScore  += 0.8;
  if (bearishPullback)    sellScore += 0.8;

  // Level confluence
  if (nearDemandZone || nearSupport)    buyScore  += 0.5;
  if (nearSupplyZone || nearResistance) sellScore += 0.5;

  // Fake breakout penalty — applied to the direction that LOOKED like a break
  if (fakeBullBreakout) buyScore  -= 1.5; // bullish fake → penalise BUY
  if (fakeBearBreakout) sellScore -= 1.5; // bearish fake → penalise SELL

  // Floor scores at 0
  buyScore  = Math.max(0, buyScore);
  sellScore = Math.max(0, sellScore);

  const maxPossible = 5.3; // Engulf(1.5)+PinAtLevel(1.5)+Strong(1)+Breakout(1)+Pullback(0.8)+Zone(0.5)
  const dominant    = Math.max(buyScore, sellScore);
  const score       = +(dominant / maxPossible).toFixed(2);

  let paSignal: 'BUY' | 'SELL' | 'NEUTRAL' = 'NEUTRAL';
  if (buyScore  > sellScore && score >= 0.30) paSignal = 'BUY';
  if (sellScore > buyScore  && score >= 0.30) paSignal = 'SELL';

  return { paSignal, paScore: Math.min(1, score) };
}

// ===== MAIN ENTRY POINT =====

export function analyzePriceAction(
  candles: OHLCV[],
  timeframe: 'M1' | 'M5',
): PriceActionResult {
  const cfg = timeframe === 'M1' ? CFG_M1 : CFG_M5;

  const neutral: PriceActionResult = {
    bullishEngulf: false, bearishEngulf: false,
    bullishPinBar: false, bearishPinBar: false,
    strongBullish: false, strongBearish: false,
    nearDemandZone: false, nearSupplyZone: false,
    nearSupport: false, nearResistance: false,
    validBullBreakout: false, validBearBreakout: false,
    fakeBullBreakout: false, fakeBearBreakout: false,
    bullishPullback: false, bearishPullback: false,
    paSignal: 'NEUTRAL', paScore: 0,
  };

  // Need enough candles for ATR + level lookback
  if (candles.length < cfg.levelLookback + cfg.atrPeriod + 5) return neutral;

  const atr = calcATR(candles, cfg.atrPeriod);
  if (atr <= 0) return neutral;

  // ── Detect all components ─────────────────────────────────────────────
  const patterns = detectPatterns(candles, cfg, atr);

  const { supportLevels, resistanceLevels } = detectSRLevels(candles, cfg);
  const { demandZones, supplyZones }         = detectSupplyDemandZones(candles, cfg, atr);

  const currentPrice = candles[candles.length - 1].close;
  const tol = cfg.levelTolerance;

  const nearSupport    = supportLevels.some(l    => Math.abs(currentPrice - l) <= tol);
  const nearResistance = resistanceLevels.some(l  => Math.abs(currentPrice - l) <= tol);
  const nearDemandZone = demandZones.some(z =>
    currentPrice >= z.bottom - tol && currentPrice <= z.top + tol);
  const nearSupplyZone = supplyZones.some(z =>
    currentPrice >= z.bottom - tol && currentPrice <= z.top + tol);

  const breakouts = detectBreakoutAndPullback(
    candles, supportLevels, resistanceLevels, demandZones, supplyZones, cfg, atr,
  );

  const { paSignal, paScore } = computePaSignal(
    patterns, breakouts, nearDemandZone, nearSupplyZone, nearSupport, nearResistance,
  );

  return {
    ...patterns,
    nearDemandZone,
    nearSupplyZone,
    nearSupport,
    nearResistance,
    ...breakouts,
    paSignal,
    paScore,
  };
}

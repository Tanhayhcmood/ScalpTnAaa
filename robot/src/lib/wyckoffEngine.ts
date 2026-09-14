// Wyckoff Analysis Engine
// Optimized for XAUUSD M1 and M5 timeframes.
// Role: CONFIRMATION ONLY — never triggers a trade on its own.
//       Results are consumed by smcEngine.computeSmcSignal as a bonus weight.
//
// Detects:
//   • Accumulation — consolidation after a downtrend where demand absorbs supply
//   • Distribution — consolidation after an uptrend where supply overwhelms demand
//   • Spring       — false breakdown below range support that reverses up (bullish)
//   • Upthrust     — false breakout above range resistance that reverses down (bearish)
//   • Volume Confirmation — volume pattern validates the detected phase

import { OHLCV } from './goldEngine.js';

// ===== CONFIG =====

interface WyckoffConfig {
  rangeBars: number;       // bars to examine for the trading range
  trendBars: number;       // bars before the range to determine prior trend
  springMargin: number;    // max distance below support that still qualifies as Spring
  upthrustMargin: number;  // max distance above resistance that still qualifies as Upthrust
  minRangeTouches: number; // min touches of BOTH support and resistance to confirm a range
  maxRangePct: number;     // max (range / price) ratio — consolidation must be bounded
  minRangePct: number;     // min (range / price) ratio — filters out flat-line noise
  recentBars: number;      // look-back window within the range for Spring / Upthrust
}

const CFG_M1: WyckoffConfig = {
  rangeBars:       30,    // 30 minutes of M1 candles
  trendBars:       20,    // 20 bars before range to assess prior trend
  springMargin:    0.10,  // Spring wick may dip up to 10 cents below support
  upthrustMargin:  0.10,  // Upthrust wick may spike up to 10 cents above resistance
  minRangeTouches: 2,
  maxRangePct:     0.008, // range must be ≤0.8% of price (tight consolidation)
  minRangePct:     0.001, // range must be ≥0.1% of price (not a flat line)
  recentBars:      8,     // scan last 8 bars of range for Spring / Upthrust
};

const CFG_M5: WyckoffConfig = {
  rangeBars:       20,    // 100 minutes of M5 candles
  trendBars:       12,
  springMargin:    0.20,
  upthrustMargin:  0.20,
  minRangeTouches: 2,
  maxRangePct:     0.010,
  minRangePct:     0.001,
  recentBars:      6,
};

// ===== CALIBRATION =====
// Runtime-calibrated M5 config, derived from real OHLCV data.
// Set via calibrateM5Config() before running backtest.
let _calibratedM5: WyckoffConfig | null = null;

/**
 * Derive a WyckoffConfig for M5 from actual OHLCV data.
 *
 * Two key parameters are measured from the dataset:
 *   maxRangePct   — 65th percentile of 20-bar rolling (high-low)/price
 *                   across the full history. Captures genuine consolidation
 *                   windows without including strong trending moves.
 *   springMargin  — 80% of the median 14-bar ATR. A wick that dips no more
 *   upthrustMargin  than 80% of one bar's ATR below/above the range boundary
 *                   is a realistic shakeout on XAUUSD M5.
 */
export function calibrateM5Config(candles: OHLCV[]): WyckoffConfig {
  const n = candles.length;
  if (n < 200) return CFG_M5;

  // ── 1. Median 14-bar ATR ──────────────────────────────────────────
  const atrs: number[] = [];
  for (let i = 20; i < n; i += 30) {
    const lo = Math.max(1, i - 13);
    let sum = 0, cnt = 0;
    for (let j = lo; j <= i; j++) {
      const c = candles[j], p = candles[j - 1];
      sum += Math.max(c.high - c.low, Math.abs(c.high - p.close), Math.abs(c.low - p.close));
      cnt++;
    }
    if (cnt > 0) atrs.push(sum / cnt);
  }
  atrs.sort((a, b) => a - b);
  const medianATR = atrs[Math.floor(atrs.length * 0.50)] ?? 5;

  // ── 2. Rolling 20-bar range/price distribution ───────────────────
  // Sample every 10 bars (~34 k samples over 5-year M5 data).
  // We use the 75th percentile because:
  //   • Windows below p75 cover the quieter/consolidating market states
  //   • Windows above p75 are predominantly trending / volatile
  //   • This gives a maxRangePct that admits most genuine consolidations
  //     while excluding strong trending periods (which would inflate the cap).
  //
  // In practice on XAUUSD M5 2021-2025 this resolves to ~1.3-1.8 %, i.e.
  // $26-$36 range at a mid-price of $2,000 — much more realistic than the
  // hardcoded 1.0 % ($20) that was blocking almost all phase detections.
  const rangePcts: number[] = [];
  for (let i = 32; i < n; i += 10) {
    const slice = candles.slice(i - 20, i);
    const hi    = Math.max(...slice.map(c => c.high));
    const lo    = Math.min(...slice.map(c => c.low));
    const price = slice[slice.length - 1].close;
    if (price > 0) rangePcts.push((hi - lo) / price);
  }
  rangePcts.sort((a, b) => a - b);

  const p50 = rangePcts[Math.floor(rangePcts.length * 0.50)] ?? 0.008;
  const p65 = rangePcts[Math.floor(rangePcts.length * 0.65)] ?? 0.010;
  const p75 = rangePcts[Math.floor(rangePcts.length * 0.75)] ?? 0.013;
  const p85 = rangePcts[Math.floor(rangePcts.length * 0.85)] ?? 0.018;
  const p90 = rangePcts[Math.floor(rangePcts.length * 0.90)] ?? 0.022;

  // Use p85 as the consolidation ceiling.
  // At this percentile ~85% of all 20-bar windows pass the range filter,
  // which includes essentially all genuine consolidation phases while
  // still excluding the widest trending windows (top 15% by range).
  const maxRangePct = +p85.toFixed(5);

  // spring/upthrust margin: 80% of one typical ATR bar.
  // On XAUUSD M5 medianATR ≈ $3-6, so margin ≈ $2.5-5 — realistic shakeout depth.
  const margin = +(medianATR * 0.80).toFixed(2);

  console.log(`  Range pct percentiles: p50=${(p50*100).toFixed(3)}% p65=${(p65*100).toFixed(3)}% p75=${(p75*100).toFixed(3)}% p85=${(p85*100).toFixed(3)}% p90=${(p90*100).toFixed(3)}%`);
  console.log(`  medianATR=${medianATR.toFixed(2)}  →  springMargin=${margin}`);

  return {
    rangeBars:       20,
    trendBars:       12,
    springMargin:    margin,
    upthrustMargin:  margin,
    minRangeTouches: 2,
    maxRangePct,
    minRangePct:     0.0005,  // slightly looser than 0.001 to catch tight squeezes
    recentBars:      8,       // wider spring/upthrust scan (was 6)
  };
}

/** Apply a calibrated config for all subsequent M5 analyzeWyckoff calls. */
export function setCalibratedM5Config(cfg: WyckoffConfig): void {
  _calibratedM5 = cfg;
}

/** Return the currently active M5 config (calibrated or default). */
export function getActiveM5Config(): WyckoffConfig {
  return _calibratedM5 ?? CFG_M5;
}

// ===== OUTPUT =====

export interface WyckoffResult {
  phase:           'ACCUMULATION' | 'DISTRIBUTION' | 'NEUTRAL';
  spring:          boolean;  // bullish Spring found inside range
  upthrust:        boolean;  // bearish Upthrust found inside range
  volumeConfirmed: boolean;  // volume pattern validates the detected phase
  wyckoffSignal:   'BUY' | 'SELL' | 'NEUTRAL';
  wyckoffScore:    number;   // 0–1 confirmation strength
}

// ===== HELPERS =====

function avg(arr: number[]): number {
  return arr.length > 0 ? arr.reduce((a, b) => a + b, 0) / arr.length : 0;
}

// ===== PHASE DETECTION =====
//
// A valid Wyckoff trading range requires:
//   1. A bounded consolidation: high - low of the range window is within
//      [minRangePct, maxRangePct] of current price.
//   2. Minimum touches of both the resistance (top 20% of range) and
//      support (bottom 20% of range) to confirm it's a true range, not
//      a one-directional drift.
//
// Prior trend classification:
//   • The trendBars window immediately BEFORE the range is used.
//   • FALLING prior trend (first close > last close) → Accumulation candidate
//   • RISING prior trend (first close < last close)  → Distribution candidate
//
// The phase is only returned when both conditions hold.

function detectPhase(
  candles: OHLCV[],
  cfg: WyckoffConfig,
): {
  phase: 'ACCUMULATION' | 'DISTRIBUTION' | 'NEUTRAL';
  support: number;
  resistance: number;
  rangeStart: number;  // bar index where the range window begins
} {
  const n = candles.length;
  if (n < cfg.rangeBars + cfg.trendBars) {
    return { phase: 'NEUTRAL', support: 0, resistance: 0, rangeStart: 0 };
  }

  const rangeStart = n - cfg.rangeBars;
  const rangeCandles = candles.slice(rangeStart);

  const support    = Math.min(...rangeCandles.map(c => c.low));
  const resistance = Math.max(...rangeCandles.map(c => c.high));
  const rangeSize  = resistance - support;
  const midPrice   = candles[n - 1].close;

  // Range quality: must be bounded but not flat
  const rangePct = rangeSize / midPrice;
  if (rangePct < cfg.minRangePct || rangePct > cfg.maxRangePct) {
    return { phase: 'NEUTRAL', support, resistance, rangeStart };
  }

  // Minimum touch validation
  const touchBand = rangeSize * 0.20; // 20% band from each extreme
  const topBand   = resistance - touchBand;
  const botBand   = support + touchBand;

  const topTouches = rangeCandles.filter(c => c.high >= topBand).length;
  const botTouches = rangeCandles.filter(c => c.low  <= botBand).length;

  if (topTouches < cfg.minRangeTouches || botTouches < cfg.minRangeTouches) {
    return { phase: 'NEUTRAL', support, resistance, rangeStart };
  }

  // Prior trend: window immediately before the range
  const trendStart   = rangeStart - cfg.trendBars;
  const trendCandles = candles.slice(Math.max(0, trendStart), rangeStart);
  if (trendCandles.length < 4) {
    return { phase: 'NEUTRAL', support, resistance, rangeStart };
  }

  const trendFirstClose = trendCandles[0].close;
  const trendLastClose  = trendCandles[trendCandles.length - 1].close;
  const trendMove       = trendLastClose - trendFirstClose;
  // Require a meaningful trend: at least 0.3% move
  const trendPct = Math.abs(trendMove) / trendFirstClose;

  // Require a meaningful prior trend: at least 0.2% move in trendBars.
  // Reduced from 0.3% to allow gentler consolidation-preceding trends that
  // are common in real XAUUSD M5 data (vs synthetic GBM that overshoots).
  let phase: 'ACCUMULATION' | 'DISTRIBUTION' | 'NEUTRAL' = 'NEUTRAL';
  if (trendPct >= 0.002) {
    phase = trendMove < 0 ? 'ACCUMULATION' : 'DISTRIBUTION';
  }

  return { phase, support, resistance, rangeStart };
}

// ===== SPRING DETECTION =====
//
// A Spring is a false breakdown below range support with an immediate reversal.
//
// CRITICAL DESIGN POINT:
//   Support is established from the FIRST (rangeBars − recentBars) bars of the
//   range window.  The LAST recentBars bars are the Spring scan window.
//   This is the only way the check `c.low < support` can ever be true: if we
//   computed support from ALL range bars (including the scan bars), the minimum
//   low of the range IS support, so no bar can ever dip below it.
//
// Conditions (all must hold):
//   1. The candle's low dips below the established support band.
//   2. The dip is within springMargin (a deep breakdown is not a Spring).
//   3. The candle's close is ABOVE support (rejection confirmed on same bar).
//   4. Volume on the Spring bar is above the average volume of the full range
//      (shakeout requires enough volume to clear resting sell-stops).

function detectSpring(
  candles: OHLCV[],
  rangeStart: number,
  cfg: WyckoffConfig,
): boolean {
  const n           = candles.length;
  const rangeCandles = candles.slice(rangeStart, n);
  const totalBars   = rangeCandles.length;

  // Need enough bars to split into an "established" zone and a scan zone
  const establishBars = Math.max(4, totalBars - cfg.recentBars);
  if (establishBars <= 0) return false;

  // Support established from the early portion of the range (before the scan window)
  const earlyRange = rangeCandles.slice(0, establishBars);
  const support    = Math.min(...earlyRange.map(c => c.low));

  // Average volume over the full range
  const avgVol = avg(rangeCandles.map(c => c.volume));

  // Scan the last recentBars of the range
  const scanStart = rangeStart + establishBars;
  for (let i = scanStart; i < n; i++) {
    const c = candles[i];
    const dipsBelowSupport   = c.low < support;
    const withinSpringMargin = c.low >= support - cfg.springMargin;
    const closesAboveSupport = c.close > support;
    const volumeIsHigh       = c.volume > avgVol;

    if (dipsBelowSupport && withinSpringMargin && closesAboveSupport && volumeIsHigh) {
      return true;
    }
  }
  return false;
}

// ===== UPTHRUST DETECTION =====
//
// An Upthrust (UT / UTAD) is a false breakout above range resistance that reverses.
//
// Same split-window design as detectSpring: resistance established from the
// early portion of the range, upthrust scan on the last recentBars.
//
// Conditions (all must hold):
//   1. The candle's high spikes above the established resistance.
//   2. The spike is within upthrustMargin (a deep breakout is not an Upthrust).
//   3. The candle's close is BELOW resistance (rejection on the same bar).
//   4. Volume is above range average (distribution requires supply volume).

function detectUpthrust(
  candles: OHLCV[],
  rangeStart: number,
  cfg: WyckoffConfig,
): boolean {
  const n            = candles.length;
  const rangeCandles = candles.slice(rangeStart, n);
  const totalBars    = rangeCandles.length;

  const establishBars = Math.max(4, totalBars - cfg.recentBars);
  if (establishBars <= 0) return false;

  // Resistance established from the early portion of the range
  const earlyRange   = rangeCandles.slice(0, establishBars);
  const resistance   = Math.max(...earlyRange.map(c => c.high));

  const avgVol = avg(rangeCandles.map(c => c.volume));

  const scanStart = rangeStart + establishBars;
  for (let i = scanStart; i < n; i++) {
    const c = candles[i];
    const spikesAboveResistance = c.high > resistance;
    const withinUpthrustMargin  = c.high <= resistance + cfg.upthrustMargin;
    const closesBelowResistance = c.close < resistance;
    const volumeIsHigh          = c.volume > avgVol;

    if (spikesAboveResistance && withinUpthrustMargin && closesBelowResistance && volumeIsHigh) {
      return true;
    }
  }
  return false;
}

// ===== VOLUME CONFIRMATION =====
//
// Volume analysis within the trading range reveals whether demand or supply
// is dominant — which predicts whether price will break up or down.
//
// Accumulation (demand absorbs supply):
//   Up bars (close > open) carry MORE total volume than down bars.
//   Ratio threshold: upVolume / totalVolume > 0.55
//
// Distribution (supply overwhelms demand):
//   Down bars (close < open) carry MORE total volume than up bars.
//   Ratio threshold: downVolume / totalVolume > 0.55
//
// A phase with the wrong volume pattern is marked as unconfirmed.

function confirmVolume(
  candles: OHLCV[],
  phase: 'ACCUMULATION' | 'DISTRIBUTION' | 'NEUTRAL',
  rangeStart: number,
): boolean {
  if (phase === 'NEUTRAL') return false;

  const rangeCandles = candles.slice(rangeStart);
  let upVol   = 0;
  let downVol = 0;

  for (const c of rangeCandles) {
    if (c.close > c.open) upVol   += c.volume;
    else                   downVol += c.volume;
  }

  const totalVol = upVol + downVol;
  if (totalVol === 0) return false;

  if (phase === 'ACCUMULATION') return (upVol / totalVol) > 0.55;
  if (phase === 'DISTRIBUTION') return (downVol / totalVol) > 0.55;
  return false;
}

// ===== MAIN ENTRY POINT =====

export function analyzeWyckoff(
  candles: OHLCV[],
  timeframe: 'M1' | 'M5',
): WyckoffResult {
  const cfg = timeframe === 'M1' ? CFG_M1 : (_calibratedM5 ?? CFG_M5);

  const neutral: WyckoffResult = {
    phase:           'NEUTRAL',
    spring:          false,
    upthrust:        false,
    volumeConfirmed: false,
    wyckoffSignal:   'NEUTRAL',
    wyckoffScore:    0,
  };

  if (candles.length < cfg.rangeBars + cfg.trendBars) return neutral;

  // ── Phase detection ────────────────────────────────────────────────────
  const { phase, support: _support, resistance: _resistance, rangeStart } = detectPhase(candles, cfg);
  if (phase === 'NEUTRAL') return neutral;

  // ── Spring / Upthrust ─────────────────────────────────────────────────
  const spring   = detectSpring(candles, rangeStart, cfg);
  const upthrust = detectUpthrust(candles, rangeStart, cfg);

  // ── Volume confirmation ────────────────────────────────────────────────
  const volumeConfirmed = confirmVolume(candles, phase, rangeStart);

  // ── Signal and score ──────────────────────────────────────────────────
  // Each positive element adds to confidence:
  //   phase match       → 0.30 base
  //   Spring/Upthrust   → +0.40 (strong structural event)
  //   volume confirmed  → +0.30
  // Score is capped at 1.0 and only emitted when phase is non-neutral.

  let wyckoffSignal: 'BUY' | 'SELL' | 'NEUTRAL' = 'NEUTRAL';
  let scoreRaw = 0.30; // base for detecting a valid phase

  if (phase === 'ACCUMULATION') {
    wyckoffSignal = 'BUY';
    if (spring)          scoreRaw += 0.40;
    if (volumeConfirmed) scoreRaw += 0.30;
  } else {
    wyckoffSignal = 'SELL';
    if (upthrust)        scoreRaw += 0.40;
    if (volumeConfirmed) scoreRaw += 0.30;
  }

  // If Spring/Upthrust contradicts the phase (e.g. Spring in Distribution),
  // downgrade the signal to NEUTRAL — mixed structure is unreliable.
  const contradiction =
    (phase === 'ACCUMULATION' && upthrust && !spring) ||
    (phase === 'DISTRIBUTION' && spring && !upthrust);

  if (contradiction) {
    return { phase, spring, upthrust, volumeConfirmed, wyckoffSignal: 'NEUTRAL', wyckoffScore: 0 };
  }

  return {
    phase,
    spring,
    upthrust,
    volumeConfirmed,
    wyckoffSignal,
    wyckoffScore: +Math.min(1, scoreRaw).toFixed(2),
  };
}

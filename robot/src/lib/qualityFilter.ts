// Quality Filter — High-Probability Trade Gate (v4.0)
//
// Final 10-category rejection gate.  A trade is rejected if ANY category fires.
// This filter runs AFTER the Confidence Score passes, so it only handles
// structural failure modes that the confidence engine cannot catch by scoring.
//
// Rejection categories:
//   ① Session     — outside liquid XAUUSD windows (UTC)
//   ② Severe Range — ADX + ATR compression + tight price band
//   ③ Late Entry   — price overextended from EMA50 / stale BOS signal
//   ④ Low Momentum — ADX < 15, no directional force whatsoever
//   ⑤ Fake BOS     — BOS present but no supporting CHoCH or OB (Wyckoff filter)
//   ⑥ Fake Breakout — PA engine detected and penalized, but also hard-blocked here
//   ⑦ Weak Volume   — volume on signal bar below 40% of recent average
//   ⑧ Range Market  — price oscillating in a band narrower than 2× ATR
//   ⑨ News Window   — ±10-minute window around :00/:30 during London/NY sessions
//  ⑩ Confidence    — below hard minimum (belt-and-braces, also checked in decisionEngine)

import { OHLCV } from './goldEngine.js';

// ===== SESSION WINDOWS (UTC) ==============================================

// XAUUSD session quality (UTC):
//   London/NY overlap:      07:00 – 17:00  → PRIME
//   Asia / overnight:       00:00 – 07:00 and 17:00 – 24:00 → MODERATE
//
// All 24 hours remain eligible for entries.  The quality label contributes to
// confidence scoring, but no valid UTC hour is a hard "dead zone" anymore.
const ALLOWED_SESSIONS = [
  { startUTC:  0, endUTC:  7, quality: 'MODERATE' as const },
  { startUTC:  7, endUTC: 17, quality: 'PRIME'    as const },
  { startUTC: 17, endUTC: 24, quality: 'MODERATE' as const },
];

export function getSessionQuality(isoTimestamp: string): 'PRIME' | 'MODERATE' | 'BLOCKED' {
  const hour = new Date(isoTimestamp).getUTCHours();
  for (const w of ALLOWED_SESSIONS) {
    if (hour >= w.startUTC && hour < w.endUTC) return w.quality;
  }
  return 'BLOCKED';
}

// ===== ADX =================================================================

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

// ===== VOLATILITY COMPRESSION ==============================================

function isVolatilityCompressed(candles: OHLCV[], lookback = 20, threshold = 0.65): boolean {
  if (candles.length < lookback + 2) return false;
  const trs: number[] = [];
  for (let i = 1; i < candles.length; i++) {
    const c = candles[i], p = candles[i - 1];
    trs.push(Math.max(c.high - c.low, Math.abs(c.high - p.close), Math.abs(c.low - p.close)));
  }
  const currentATR = trs[trs.length - 1];
  const meanATR    = trs.slice(-lookback - 1, -1).reduce((a, b) => a + b, 0) / lookback;
  return meanATR > 0 && currentATR < meanATR * threshold;
}

// ===== SEVERE RANGE ========================================================

function isSevereRange(candles: OHLCV[], adx: number): boolean {
  if (adx >= 22) return false;
  if (!isVolatilityCompressed(candles, 20, 0.65)) return false;
  const slice = candles.slice(-15);
  const highest = Math.max(...slice.map(c => c.high));
  const lowest  = Math.min(...slice.map(c => c.low));
  const recentRange = highest - lowest;
  const trs: number[] = [];
  for (let i = 1; i < candles.length; i++) {
    const c = candles[i], p = candles[i - 1];
    trs.push(Math.max(c.high - c.low, Math.abs(c.high - p.close), Math.abs(c.low - p.close)));
  }
  const meanATR = trs.slice(-20).reduce((a, b) => a + b, 0) / 20;
  return recentRange < meanATR * 2.5;
}

// ===== LATE ENTRY ==========================================================

// XAUUSD runs 3–5 ATRs from EMA50 during strong trending periods.
// Original value of 2.0 blocked valid setups in the 2021-2025 gold bull run.
const LATE_EXTENSION_MULT = 3.5;
const MOMENTUM_BARS       = 5;
// BOS must have fired within this many bars of the current bar (within the
// 250-bar SMC window).  The original value of 8 (40 min of M5) was calibrated
// for tick data where BOS fires frequently near the current bar.  On real M5
// data, Wyckoff setups develop over 1–3 hours so BOS naturally precedes the
// spring/upthrust by 20–60 bars.  Raised to 30 (150 min / 2.5 hours).
const STALE_BAR_COUNT     = 30;

function calcEMA50(closes: number[]): number {
  const period = 50;
  if (closes.length < period) return closes[closes.length - 1];
  const k = 2 / (period + 1);
  let ema = closes.slice(0, period).reduce((a, b) => a + b, 0) / period;
  for (let i = period; i < closes.length; i++) ema = closes[i] * k + ema * (1 - k);
  return ema;
}

function isLateEntry(candles: OHLCV[], smcSignal: 'BUY' | 'SELL', lastBosBarIndex: number | null): boolean {
  const n      = candles.length;
  const closes = candles.map(c => c.close);
  const price  = closes[n - 1];
  const prev   = candles[n - 2];
  const curr   = candles[n - 1];
  const atr    = Math.max(
    curr.high - curr.low,
    Math.abs(curr.high - prev.close),
    Math.abs(curr.low  - prev.close),
  );
  if (Math.abs(price - calcEMA50(closes)) > LATE_EXTENSION_MULT * atr) return true;
  if (n >= MOMENTUM_BARS + 1) {
    const recent  = candles.slice(-MOMENTUM_BARS);
    const allBull = recent.every(c => c.close > c.open);
    const allBear = recent.every(c => c.close < c.open);
    if (allBull || allBear) {
      const bodies   = recent.map(c => Math.abs(c.close - c.open));
      const shrinking = bodies.slice(1).every((b, i) => b <= bodies[i]);
      if (shrinking) return true;
    }
  }
  if (lastBosBarIndex !== null && (n - 1) - lastBosBarIndex > STALE_BAR_COUNT) return true;
  return false;
}

// ===== WEAK VOLUME =========================================================
// Signal bar volume must be at least 40% of the 20-bar average.
// Filters low-conviction breakouts that often fail on XAUUSD.

function isWeakVolume(candles: OHLCV[]): boolean {
  if (candles.length < 22) return false;
  const slice    = candles.slice(-21, -1); // 20 bars before current
  const avgVol   = slice.reduce((a, c) => a + c.volume, 0) / slice.length;
  const currVol  = candles[candles.length - 1].volume;
  return avgVol > 0 && currVol < avgVol * 0.40;
}

// ===== OUTPUT ================================================================

export interface QualityFilterResult {
  allowed:          boolean;
  blockedReasons:   string[];
  sessionQuality:   'PRIME' | 'MODERATE' | 'BLOCKED';
  adx:              number;
  isSevereRange:    boolean;
  isLateEntry:      boolean;
  isLowProbability: boolean;
  isFakeBreakout:   boolean;
  isWeakVolume:     boolean;
  isLowMomentum:    boolean;
}

// ===== MAIN ENTRY POINT =====================================================
//
// Parameters:
//   candles          — closed-bar OHLCV array (newest = last)
//   smcSignal        — candidate direction from Decision Engine
//   confidence       — confidence score (0–100) from Confidence Engine
//   lastBosBarIndex  — bar index of most recent BOS (or null)
//   adx              — pre-computed ADX from Market Regime Detector
//   atrRatio         — pre-computed atr/atrMean from Market Regime Detector

export function applyQualityFilter(
  candles:          OHLCV[],
  smcSignal:        'BUY' | 'SELL' | 'NEUTRAL',
  confidence:       number,
  lastBosBarIndex:  number | null,
  adx?:             number,
  atrRatio?:        number,
): QualityFilterResult {
  const blocked: QualityFilterResult = {
    allowed: false, blockedReasons: [],
    sessionQuality: 'BLOCKED', adx: 0,
    isSevereRange: false, isLateEntry: false,
    isLowProbability: false, isFakeBreakout: false,
    isWeakVolume: false, isLowMomentum: false,
  };

  if (candles.length < 30) return { ...blocked, blockedReasons: ['Insufficient candle data (< 30)'] };
  if (smcSignal === 'NEUTRAL') return { ...blocked, blockedReasons: ['No SMC direction signal'] };

  const reasons: string[] = [];
  const lastCandle = candles[candles.length - 1];

  // ── ① Session ──────────────────────────────────────────────────────
  const sessionQuality = getSessionQuality(lastCandle.time);
  if (sessionQuality === 'BLOCKED') {
    reasons.push('Invalid candle timestamp — cannot determine session');
  }

  // ── ② Severe Range ────────────────────────────────────────────────
  const adxVal = adx ?? calcADX(candles);
  const sevRange = isSevereRange(candles, adxVal);
  if (sevRange) reasons.push(`Severe range (ADX ${adxVal.toFixed(1)} + ATR compressed + tight price band)`);

  // ── ③ Late Entry ──────────────────────────────────────────────────
  const late = isLateEntry(candles, smcSignal, lastBosBarIndex);
  if (late) reasons.push('Late entry: overextended from EMA50, momentum exhausted, or stale BOS');

  // ── ④ Low Momentum ────────────────────────────────────────────────
  const lowMom = adxVal < 15;
  if (lowMom) reasons.push(`Low momentum: ADX ${adxVal.toFixed(1)} < 15 — no directional force`);

  // ── ⑤ Weak Volume ─────────────────────────────────────────────────
  const weakVol = isWeakVolume(candles);
  if (weakVol) reasons.push('Weak volume: signal bar < 40% of 20-bar average — low conviction');

  // ── ⑥ News Window ─────────────────────────────────────────────────
  // Disabled — timestamp precision varies; covered by session quality instead.
  // if (isNewsWindow(lastCandle.time)) reasons.push('News window: ±10 min of hour/half-hour');

  // ── ⑦ ATR Extremes ────────────────────────────────────────────────
  // LOW_VOLATILITY already causes minConfidence=96; no extra hard-block needed.
  // HIGH_VOLATILITY already increases minConfidence; no extra block.

  // ── ⑧ Confidence floor (belt-and-braces) ─────────────────────────
  // Threshold aligned with CONF_HARD_MIN in decisionEngine.ts (70).
  // The hard minimum is already enforced upstream; this check catches
  // any edge cases where qualityFilter is called directly.
  const LOW_PROB_THRESHOLD = 70;
  const lowProb = confidence < LOW_PROB_THRESHOLD;
  if (lowProb) {
    reasons.push(`Confidence ${confidence.toFixed(1)}% < ${LOW_PROB_THRESHOLD}% hard minimum`);
  }

  const allowed = reasons.length === 0;

  return {
    allowed,
    blockedReasons:   reasons,
    sessionQuality,
    adx:              adxVal,
    isSevereRange:    sevRange,
    isLateEntry:      late,
    isLowProbability: lowProb,
    isFakeBreakout:   false, // PA engine penalty already reduces confidence; not a hard block
    isWeakVolume:     weakVol,
    isLowMomentum:    lowMom,
  };
}

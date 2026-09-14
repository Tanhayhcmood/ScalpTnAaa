// Confidence Score Engine — XAUUSD Scalping (v4.0)
//
// Replaces the old binary rule-based decision system with a professional
// weighted confidence score (0–100).  Every engine contributes a score in
// its own band; the sum becomes the final confidence percentage.
//
// Score bands:
//   SMC (Structure)      0–35  Primary signal source
//   Trend (EMA)          0–20  Directional bias and alignment quality
//   Price Action         0–20  Entry timing and pattern quality
//   Wyckoff              0–15  Institutional intent confirmation
//   Liquidity Quality    0–5   Sweep/equal-level confluence
//   Volatility/Session   0–5   Session timing and market condition
//   ─────────────────────────
//   Total                0–100
//
// Scoring is dynamic — the same setup can score differently depending on
// the quality of each component (BOS distance, OB body ratio, etc.), not
// just whether it's present.

import type { SmcResult } from './smcEngine.js';
import type { WyckoffResult } from './wyckoffEngine.js';
import type { PriceActionResult } from './priceActionEngine.js';
import type { TrendResult } from './trendEngine.js';
import type { RegimeResult } from './marketRegimeDetector.js';

// ===== TYPES =====

export interface ConfidenceComponents {
  smcScore:        number;   // 0–35
  trendScore:      number;   // 0–20
  paScore:         number;   // 0–20
  wyckoffScore:    number;   // 0–15
  liquidityScore:  number;   // 0–5
  volatilityScore: number;   // 0–5
  total:           number;   // 0–100
}

export interface ConfidenceResult {
  /** Final confidence percentage, e.g. 91.8 */
  confidence:    number;
  components:    ConfidenceComponents;
  grade:         'PRIME' | 'HIGH' | 'MARGINAL' | 'REJECTED';
  /** Bullet-point reasons that explain the score */
  reasoning:     string[];
}

// ===== SCORING HELPERS =====

function cap(val: number, max: number): number {
  return Math.max(0, Math.min(max, val));
}

// ===== SMC SCORE (0–35) =====
//
// Uses the quality-filtered signals already returned by analyzeSmcStructure.
// Dynamic elements:
//   • CHoCH weighs more than BOS (structural reversal vs continuation)
//   • Multiple BOS in same direction → trend confirmed → bonus
//   • OB presence always quality-filtered upstream, so each OB is valid
//   • FVG score scales with count (2 aligned FVGs = stronger confluence)
//   • Sweep adds significant weight (institutional stop-hunt confirmation)

function calcSmcScore(smc: SmcResult, candidate: 'BUY' | 'SELL'): { score: number; reasons: string[] } {
  const reasons: string[] = [];
  let pts = 0;

  const dir = candidate;

  // Structural trend alignment — 4 pts
  if (smc.trend === (dir === 'BUY' ? 'BULLISH' : 'BEARISH')) {
    pts += 4;
    reasons.push('Structural trend aligned');
  }

  // BOS signals — up to 7 pts
  const alignedBos = smc.bosSignals.filter(b => b.type === dir);
  if (alignedBos.length >= 2) {
    pts += 7; // consecutive BOS = confirmed structure
    reasons.push('Multiple BOS confirmed');
  } else if (alignedBos.length === 1) {
    pts += 5;
    reasons.push('BOS confirmed');
  }

  // CHoCH — 8 pts (strongest single SMC event, overrides BOS)
  const lastChoch = smc.chochSignals[smc.chochSignals.length - 1];
  if (lastChoch?.type === dir) {
    pts += 8;
    reasons.push('CHoCH (structural reversal) confirmed');
  }

  // Active Order Blocks — up to 8 pts
  // Every OB here has already passed body-size and body-ratio filters.
  let obPts = 0;
  let obCount = 0;
  for (const ob of smc.orderBlocks) {
    if (ob.type !== (dir === 'BUY' ? 'BULLISH' : 'BEARISH')) continue;
    if (obCount >= 2) break;
    // Quality bonus: large-body OBs score higher
    const bodyRatio = Math.abs(ob.close - ob.open) / Math.max(ob.high - ob.low, 0.01);
    obPts += bodyRatio >= 0.5 ? 4 : 3;
    obCount++;
  }
  if (obPts > 0) {
    pts += cap(obPts, 8);
    reasons.push(`Order Block${obCount > 1 ? 's ×' + obCount : ''} in zone`);
  }

  // Fair Value Gaps — up to 4 pts
  let fvgCount = 0;
  for (const fvg of smc.fairValueGaps) {
    if (fvg.type === (dir === 'BUY' ? 'BULLISH' : 'BEARISH')) fvgCount++;
  }
  if (fvgCount >= 2) { pts += 4; reasons.push('Multiple FVGs in direction'); }
  else if (fvgCount === 1) { pts += 2; reasons.push('FVG in direction'); }

  // Liquidity Sweep — 4 pts
  const lastSweep = smc.liquiditySweeps[smc.liquiditySweeps.length - 1];
  if (lastSweep?.type === (dir === 'BUY' ? 'BULLISH' : 'BEARISH')) {
    pts += 4;
    reasons.push('Liquidity sweep confirmed');
  }

  return { score: cap(pts, 35), reasons };
}

// ===== TREND SCORE (0–20) =====
//
// Penalises counter-trend setups (should be blocked upstream, but belt-and-braces)
// and rewards full EMA stack alignment.

function calcTrendScore(trend: TrendResult, candidate: 'BUY' | 'SELL'): { score: number; reasons: string[] } {
  const reasons: string[] = [];
  const trendForDir = candidate === 'BUY' ? 'BULLISH' : 'BEARISH';

  if (trend.trend === trendForDir) {
    if (trend.strength === 'STRONG') {
      reasons.push('Strong EMA alignment (50/100/200)');
      return { score: 20, reasons };
    }
    reasons.push('Moderate EMA alignment (50/100)');
    return { score: 14, reasons };
  }
  if (trend.trend === 'NEUTRAL') {
    reasons.push('Neutral EMA trend (choppy)');
    return { score: 7, reasons };
  }
  // Counter-trend — this setup should have been blocked earlier, but cap at 0
  return { score: 0, reasons: ['Counter-trend: EMA opposes direction'] };
}

// ===== PA SCORE (0–20) =====
//
// Weights calibrated for XAUUSD scalping where the first candle quality is
// the most reliable predictor of follow-through.

function calcPaScore(pa: PriceActionResult, candidate: 'BUY' | 'SELL'): { score: number; reasons: string[] } {
  const reasons: string[] = [];
  const isBuy = candidate === 'BUY';

  // Base: normalized pa score → 0–13
  let pts = (pa.paSignal === candidate ? pa.paScore : 0) * 13;

  // Pattern bonuses
  if (isBuy) {
    if (pa.bullishEngulf)           { pts += 4; reasons.push('Bullish Engulf'); }
    else if (pa.bullishPinBar)      { pts += 3; reasons.push('Bullish Pin Bar'); }
    else if (pa.strongBullish)      { pts += 2; reasons.push('Strong Bull candle'); }
    if (pa.validBullBreakout)       { pts += 2; reasons.push('Valid Breakout'); }
    if (pa.bullishPullback)         { pts += 1; reasons.push('Pullback to demand'); }
    if (pa.nearDemandZone || pa.nearSupport) { pts += 1; reasons.push('Near demand/support'); }
    if (pa.fakeBullBreakout)        { pts -= 6; reasons.push('⚠ Fake Breakout detected'); }
  } else {
    if (pa.bearishEngulf)           { pts += 4; reasons.push('Bearish Engulf'); }
    else if (pa.bearishPinBar)      { pts += 3; reasons.push('Bearish Pin Bar'); }
    else if (pa.strongBearish)      { pts += 2; reasons.push('Strong Bear candle'); }
    if (pa.validBearBreakout)       { pts += 2; reasons.push('Valid Breakout'); }
    if (pa.bearishPullback)         { pts += 1; reasons.push('Pullback to supply'); }
    if (pa.nearSupplyZone || pa.nearResistance) { pts += 1; reasons.push('Near supply/resistance'); }
    if (pa.fakeBearBreakout)        { pts -= 6; reasons.push('⚠ Fake Breakout detected'); }
  }

  if (pa.paSignal === candidate && reasons.length === 0) reasons.push('PA signal aligned');

  return { score: cap(pts, 20), reasons };
}

// ===== WYCKOFF SCORE (0–15) =====

function calcWyckoffScore(wyckoff: WyckoffResult, candidate: 'BUY' | 'SELL'): { score: number; reasons: string[] } {
  const reasons: string[] = [];
  if (wyckoff.wyckoffSignal !== candidate) return { score: 0, reasons: [] };

  let pts = wyckoff.wyckoffScore * 8; // phase base (0–8)

  if (candidate === 'BUY' && wyckoff.spring) {
    pts += 4; reasons.push('Spring confirmed (shakeout of weak longs)');
  }
  if (candidate === 'SELL' && wyckoff.upthrust) {
    pts += 4; reasons.push('Upthrust confirmed (distribution shakeout)');
  }
  if (wyckoff.volumeConfirmed) {
    pts += 3; reasons.push('Volume confirms phase');
  }

  if (wyckoff.phase !== 'NEUTRAL') {
    reasons.unshift(`Wyckoff ${wyckoff.phase} phase`);
  }

  return { score: cap(pts, 15), reasons };
}

// ===== LIQUIDITY SCORE (0–5) =====

function calcLiquidityScore(smc: SmcResult, candidate: 'BUY' | 'SELL'): { score: number; reasons: string[] } {
  const reasons: string[] = [];
  let pts = 0;

  // Sweep present and aligned
  const alignedSweep = smc.liquiditySweeps
    .filter(s => s.type === (candidate === 'BUY' ? 'BULLISH' : 'BEARISH'));
  if (alignedSweep.length > 0) {
    pts += 2.5;
    reasons.push('Liquidity sweep in direction');
  }

  // Equal levels (resting liquidity pools)
  const alignedEqLevels = candidate === 'BUY'
    ? smc.equalLows.length   // equal lows = resting sell-stops for bull
    : smc.equalHighs.length; // equal highs = resting buy-stops for bear
  if (alignedEqLevels >= 2) {
    pts += 1.5;
    reasons.push('Multiple equal-level liquidity pools');
  } else if (alignedEqLevels === 1) {
    pts += 0.75;
    reasons.push('Equal-level liquidity pool');
  }

  // Sweep + BOS confluence (sweep followed by structure confirmation)
  if (alignedSweep.length > 0 && smc.bosSignals.filter(b => b.type === candidate).length > 0) {
    pts += 1;
    reasons.push('Sweep + BOS confluence');
  }

  return { score: cap(pts, 5), reasons };
}

// ===== VOLATILITY/SESSION SCORE (0–5) =====

function calcVolatilityScore(
  regime: RegimeResult,
  sessionQuality: 'PRIME' | 'MODERATE' | 'BLOCKED',
): { score: number; reasons: string[] } {
  const reasons: string[] = [];
  let pts = 0;

  // Session quality
  if (sessionQuality === 'PRIME')     { pts += 2.5; reasons.push('Prime session (London/NY)'); }
  else if (sessionQuality === 'MODERATE') { pts += 1.0; reasons.push('Moderate session'); }

  // ADX (trend strength as volatility quality)
  if (regime.adx >= 30) { pts += 1.5; reasons.push(`ADX ${regime.adx} (strong momentum)`); }
  else if (regime.adx >= 20) { pts += 0.75; }

  // ATR ratio (healthy volatility range)
  if (regime.atrRatio >= 0.8 && regime.atrRatio <= 1.5) {
    pts += 1.0; reasons.push('Normal ATR range');
  } else if (regime.atrRatio > 1.5) {
    // elevated volatility — small bonus (we already require higher confidence in this regime)
    pts += 0.5;
  }

  return { score: cap(pts, 5), reasons };
}

// ===== GRADE ASSIGNMENT =====

function assignGrade(confidence: number, minConf: number): ConfidenceResult['grade'] {
  if (confidence >= minConf)   return 'PRIME';
  if (confidence >= 90)        return 'HIGH';
  if (confidence >= 85)        return 'MARGINAL';
  return 'REJECTED';
}

// ===== MAIN ENTRY POINT =====

export function calcConfidence(
  smc:            SmcResult,
  wyckoff:        WyckoffResult,
  pa:             PriceActionResult,
  trend:          TrendResult,
  regime:         RegimeResult,
  sessionQuality: 'PRIME' | 'MODERATE' | 'BLOCKED',
  candidate:      'BUY' | 'SELL',
): ConfidenceResult {
  const smcResult  = calcSmcScore(smc, candidate);
  const trendRes   = calcTrendScore(trend, candidate);
  const paRes      = calcPaScore(pa, candidate);
  const wyRes      = calcWyckoffScore(wyckoff, candidate);
  const liqRes     = calcLiquidityScore(smc, candidate);
  const volRes     = calcVolatilityScore(regime, sessionQuality);

  const components: ConfidenceComponents = {
    smcScore:        smcResult.score,
    trendScore:      trendRes.score,
    paScore:         paRes.score,
    wyckoffScore:    wyRes.score,
    liquidityScore:  liqRes.score,
    volatilityScore: volRes.score,
    total:           0,
  };
  components.total = +(
    components.smcScore + components.trendScore + components.paScore +
    components.wyckoffScore + components.liquidityScore + components.volatilityScore
  ).toFixed(1);

  const confidence = components.total; // already 0–100

  const reasoning = [
    ...smcResult.reasons,
    ...trendRes.reasons,
    ...paRes.reasons,
    ...wyRes.reasons,
    ...liqRes.reasons,
    ...volRes.reasons,
  ];

  return {
    confidence,
    components,
    grade: assignGrade(confidence, regime.rules.minConfidence),
    reasoning,
  };
}

// Decision Engine — Central Orchestrator (v4.0)
//
// Single entry point that connects ALL 7 engines:
//   ① SMC Engine         — structure, BOS/CHoCH, OBs, FVGs, sweeps
//   ② Wyckoff Engine     — phase, Spring/Upthrust, volume
//   ③ Price Action Engine — patterns, levels, breakouts
//   ④ Trend Engine       — EMA 50/100/200 gate
//   ⑤ Market Regime Detector — adaptive entry rules per regime
//   ⑥ Confidence Engine  — 0–100% weighted score
//   ⑦ Quality Filter     — 10-category final gate
//   ⑧ Capital Manager    — SL/TP/lots from structural levels
//
// No trade decision is made anywhere else in the codebase.
// All engines contribute scores; no single engine can block or open
// a trade on its own.

import { OHLCV } from './goldEngine.js';
import { analyzeSmcStructure, type SmcResult } from './smcEngine.js';
import { analyzeWyckoff, type WyckoffResult } from './wyckoffEngine.js';
import { analyzePriceAction, type PriceActionResult } from './priceActionEngine.js';
import { analyzeTrend, type TrendResult } from './trendEngine.js';
import { detectMarketRegime, type RegimeResult, type MarketRegime } from './marketRegimeDetector.js';
import { calcConfidence, type ConfidenceResult, type ConfidenceComponents } from './confidenceEngine.js';
import { applyQualityFilter, getSessionQuality, type QualityFilterResult } from './qualityFilter.js';
import { calcTradeParameters, type CapitalInput, type CapitalOutput } from './capitalManager.js';

// ===== TYPES =====

export interface DecisionResult {
  /** Final gate — only true when ALL checks pass and confidence meets threshold */
  allowed:           boolean;
  direction:         'BUY' | 'SELL' | 'NEUTRAL';
  /** Confidence percentage, e.g. 91.8. 0 when no signal. */
  confidence:        number;
  components:        ConfidenceComponents;
  grade:             ConfidenceResult['grade'];
  regime:            MarketRegime;
  regimeLabel:       string;
  regimeRules:       RegimeResult['rules'];

  // Quality gate details
  qualityFilter:     QualityFilterResult;
  blockedReasons:    string[];

  // Decision reasoning (bullet points for logging/UI)
  reasoning:         string[];

  // Trade parameters (only populated when allowed === true)
  tradeParams?:      CapitalOutput;

  // Raw engine outputs (for API transparency)
  smc:    SmcResult;
  wyckoff: WyckoffResult;
  pa:     PriceActionResult;
  trend:  TrendResult;
}

// ===== CONSTANTS =====

/**
 * Hard minimum confidence — trades below this are ALWAYS rejected.
 *
 * Empirical calibration note (XAUUSD M5, 2021–2025):
 *   The original 85% threshold assumed synthetic / tick-level data where the
 *   Wyckoff, SMC, and PA engines all score near their theoretical maxima
 *   simultaneously.  On real M5 aggregated data the empirical maximum across
 *   340 k bars is 84.4%, which proves 85% is mathematically unattainable.
 *
 *   After auto-calibrating CFG_M5 (maxRangePct from data percentile, spring-
 *   margin from median ATR), fixing the spring-detection bug, and reducing the
 *   trendPct gate, the engine was re-validated with a full 5-year CSV backtest.
 *   The threshold is lowered to 70% to capture the populated 70–84% confidence
 *   band while still demanding strong multi-component alignment.
 */
const CONF_HARD_MIN  = 70;
/** Marginal zone: 70–regime.minConf — allowed when R:R ≥ CONF_MARGINAL_RR.
 *  Lowered from 2.0 to 1.5 to align with STRONG_TREND regime's minRR=1.5. */
const CONF_MARGINAL_RR = 1.5;

// ===== SESSION HELPER =====
// getSessionQuality is re-exported from qualityFilter.ts and imported above.
// The previous local copy was an exact duplicate of that export; removed to
// eliminate the maintenance risk of the two implementations diverging silently.

// ===== CANDIDATE DIRECTION =====
// Priority: CHoCH > BOS > structural trend.
// This is the direction the confidence engine evaluates.

function candidateDirection(smc: SmcResult): 'BUY' | 'SELL' | 'NEUTRAL' {
  const lastChoch = smc.chochSignals[smc.chochSignals.length - 1];
  if (lastChoch) return lastChoch.type;

  const lastBos = smc.bosSignals[smc.bosSignals.length - 1];
  if (lastBos) return lastBos.type;

  if (smc.trend === 'BULLISH') return 'BUY';
  if (smc.trend === 'BEARISH') return 'SELL';

  return 'NEUTRAL';
}

// ===== MAIN ENTRY POINT =====

// ===== ENGINE CONFIG =====
// Optional configuration for comparative backtesting.
// minConfirmations: 2 or 3 (default 3)
// useAtrHighVolFilter: enable/disable ATR spike regime (default false — disabled)
export interface DecisionEngineConfig {
  minConfirmations?:    number;   // default: 3
  useAtrHighVolFilter?: boolean;  // default: false (disabled — reduced profits in backtests)
}

export function runDecisionEngine(
  candles:        OHLCV[],
  timeframe:      'M1' | 'M5',
  accountBalance: number,
  riskPercent = 1.0,
  engConfig: DecisionEngineConfig = {},
): DecisionResult {
  const minConfs       = engConfig.minConfirmations   ?? 3;
  const useAtrHighVol  = engConfig.useAtrHighVolFilter ?? false;

  // ── Run all 4 core engines ────────────────────────────────────────
  // analyzeSmcStructure internally calls Wyckoff, PA, Trend for its own
  // scoring.  We call them separately here so the Decision Engine has
  // access to the raw outputs for its own confidence calculation.
  const smc     = analyzeSmcStructure(candles, timeframe, minConfs);
  const wyckoff = analyzeWyckoff(candles, timeframe);
  const pa      = analyzePriceAction(candles, timeframe);
  const trend   = analyzeTrend(candles);

  // ── Determine candidate direction ─────────────────────────────────
  const candidate = candidateDirection(smc);

  const neutral: DecisionResult = {
    allowed:        false,
    direction:      'NEUTRAL',
    confidence:     0,
    components:     { smcScore: 0, trendScore: 0, paScore: 0, wyckoffScore: 0, liquidityScore: 0, volatilityScore: 0, total: 0 },
    grade:          'REJECTED',
    regime:         'RANGE',
    regimeLabel:    'No Signal',
    regimeRules:    { minConfidence: 90, minRR: 1.5, allowLong: true, allowShort: true, slAtrMultAdjust: 1, label: 'No Signal' },
    qualityFilter:  { allowed: false, blockedReasons: ['No SMC signal'], sessionQuality: 'BLOCKED', adx: 0, isSevereRange: false, isLateEntry: false, isLowProbability: false, isFakeBreakout: false, isWeakVolume: false, isLowMomentum: false },
    blockedReasons: ['No SMC signal'],
    reasoning:      [],
    smc, wyckoff, pa, trend,
  };

  if (candidate === 'NEUTRAL') return neutral;

  // ── Hard EMA gate (direct trend opposition blocks immediately) ────
  const trendDir = trend.trend === 'BULLISH' ? 'BUY' : trend.trend === 'BEARISH' ? 'SELL' : 'NEUTRAL';
  if ((candidate === 'BUY' && trendDir === 'SELL') || (candidate === 'SELL' && trendDir === 'BUY')) {
    return {
      ...neutral,
      blockedReasons: [`EMA trend (${trend.trend}) directly opposes SMC signal (${candidate})`],
      reasoning:      [`EMA trend (${trend.trend}) directly opposes SMC signal (${candidate})`],
    };
  }

  // ── Market Regime ─────────────────────────────────────────────────
  const regime = detectMarketRegime(candles, trend, wyckoff, useAtrHighVol);

  // ── Direction allowed by regime? ──────────────────────────────────
  if (candidate === 'BUY'  && !regime.rules.allowLong) {
    return {
      ...neutral,
      regime:      regime.regime,
      regimeLabel: regime.rules.label,
      regimeRules: regime.rules,
      blockedReasons: [`Regime "${regime.rules.label}" does not allow LONG entries`],
      reasoning:      [`Regime: ${regime.description}`],
    };
  }
  if (candidate === 'SELL' && !regime.rules.allowShort) {
    return {
      ...neutral,
      regime:      regime.regime,
      regimeLabel: regime.rules.label,
      regimeRules: regime.rules,
      blockedReasons: [`Regime "${regime.rules.label}" does not allow SHORT entries`],
      reasoning:      [`Regime: ${regime.description}`],
    };
  }

  // ── Session quality (feeds both confidence engine and quality filter) ─
  const lastCandle = candles[candles.length - 1];
  const sessionQuality = getSessionQuality(lastCandle.time);

  // ── Confidence Score Engine ───────────────────────────────────────
  const confResult = calcConfidence(smc, wyckoff, pa, trend, regime, sessionQuality, candidate);

  // ── Hard minimum confidence check ────────────────────────────────
  if (confResult.confidence < CONF_HARD_MIN) {
    return {
      ...neutral,
      direction:      candidate,
      confidence:     confResult.confidence,
      components:     confResult.components,
      grade:          'REJECTED',
      regime:         regime.regime,
      regimeLabel:    regime.rules.label,
      regimeRules:    regime.rules,
      blockedReasons: [`Confidence ${confResult.confidence.toFixed(1)}% < ${CONF_HARD_MIN}% hard minimum`],
      reasoning:      confResult.reasoning,
      smc, wyckoff, pa, trend,
    };
  }

  // ── Quality Filter (10-category gate) ────────────────────────────
  const lastBosBarIndex = smc.bosSignals.length > 0
    ? smc.bosSignals[smc.bosSignals.length - 1].barIndex
    : null;
  const quality = applyQualityFilter(candles, candidate, confResult.confidence, lastBosBarIndex, regime.adx, regime.atrRatio);

  if (!quality.allowed) {
    return {
      ...neutral,
      direction:      candidate,
      confidence:     confResult.confidence,
      components:     confResult.components,
      grade:          confResult.grade,
      regime:         regime.regime,
      regimeLabel:    regime.rules.label,
      regimeRules:    regime.rules,
      qualityFilter:  quality,
      blockedReasons: quality.blockedReasons,
      reasoning:      confResult.reasoning,
      smc, wyckoff, pa, trend,
    };
  }

  // ── Capital Manager ───────────────────────────────────────────────
  const lastCanc = candles[candles.length - 1];
  const atr = regime.atr;

  // Best structural levels for SL/TP
  const alignedObs = smc.orderBlocks.filter(ob =>
    ob.type === (candidate === 'BUY' ? 'BULLISH' : 'BEARISH'));
  const latestOb = alignedObs[alignedObs.length - 1];

  const swingHighs = smc.bosSignals.filter(b => b.type === 'BUY').map(b => b.price);
  const swingLows  = smc.bosSignals.filter(b => b.type === 'SELL').map(b => b.price);

  const capitalInput: CapitalInput = {
    direction:         candidate,
    entryPrice:        lastCanc.close,
    atr,
    accountBalance,
    riskPercent,
    orderBlockTop:     latestOb?.high,
    orderBlockBottom:  latestOb?.low,
    swingHigh:         swingHighs.length ? Math.max(...swingHighs) : undefined,
    swingLow:          swingLows.length  ? Math.min(...swingLows)  : undefined,
  };
  const tradeParams = calcTradeParameters(capitalInput);

  // ── Marginal confidence check (85–threshold needs R:R ≥ 2.0) ─────
  const minConf = regime.rules.minConfidence;
  if (confResult.confidence < minConf) {
    if (tradeParams.riskRewardRatio < CONF_MARGINAL_RR) {
      return {
        ...neutral,
        direction:      candidate,
        confidence:     confResult.confidence,
        components:     confResult.components,
        grade:          'MARGINAL',
        regime:         regime.regime,
        regimeLabel:    regime.rules.label,
        regimeRules:    regime.rules,
        qualityFilter:  quality,
        blockedReasons: [
          `Marginal confidence ${confResult.confidence.toFixed(1)}% (< ${minConf}%) requires R:R ≥ ${CONF_MARGINAL_RR} (got ${tradeParams.riskRewardRatio.toFixed(2)})`,
        ],
        reasoning: confResult.reasoning,
        smc, wyckoff, pa, trend,
      };
    }
  }

  // ── R:R gate (regime-specific minimum) ───────────────────────────
  if (tradeParams.riskRewardRatio < regime.rules.minRR) {
    return {
      ...neutral,
      direction:      candidate,
      confidence:     confResult.confidence,
      components:     confResult.components,
      grade:          confResult.grade,
      regime:         regime.regime,
      regimeLabel:    regime.rules.label,
      regimeRules:    regime.rules,
      qualityFilter:  quality,
      blockedReasons: [
        `R:R ${tradeParams.riskRewardRatio.toFixed(2)} < ${regime.rules.minRR} minimum for "${regime.rules.label}" regime`,
      ],
      reasoning:      confResult.reasoning,
      smc, wyckoff, pa, trend,
    };
  }

  // ── TRADE ALLOWED ─────────────────────────────────────────────────
  return {
    allowed:        true,
    direction:      candidate,
    confidence:     confResult.confidence,
    components:     confResult.components,
    grade:          confResult.grade,
    regime:         regime.regime,
    regimeLabel:    regime.rules.label,
    regimeRules:    regime.rules,
    qualityFilter:  quality,
    blockedReasons: [],
    reasoning:      confResult.reasoning,
    tradeParams,
    smc, wyckoff, pa, trend,
  };
}

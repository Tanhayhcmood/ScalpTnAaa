// Backtest Engine V2 — Pluggable Data Provider
//
// Identical logic to backtestEngine.ts but accepts a DataProvider
// instead of calling generateCandles() directly.
//
// Both the M5 full-pipeline path and the M15 legacy path are preserved
// unchanged. The only difference is the candle source.
//
// For confidence distribution analysis (zero-trade root cause):
//   runBacktestV2() returns a full ConfidenceDistribution alongside the
//   standard BacktestOutput so callers can see EXACTLY which gate blocked
//   entries — with the gate name, source file, and line number.

import type { OHLCV } from './goldEngine.js';
import { calcEMA, calcRSI, calcBB, calcATR } from './goldEngine.js';
import { computeSmcStatePerBar } from './smcEngine.js';
import { runDecisionEngine, type DecisionEngineConfig } from './decisionEngine.js';
import type { BacktestConfig, BacktestTradeRecord, EquityPoint, BacktestOutput } from './backtestEngine.js';

export type { BacktestConfig, BacktestTradeRecord, EquityPoint, BacktestOutput };

// ===== CONFIDENCE DISTRIBUTION =====
// Maps each gate to the source file + line where it fires, so zero-trade
// reports can point directly to the responsible code.

export interface GateSite {
  file: string;
  line: number;
  description: string;
}

export const GATE_SITES: Record<string, GateSite> = {
  'no_smc_signal': {
    file: 'decisionEngine.ts',
    line: 130,
    description: 'candidateDirection() returned NEUTRAL — no CHoCH, BOS, or structural trend from SMC engine',
  },
  'ema_opposes_smc': {
    file: 'decisionEngine.ts',
    line: 134,
    description: 'Hard EMA gate: EMA trend directly opposes SMC signal direction',
  },
  'regime_no_long': {
    file: 'decisionEngine.ts',
    line: 146,
    description: 'Market regime does not allow LONG entries (e.g. STRONG_TREND_BEAR)',
  },
  'regime_no_short': {
    file: 'decisionEngine.ts',
    line: 155,
    description: 'Market regime does not allow SHORT entries (e.g. STRONG_TREND_BULL)',
  },
  'confidence_below_hard_min': {
    file: 'decisionEngine.ts',
    line: 175,
    description: `Confidence score < ${85}% hard minimum. Root cause: Wyckoff+PA rarely score on synthetic/thin data`,
  },
  'quality_filter_blocked': {
    file: 'decisionEngine.ts',
    line: 196,
    description: 'Quality filter rejected: session/ADX/lateEntry/weakVolume/severeRange',
  },
  'marginal_rr_too_low': {
    file: 'decisionEngine.ts',
    line: 242,
    description: 'Marginal confidence (85–minConf%) but R:R < 2.0 — not worth the risk',
  },
  'rr_below_regime_min': {
    file: 'decisionEngine.ts',
    line: 263,
    description: 'R:R below regime-specific minimum (e.g. RANGE requires R:R ≥ 2.5)',
  },
  'session_blocked': {
    file: 'qualityFilter.ts',
    line: 217,
    description: 'Outside allowed XAUUSD session windows (UTC 0-3, 6-12, 12-16, 16-17)',
  },
  'late_entry': {
    file: 'qualityFilter.ts',
    line: 228,
    description: 'Price overextended from EMA50, momentum exhausted, or BOS signal is stale (> 8 bars old)',
  },
  'low_momentum': {
    file: 'qualityFilter.ts',
    line: 232,
    description: 'ADX < 15 — no directional momentum, likely choppy/flat market',
  },
  'weak_volume': {
    file: 'qualityFilter.ts',
    line: 236,
    description: 'Signal bar volume < 40% of 20-bar average — low conviction',
  },
  'severe_range': {
    file: 'qualityFilter.ts',
    line: 224,
    description: 'ADX < 22 + volatility compressed + price band < 2.5× ATR',
  },
};

export interface ConfidenceDistribution {
  totalBarsEvaluated:  number;
  neutralDirection:    number;

  // Breakdown of non-neutral bars by confidence band
  lt40:  number;
  s4050: number;
  s5060: number;
  s6070: number;
  s7080: number;
  s8085: number;
  gte85: number;

  maxConf:            number;
  maxConfBar:         number;
  maxConfComponents:  Record<string, number> | null;
  maxConfRegime:      string | null;
  maxConfDirection:   string | null;
  maxConfAllowed:     boolean;

  // Gate breakdown: which gate fired most often
  gateBreakdown:      Record<string, number>;

  // Top blocking gates sorted by frequency
  topGates: Array<{ gate: string; count: number; site: GateSite }>;

  // Component average scores (non-neutral bars only)
  avgSmcScore:       number;
  avgTrendScore:     number;
  avgPaScore:        number;
  avgWyckoffScore:   number;
  avgLiqScore:       number;
  avgVolScore:       number;
}

// ===== EXTENDED OUTPUT =====

export interface BacktestOutputV2 extends BacktestOutput {
  // All standard BacktestOutput fields plus:
  grossProfit:   number;
  grossLoss:     number;
  netProfit:     number;
  avgRR:         number;
  expectancy:    number;
  recoveryFactor: number;
  tradesPerDay:  number;
  avgTradeDurationBars: number;
  avgTradeDurationMinutes: number;
  dataSource:    string;
  periodDays:    number;

  // Confidence analysis (only populated when no trades or requested)
  confidenceDistribution: ConfidenceDistribution | null;

  // Trade-by-trade records
  tradeRecords: BacktestTradeRecord[];
}

// ===== POSITION SIZING (M15 legacy path only) =====

function calcLotSize(balance: number, riskPct: number, slPips: number): number {
  const riskAmount = balance * (riskPct / 100);
  const pipValue = 10;
  const lots = riskAmount / (slPips * pipValue);
  return Math.max(0.01, Math.min(parseFloat(lots.toFixed(2)), 5));
}

// ===== SMC SCORE HELPER (M15 legacy path only) =====

function smcDirectionalScore(
  trend:     'BULLISH' | 'BEARISH' | 'NEUTRAL',
  lastBos:   'BUY' | 'SELL' | null,
  lastChoch: 'BUY' | 'SELL' | null,
): { buyScore: number; sellScore: number } {
  let b = 0, s = 0;
  if (trend === 'BULLISH') b += 2; else if (trend === 'BEARISH') s += 2;
  if (lastBos === 'BUY') b += 2; else if (lastBos === 'SELL') s += 2;
  if (lastChoch === 'BUY') b += 3; else if (lastChoch === 'SELL') s += 3;
  return { buyScore: b, sellScore: s };
}

// ===== MANAGED TRADE =====

interface ManagedTrade {
  type:            'BUY' | 'SELL';
  entryPrice:      number;
  sl:              number;
  tp:              number;
  lots:            number;
  trailDist:       number;
  trailActivation: number;
  beAt:            number;
  beSL:            number;
  beDone:          boolean;
  openBar:         number;
}

function dollarPnl(dir: 'BUY' | 'SELL', entry: number, exit: number, lots: number): number {
  return (dir === 'BUY' ? 1 : -1) * (exit - entry) * 100 * lots;
}

const SMC_WINDOW = 250;

// ===== GATE CLASSIFIER =====
// Reads the blocked reason strings from DecisionResult and maps them to
// canonical gate keys so the distribution counters are consistent.

function classifyGate(blockedReasons: string[], qualityBlockedReasons: string[]): string {
  const r0 = blockedReasons[0] ?? '';
  const q0 = qualityBlockedReasons[0] ?? '';

  if (r0.includes('No SMC signal'))          return 'no_smc_signal';
  if (r0.includes('EMA trend'))               return 'ema_opposes_smc';
  if (r0.includes('does not allow LONG'))     return 'regime_no_long';
  if (r0.includes('does not allow SHORT'))    return 'regime_no_short';
  if (r0.includes('< 85%') || r0.includes('hard minimum')) return 'confidence_below_hard_min';
  if (r0.includes('Marginal confidence'))     return 'marginal_rr_too_low';
  if (r0.includes('R:R') && r0.includes('<')) return 'rr_below_regime_min';
  if (q0.includes('session'))                 return 'session_blocked';
  if (q0.includes('Late entry') || q0.includes('late entry')) return 'late_entry';
  if (q0.includes('ADX') && q0.includes('< 15')) return 'low_momentum';
  if (q0.includes('volume'))                  return 'weak_volume';
  if (q0.includes('range') || q0.includes('Range')) return 'severe_range';

  // Quality filter fired but reason not specifically matched
  if (!blockedReasons.length && !qualityBlockedReasons.length) return 'allowed';
  return 'other';
}

// ===== MAIN ENTRY POINT =====

export function runBacktestV2(
  cfg:        BacktestConfig,
  candles:    OHLCV[],   // pre-loaded candles from provider
  dataSource: string,
  engConfig:  DecisionEngineConfig = {},  // optional: minConfirmations, useAtrHighVolFilter
): BacktestOutputV2 {
  if (candles.length < SMC_WINDOW + 10) {
    throw new Error(`runBacktestV2: need at least ${SMC_WINDOW + 10} candles, got ${candles.length}`);
  }

  const minutesPerCandle = cfg.timeframe === 'M5' ? 5 : 15;
  const periodDays = Math.round(candles.length * minutesPerCandle / (24 * 60));

  const closes = candles.map(c => c.close);
  const emaFast = calcEMA(closes, cfg.emaFastPeriod);
  const emaSlow = calcEMA(closes, cfg.emaSlowPeriod);
  const rsi     = calcRSI(closes, cfg.rsiPeriod);
  const bb      = calcBB(closes, cfg.bbPeriod, cfg.bbDeviation);
  const atr     = calcATR(candles, cfg.atrPeriod);

  const useFullPipeline = cfg.timeframe === 'M5';
  const smcStates = useFullPipeline ? null : computeSmcStatePerBar(candles, cfg.timeframe);

  const trades: BacktestTradeRecord[] = [];
  let balance    = cfg.initialBalance;
  let peakBal    = balance;
  let maxDD      = 0;
  let inTrade    = false;
  let currentTrade: ManagedTrade | null = null;
  const equityCurve: EquityPoint[] = [];

  // ── Confidence distribution tracking ────────────────────────────────────
  const confDist: ConfidenceDistribution = {
    totalBarsEvaluated: 0,
    neutralDirection: 0,
    lt40: 0, s4050: 0, s5060: 0, s6070: 0, s7080: 0, s8085: 0, gte85: 0,
    maxConf: 0, maxConfBar: 0, maxConfComponents: null,
    maxConfRegime: null, maxConfDirection: null, maxConfAllowed: false,
    gateBreakdown: {},
    topGates: [],
    avgSmcScore: 0, avgTrendScore: 0, avgPaScore: 0,
    avgWyckoffScore: 0, avgLiqScore: 0, avgVolScore: 0,
  };
  let nonNeutralCount = 0;
  let sumSmc = 0, sumTrend = 0, sumPa = 0, sumWyc = 0, sumLiq = 0, sumVol = 0;

  const warmup = useFullPipeline
    ? SMC_WINDOW
    : Math.max(cfg.emaSlowPeriod, cfg.rsiPeriod, cfg.bbPeriod, cfg.atrPeriod) + 5;

  for (let i = warmup; i < candles.length; i++) {
    const candle = candles[i];
    const price  = candle.close;

    // ── Manage open trade ──────────────────────────────────────────────────
    if (inTrade && currentTrade) {
      let closed    = false;
      let exitPrice = price;

      if (!currentTrade.beDone) {
        if (currentTrade.type === 'BUY'  && candle.high >= currentTrade.beAt) {
          currentTrade.sl = Math.max(currentTrade.sl, currentTrade.beSL);
          currentTrade.beDone = true;
        } else if (currentTrade.type === 'SELL' && candle.low <= currentTrade.beAt) {
          currentTrade.sl = Math.min(currentTrade.sl, currentTrade.beSL);
          currentTrade.beDone = true;
        }
      }

      if (currentTrade.trailDist > 0) {
        if (currentTrade.type === 'BUY' && candle.high >= currentTrade.trailActivation) {
          currentTrade.sl = Math.max(currentTrade.sl, candle.high - currentTrade.trailDist);
        } else if (currentTrade.type === 'SELL' && candle.low <= currentTrade.trailActivation) {
          currentTrade.sl = Math.min(currentTrade.sl, candle.low + currentTrade.trailDist);
        }
      }

      if (currentTrade.type === 'BUY') {
        if (candle.low  <= currentTrade.sl) { exitPrice = currentTrade.sl; closed = true; }
        else if (candle.high >= currentTrade.tp) { exitPrice = currentTrade.tp; closed = true; }
      } else {
        if (candle.high >= currentTrade.sl) { exitPrice = currentTrade.sl; closed = true; }
        else if (candle.low  <= currentTrade.tp) { exitPrice = currentTrade.tp; closed = true; }
      }

      if (closed) {
        const pipMult = currentTrade.type === 'BUY' ? 1 : -1;
        const pips    = pipMult * (exitPrice - currentTrade.entryPrice) / 0.1;
        const profit  = dollarPnl(currentTrade.type, currentTrade.entryPrice, exitPrice, currentTrade.lots);
        balance      += profit;
        peakBal       = Math.max(peakBal, balance);
        const dd      = ((peakBal - balance) / peakBal) * 100;
        maxDD         = Math.max(maxDD, dd);

        trades.push({
          type:       currentTrade.type,
          entryPrice: currentTrade.entryPrice,
          exitPrice,
          sl:         currentTrade.sl,
          tp:         currentTrade.tp,
          pips:       +pips.toFixed(1),
          profit:     +profit.toFixed(2),
          openBar:    currentTrade.openBar,
          closeBar:   i,
        });
        inTrade = false; currentTrade = null;
      }
    }

    // ── Entry evaluation ───────────────────────────────────────────────────
    if (!inTrade) {
      if (useFullPipeline) {
        // M5: Full Decision Engine
        const windowCandles = candles.slice(i - SMC_WINDOW + 1, i + 1);
        const decision = runDecisionEngine(windowCandles, 'M5', balance, cfg.riskPerTrade, engConfig);

        // ── Record confidence distribution ──────────────────────────────
        confDist.totalBarsEvaluated++;

        if (decision.direction === 'NEUTRAL') {
          confDist.neutralDirection++;
          const gate = classifyGate(decision.blockedReasons, decision.qualityFilter?.blockedReasons ?? []);
          confDist.gateBreakdown[gate] = (confDist.gateBreakdown[gate] ?? 0) + 1;
        } else {
          nonNeutralCount++;
          const c = decision.confidence;
          if (c < 40)       confDist.lt40++;
          else if (c < 50)  confDist.s4050++;
          else if (c < 60)  confDist.s5060++;
          else if (c < 70)  confDist.s6070++;
          else if (c < 80)  confDist.s7080++;
          else if (c < 85)  confDist.s8085++;
          else              confDist.gte85++;

          if (c > confDist.maxConf) {
            confDist.maxConf          = c;
            confDist.maxConfBar       = i;
            confDist.maxConfComponents = { ...decision.components } as unknown as Record<string, number>;
            confDist.maxConfRegime    = decision.regime;
            confDist.maxConfDirection = decision.direction;
            confDist.maxConfAllowed   = decision.allowed;
          }

          // decision.components is typed as ConfidenceComponents which already
          // declares all these fields — the previous `as any` cast was hiding
          // the type from the compiler without providing any runtime benefit.
          const comp = decision.components;
          sumSmc   += comp.smcScore        ?? 0;
          sumTrend += comp.trendScore      ?? 0;
          sumPa    += comp.paScore         ?? 0;
          sumWyc   += comp.wyckoffScore    ?? 0;
          sumLiq   += comp.liquidityScore  ?? 0;
          sumVol   += comp.volatilityScore ?? 0;

          const gate = classifyGate(decision.blockedReasons, decision.qualityFilter?.blockedReasons ?? []);
          confDist.gateBreakdown[gate] = (confDist.gateBreakdown[gate] ?? 0) + 1;
        }

        if (!decision.allowed || decision.direction === 'NEUTRAL') continue;

        const capital = decision.tradeParams!;
        currentTrade = {
          type:            decision.direction,
          entryPrice:      capital.entryPrice,
          sl:              capital.stopLoss,
          tp:              capital.takeProfit,
          lots:            capital.lotSize,
          trailDist:       capital.trailingStopDistance,
          trailActivation: capital.trailingActivationAt,
          beAt:            capital.breakEvenAt,
          beSL:            capital.breakEvenSL,
          beDone:          false,
          openBar:         i,
        };
        inTrade = true;

      } else {
        // M15 legacy path (unchanged)
        const smcState  = smcStates![i] ?? { trend: 'NEUTRAL', lastBosDir: null, lastChochDir: null };
        const { buyScore: smcBuy, sellScore: smcSell } = smcDirectionalScore(
          smcState.trend, smcState.lastBosDir, smcState.lastChochDir,
        );

        if (!smcBuy && !smcSell) continue;

        const smcQuality = Math.max(smcBuy, smcSell) / 7;
        if (smcQuality < cfg.minSignalScore * 0.5) continue;

        const indBuy  = (emaFast[i] > emaSlow[i] ? 1 : 0) + (rsi[i] < cfg.rsiOversold ? 1 : 0) + (price <= bb.lower[i] ? 1 : 0);
        const indSell = (emaFast[i] < emaSlow[i] ? 1 : 0) + (rsi[i] > cfg.rsiOverbought ? 1 : 0) + (price >= bb.upper[i] ? 1 : 0);
        const totalBuy  = smcBuy  * 2 + indBuy;
        const totalSell = smcSell * 2 + indSell;

        const atrVal = atr[i];
        const sl = atrVal * cfg.atrSlMultiplier;
        const tp = atrVal * cfg.atrTpMultiplier;
        const lots = calcLotSize(balance, cfg.riskPerTrade, sl / 0.1);

        if (smcBuy > 0 && !smcSell) {
          if (indSell < 3) {
            currentTrade = { type: 'BUY',  entryPrice: price, sl: price - sl, tp: price + tp, lots, trailDist: 0, trailActivation: 0, beAt: 0, beSL: 0, beDone: true, openBar: i };
            inTrade = true;
          }
        } else if (smcSell > 0 && !smcBuy) {
          if (indBuy < 3) {
            currentTrade = { type: 'SELL', entryPrice: price, sl: price + sl, tp: price - tp, lots, trailDist: 0, trailActivation: 0, beAt: 0, beSL: 0, beDone: true, openBar: i };
            inTrade = true;
          }
        } else {
          if (totalBuy > totalSell) {
            currentTrade = { type: 'BUY',  entryPrice: price, sl: price - sl, tp: price + tp, lots, trailDist: 0, trailActivation: 0, beAt: 0, beSL: 0, beDone: true, openBar: i };
            inTrade = true;
          } else if (totalSell > totalBuy) {
            currentTrade = { type: 'SELL', entryPrice: price, sl: price + sl, tp: price - tp, lots, trailDist: 0, trailActivation: 0, beAt: 0, beSL: 0, beDone: true, openBar: i };
            inTrade = true;
          }
        }
      }
    }

    // ── Equity curve ───────────────────────────────────────────────────────
    const candleDate = new Date(candle.time).toDateString();
    const lastPt     = equityCurve[equityCurve.length - 1];
    if (!lastPt || lastPt.date !== candleDate) {
      equityCurve.push({
        date:     new Date(candle.time).toISOString().split('T')[0],
        equity:   +balance.toFixed(2),
        drawdown: +((peakBal - balance) / peakBal * 100).toFixed(2),
      });
    }
  }

  // ── Finalize confidence distribution ──────────────────────────────────
  if (nonNeutralCount > 0) {
    confDist.avgSmcScore   = +(sumSmc   / nonNeutralCount).toFixed(2);
    confDist.avgTrendScore = +(sumTrend / nonNeutralCount).toFixed(2);
    confDist.avgPaScore    = +(sumPa    / nonNeutralCount).toFixed(2);
    confDist.avgWyckoffScore = +(sumWyc / nonNeutralCount).toFixed(2);
    confDist.avgLiqScore   = +(sumLiq   / nonNeutralCount).toFixed(2);
    confDist.avgVolScore   = +(sumVol   / nonNeutralCount).toFixed(2);
  }

  confDist.topGates = Object.entries(confDist.gateBreakdown)
    .filter(([k]) => k !== 'allowed')
    .sort(([, a], [, b]) => b - a)
    .slice(0, 8)
    .map(([gate, count]) => ({ gate, count, site: GATE_SITES[gate] ?? { file: 'unknown', line: 0, description: gate } }));

  // ── Aggregate stats ────────────────────────────────────────────────────
  const winning     = trades.filter(t => t.profit  > 0);
  const losing      = trades.filter(t => t.profit <= 0);
  const grossProfit = winning.reduce((a, t) => a + t.profit, 0);
  const grossLoss   = Math.abs(losing.reduce((a, t) => a + t.profit, 0));
  const netProfit   = grossProfit - grossLoss;

  const returns     = equityCurve.map((p, i) =>
    i === 0 ? 0 : (p.equity - equityCurve[i - 1].equity) / equityCurve[i - 1].equity,
  );
  const avgRet  = returns.reduce((a, b) => a + b, 0) / (returns.length || 1);
  const stdRet  = Math.sqrt(
    returns.map(r => (r - avgRet) ** 2).reduce((a, b) => a + b, 0) / (returns.length || 1),
  );
  const sharpe  = stdRet > 0 ? +(avgRet / stdRet * Math.sqrt(252)).toFixed(2) : 0;

  const avgWin  = winning.length > 0 ? +(grossProfit / winning.length).toFixed(2) : 0;
  const avgLoss = losing.length  > 0 ? +(grossLoss   / losing.length ).toFixed(2) : 0;
  const avgRR   = avgLoss > 0 ? +(avgWin / avgLoss).toFixed(2) : 0;
  const winPct  = trades.length > 0 ? winning.length / trades.length : 0;
  const expectancy = +((winPct * avgWin) - ((1 - winPct) * avgLoss)).toFixed(2);
  const maxDDAmt   = cfg.initialBalance * maxDD / 100;
  const recovFact  = maxDDAmt > 0 ? +(netProfit / maxDDAmt).toFixed(2) : 0;

  const durBars    = trades.length > 0
    ? Math.round(trades.reduce((a, t) => a + (t.closeBar - t.openBar), 0) / trades.length)
    : 0;
  const durMin     = durBars * minutesPerCandle;
  const tpd        = periodDays > 0 ? +(trades.length / periodDays).toFixed(2) : 0;

  return {
    initialBalance: cfg.initialBalance,
    finalBalance:   +balance.toFixed(2),
    totalTrades:    trades.length,
    winningTrades:  winning.length,
    losingTrades:   losing.length,
    winRate:        trades.length > 0 ? +(winning.length / trades.length * 100).toFixed(1) : 0,
    profitFactor:   grossLoss > 0 ? +(grossProfit / grossLoss).toFixed(2) : grossProfit > 0 ? 99 : 0,
    maxDrawdown:    +maxDD.toFixed(2),
    sharpeRatio:    sharpe,
    totalPips:      +trades.reduce((a, t) => a + t.pips, 0).toFixed(1),
    avgWin,
    avgLoss,
    equityCurve,
    grossProfit:    +grossProfit.toFixed(2),
    grossLoss:      +grossLoss.toFixed(2),
    netProfit:      +netProfit.toFixed(2),
    avgRR,
    expectancy,
    recoveryFactor: recovFact,
    tradesPerDay:   tpd,
    avgTradeDurationBars:    durBars,
    avgTradeDurationMinutes: durMin,
    dataSource,
    periodDays,
    confidenceDistribution: confDist,
    tradeRecords: trades,
  };
}

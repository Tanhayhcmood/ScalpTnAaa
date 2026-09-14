// Backtest Engine — All decisions route through the Decision Engine (v4.0).
//
// Entry signal pipeline (identical to live routes):
//
//   Market Data (sliding window)
//        ↓
//   Decision Engine  (runDecisionEngine)
//        ↓  — runs internally:
//   SMC Engine → Wyckoff Engine → Price Action Engine → Trend Engine
//        ↓
//   Market Regime Detector
//        ↓
//   Confidence Engine  (0–100% score)
//        ↓
//   Quality Filter  (10-category gate)
//        ↓
//   Capital Manager  (SL / TP / lots / trailing / break-even)
//        ↓
//   DecisionResult.allowed === true  →  open trade
//
// M5 path: full Decision Engine per bar (sliding window of SMC_WINDOW bars).
// M15 path: no dedicated SMC/Wyckoff/PA/Trend tuning exists for M15 in
//           the engine suite. M15 keeps the original indicator+SMC-state
//           approach as before — extending those engines to M15 is a new
//           feature, not a fix.
//
// NO parallel decision logic is present here. The M5 entry block contains
// exactly one call: runDecisionEngine(windowCandles, 'M5', balance, risk).

import { generateCandles, calcEMA, calcRSI, calcBB, calcATR } from "./goldEngine.js";
import { computeSmcStatePerBar } from "./smcEngine.js";
import { runDecisionEngine } from "./decisionEngine.js";

export interface BacktestConfig {
  /** Informational only — both engine implementations derive this from the
   *  actual candle count at runtime.  Optional so callers that rely on
   *  dynamic data (CSV backtests) do not have to supply a fixed value. */
  periodDays?: number;
  timeframe: 'M5' | 'M15';
  initialBalance: number;
  riskPerTrade: number;
  emaFastPeriod: number;
  emaSlowPeriod: number;
  rsiPeriod: number;
  rsiOverbought: number;
  rsiOversold: number;
  bbPeriod: number;
  bbDeviation: number;
  atrPeriod: number;
  atrSlMultiplier: number;
  atrTpMultiplier: number;
  /** Minimum SMC structural score (0–1) — used only on the M15 legacy path */
  minSignalScore: number;
}

export interface BacktestTradeRecord {
  type: 'BUY' | 'SELL';
  entryPrice: number;
  exitPrice: number;
  sl: number;
  tp: number;
  pips: number;
  profit: number;
  openBar: number;
  closeBar: number;
}

export interface EquityPoint {
  date: string;
  equity: number;
  drawdown: number;
}

export interface BacktestOutput {
  initialBalance: number;
  finalBalance: number;
  totalTrades: number;
  winningTrades: number;
  losingTrades: number;
  winRate: number;
  profitFactor: number;
  maxDrawdown: number;
  sharpeRatio: number;
  totalPips: number;
  avgWin: number;
  avgLoss: number;
  equityCurve: EquityPoint[];
}

// ── Position sizing helper (M15 legacy path only) ───────────────────────────
// The M5 path uses Capital Manager via runDecisionEngine; no separate
// lot-size calculation is needed there.
function calcLotSize(balance: number, riskPct: number, slPips: number): number {
  const riskAmount = balance * (riskPct / 100);
  const pipValue = 10; // $10 per pip per standard lot (XAUUSD)
  const lots = riskAmount / (slPips * pipValue);
  return Math.max(0.01, Math.min(parseFloat(lots.toFixed(2)), 5));
}

// ── SMC structural score helper (M15 legacy path only) ──────────────────────
function smcDirectionalScore(
  trend:     'BULLISH' | 'BEARISH' | 'NEUTRAL',
  lastBos:   'BUY' | 'SELL' | null,
  lastChoch: 'BUY' | 'SELL' | null,
): { buyScore: number; sellScore: number } {
  let buyScore = 0;
  let sellScore = 0;

  if (trend === 'BULLISH') buyScore  += 2;
  else if (trend === 'BEARISH') sellScore += 2;

  if (lastBos === 'BUY')  buyScore  += 2;
  else if (lastBos === 'SELL') sellScore += 2;

  if (lastChoch === 'BUY')  buyScore  += 3;
  else if (lastChoch === 'SELL') sellScore += 3;

  return { buyScore, sellScore };
}

// ── Managed trade state (shared by both paths) ───────────────────────────────
interface ManagedTrade {
  type: 'BUY' | 'SELL';
  entryPrice: number;
  sl: number;
  tp: number;
  lots: number;
  trailDist: number;
  trailActivation: number;
  beAt: number;
  beSL: number;
  beDone: boolean;
  openBar: number;
}

// XAUUSD: $1 price move × 1 lot = $100 P&L (Capital Manager convention).
function dollarPnl(direction: 'BUY' | 'SELL', entry: number, exit: number, lots: number): number {
  const dirMult = direction === 'BUY' ? 1 : -1;
  return dirMult * (exit - entry) * 100 * lots;
}

// How many bars to feed the Decision Engine per bar evaluation.
// Mirrors the candle counts used by the live /gold/decision route (M5: 250).
const SMC_WINDOW = 250;

export function runBacktest(cfg: BacktestConfig): BacktestOutput {
  const minuteMap: Record<string, number> = { M5: 5, M15: 15 };
  const minutesPerCandle = minuteMap[cfg.timeframe];
  // periodDays is optional in BacktestConfig (V2 computes it from candle count
  // instead of reading it from cfg).  runBacktest still uses it for synthetic
  // candle generation; fall back to 252 trading days if callers omit it.
  const totalCandles = Math.floor(((cfg.periodDays ?? 252) * 24 * 60) / minutesPerCandle) + 200;

  const candles = generateCandles(cfg.timeframe as 'M5' | 'M15', totalCandles);
  const closes = candles.map(c => c.close);

  // Indicator arrays — needed only for the M15 legacy path.
  const emaFast = calcEMA(closes, cfg.emaFastPeriod);
  const emaSlow = calcEMA(closes, cfg.emaSlowPeriod);
  const rsi     = calcRSI(closes, cfg.rsiPeriod);
  const bb      = calcBB(closes, cfg.bbPeriod, cfg.bbDeviation);
  const atr     = calcATR(candles, cfg.atrPeriod);

  const useFullPipeline = cfg.timeframe === 'M5';

  // Per-bar SMC state — only needed for the M15 fallback path.
  const smcStates = useFullPipeline ? null : computeSmcStatePerBar(candles, cfg.timeframe);

  const trades: BacktestTradeRecord[] = [];
  let balance = cfg.initialBalance;
  let peakBalance = balance;
  let maxDrawdown = 0;
  let inTrade = false;
  let currentTrade: ManagedTrade | null = null;
  const equityCurve: EquityPoint[] = [];

  const warmup = useFullPipeline
    ? SMC_WINDOW
    : Math.max(cfg.emaSlowPeriod, cfg.rsiPeriod, cfg.bbPeriod, cfg.atrPeriod) + 5;

  for (let i = warmup; i < candles.length; i++) {
    const candle = candles[i];
    const price  = candle.close;

    // ── Manage open trade: break-even, trailing stop, SL/TP hit ────────────
    if (inTrade && currentTrade) {
      let closed    = false;
      let exitPrice = price;

      // Break-even (M5 full-pipeline trades have trailDist > 0; M15 legacy has beDone=true)
      if (!currentTrade.beDone) {
        if (currentTrade.type === 'BUY' && candle.high >= currentTrade.beAt) {
          currentTrade.sl = Math.max(currentTrade.sl, currentTrade.beSL);
          currentTrade.beDone = true;
        } else if (currentTrade.type === 'SELL' && candle.low <= currentTrade.beAt) {
          currentTrade.sl = Math.min(currentTrade.sl, currentTrade.beSL);
          currentTrade.beDone = true;
        }
      }

      // Trailing stop (activates past trailActivation price level)
      if (currentTrade.trailDist > 0) {
        if (currentTrade.type === 'BUY' && candle.high >= currentTrade.trailActivation) {
          currentTrade.sl = Math.max(currentTrade.sl, candle.high - currentTrade.trailDist);
        } else if (currentTrade.type === 'SELL' && candle.low <= currentTrade.trailActivation) {
          currentTrade.sl = Math.min(currentTrade.sl, candle.low + currentTrade.trailDist);
        }
      }

      // SL/TP hit check
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
        peakBalance   = Math.max(peakBalance, balance);
        const dd      = ((peakBalance - balance) / peakBalance) * 100;
        maxDrawdown   = Math.max(maxDrawdown, dd);

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

        inTrade      = false;
        currentTrade = null;
      }
    }

    // ── Entry evaluation ────────────────────────────────────────────────────
    if (!inTrade) {
      if (useFullPipeline) {
        // ── M5: Full Decision Engine pipeline ─────────────────────────────
        //
        // runDecisionEngine runs the complete pipeline internally:
        //   SMC Engine → Wyckoff → Price Action → Trend
        //   → Market Regime Detector
        //   → Confidence Engine
        //   → Quality Filter
        //   → Capital Manager
        //
        // A trade is opened ONLY when decision.allowed === true.
        // No supplementary checks or parallel logic are applied here.
        const windowCandles = candles.slice(i - SMC_WINDOW + 1, i + 1);

        const decision = runDecisionEngine(
          windowCandles,
          'M5',
          balance,
          cfg.riskPerTrade,
        );

        // Gate: trade only when the full pipeline approves
        if (!decision.allowed || decision.direction === 'NEUTRAL') continue;

        // Capital Manager output is embedded in DecisionResult.tradeParams
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
        // ── M15 legacy path ───────────────────────────────────────────────
        // No dedicated SMC/Wyckoff/PA/Trend engine tuning exists for M15.
        // Extending those engines to M15 would be a new feature, so this
        // path keeps the original indicator+SMC-state approach unchanged.
        const smcState  = smcStates![i] ?? { trend: 'NEUTRAL', lastBosDir: null, lastChochDir: null };
        const { buyScore: smcBuy, sellScore: smcSell } = smcDirectionalScore(
          smcState.trend, smcState.lastBosDir, smcState.lastChochDir,
        );

        const hasBuySmcTrigger  = smcBuy  > 0;
        const hasSellSmcTrigger = smcSell > 0;
        if (!hasBuySmcTrigger && !hasSellSmcTrigger) continue;

        const smcMaxRaw = 7;
        const smcQuality = Math.max(smcBuy, smcSell) / smcMaxRaw;
        if (smcQuality < cfg.minSignalScore * 0.5) continue;

        const indBuyVotes = (
          (emaFast[i] > emaSlow[i] ? 1 : 0) +
          (rsi[i] < cfg.rsiOversold ? 1 : 0) +
          (price <= bb.lower[i]     ? 1 : 0)
        );
        const indSellVotes = (
          (emaFast[i] < emaSlow[i]    ? 1 : 0) +
          (rsi[i] > cfg.rsiOverbought ? 1 : 0) +
          (price >= bb.upper[i]       ? 1 : 0)
        );

        const totalBuy  = smcBuy  * 2 + indBuyVotes;
        const totalSell = smcSell * 2 + indSellVotes;

        const atrVal = atr[i];
        const sl     = atrVal * cfg.atrSlMultiplier;
        const tp     = atrVal * cfg.atrTpMultiplier;
        const slPips = sl / 0.1;
        const lots   = calcLotSize(balance, cfg.riskPerTrade, slPips);

        if (hasBuySmcTrigger && !hasSellSmcTrigger) {
          if (indSellVotes < 3) {
            currentTrade = { type: 'BUY', entryPrice: price, sl: price - sl, tp: price + tp, lots, trailDist: 0, trailActivation: 0, beAt: 0, beSL: 0, beDone: true, openBar: i };
            inTrade = true;
          }
        } else if (hasSellSmcTrigger && !hasBuySmcTrigger) {
          if (indBuyVotes < 3) {
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

    // ── Record equity at daily intervals ────────────────────────────────────
    const candleDate = new Date(candle.time).toDateString();
    const lastPoint  = equityCurve[equityCurve.length - 1];
    if (!lastPoint || lastPoint.date !== candleDate) {
      const dd = ((peakBalance - balance) / peakBalance) * 100;
      equityCurve.push({
        date:     new Date(candle.time).toISOString().split('T')[0],
        equity:   +balance.toFixed(2),
        drawdown: +dd.toFixed(2),
      });
    }
  }

  // ── Aggregate statistics ────────────────────────────────────────────────
  const winning     = trades.filter(t => t.profit  > 0);
  const losing      = trades.filter(t => t.profit <= 0);
  const grossProfit = winning.reduce((a, t) => a + t.profit, 0);
  const grossLoss   = Math.abs(losing.reduce((a, t) => a + t.profit, 0));

  const returns   = equityCurve.map((p, i) =>
    i === 0 ? 0 : (p.equity - equityCurve[i - 1].equity) / equityCurve[i - 1].equity,
  );
  // Guard against empty equityCurve (returns array has 0 or 1 element).
  // Without the guard, dividing by 0 produces NaN which propagates into the
  // Sharpe ratio and all downstream statistics.  backtestEngineV2 already
  // uses the same `|| 1` pattern.
  const n = returns.length || 1;
  const avgReturn = returns.reduce((a, b) => a + b, 0) / n;
  const stdReturn = Math.sqrt(
    returns.map(r => (r - avgReturn) ** 2).reduce((a, b) => a + b, 0) / n,
  );
  const sharpe = stdReturn > 0 ? +(avgReturn / stdReturn * Math.sqrt(252)).toFixed(2) : 0;

  return {
    initialBalance: cfg.initialBalance,
    finalBalance:   +balance.toFixed(2),
    totalTrades:    trades.length,
    winningTrades:  winning.length,
    losingTrades:   losing.length,
    winRate:        trades.length > 0 ? +(winning.length / trades.length * 100).toFixed(1) : 0,
    profitFactor:   grossLoss > 0 ? +(grossProfit / grossLoss).toFixed(2) : grossProfit > 0 ? 99 : 0,
    maxDrawdown:    +maxDrawdown.toFixed(2),
    sharpeRatio:    sharpe,
    totalPips:      +trades.reduce((a, t) => a + t.pips, 0).toFixed(1),
    avgWin:         winning.length > 0 ? +(grossProfit / winning.length).toFixed(2) : 0,
    avgLoss:        losing.length  > 0 ? +(grossLoss   / losing.length ).toFixed(2) : 0,
    equityCurve,
  };
}

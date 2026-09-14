#!/usr/bin/env tsx
// comparativeBacktest.ts — Gold Scalper Pro v4 · Comparative Analysis
//
// Runs 4 strategy combinations and reports per-year + aggregate results:
//
//   A) MIN_CONFIRMATIONS=3  +  Direct Entry   (baseline)
//   B) MIN_CONFIRMATIONS=2  +  Direct Entry
//   C) MIN_CONFIRMATIONS=3  +  Pullback Entry
//   D) MIN_CONFIRMATIONS=2  +  Pullback Entry
//
// Rules:
//   • ATR High Volatility Filter: OFF (useAtrHighVolFilter=false) — disabled
//     because previous tests showed it reduced profit without improving DD.
//   • TP / SL / FIXED_2R / Capital Manager: UNCHANGED
//   • Per-year breakdown: 2021 · 2022 · 2023 · 2024 · 2025
//
// Usage:
//   npx tsx src/comparativeBacktest.ts [--csv path/to/data.csv]

import path from 'node:path';
import { CsvDataProvider } from './lib/csvDataProvider.js';
import { runBacktestV2, type BacktestOutputV2 } from './lib/backtestEngineV2.js';
import { calibrateM5Config, setCalibratedM5Config } from './lib/wyckoffEngine.js';
import { calcEMA, calcATR, type OHLCV } from './lib/goldEngine.js';
import { runDecisionEngine } from './lib/decisionEngine.js';

// ===== CLI =====

function parseCsvArg(): string {
  const args = process.argv.slice(2);
  const idx  = args.indexOf('--csv');
  return idx !== -1 && args[idx + 1]
    ? args[idx + 1]
    : path.join(process.cwd(), 'data', 'xauusd_m5_2021_2025.csv');
}

// ===== BASE BACKTEST CONFIG =====

const BASE_CFG = {
  timeframe:         'M5' as const,
  initialBalance:    10_000,
  riskPerTrade:      1.0,
  emaFastPeriod:     9,
  emaSlowPeriod:     21,
  rsiPeriod:         14,
  rsiOverbought:     70,
  rsiOversold:       30,
  bbPeriod:          20,
  bbDeviation:       2.0,
  atrPeriod:         14,
  atrSlMultiplier:   1.5,
  atrTpMultiplier:   3.0,
  minSignalScore:    0.60,
};

// ===== YEAR SLICES =====

const YEAR_SLICES = [
  { label: '2021', startDate: '2021-01-01', endDate: '2021-12-31' },
  { label: '2022', startDate: '2022-01-01', endDate: '2022-12-31' },
  { label: '2023', startDate: '2023-01-01', endDate: '2023-12-31' },
  { label: '2024', startDate: '2024-01-01', endDate: '2024-12-31' },
  { label: '2025', startDate: '2025-01-01', endDate: '2025-07-14' },
];

// ===== PULLBACK ENTRY ENGINE =====
//
// After BOS fires and decision is allowed, instead of entering immediately:
//   1. Mark trade as "pending pullback"
//   2. For up to PULLBACK_WAIT_BARS candles, check if price returns to:
//      - Order Block zone (OB high/low ± tolerance)
//      - Fair Value Gap zone (FVG top/bottom)
//      - EMA50 (within ATR × PULLBACK_EMA_TOL)
//   3. If touched: enter from that zone with adjusted TP (same R:R)
//   4. If not touched in time: cancel pending entry

const PULLBACK_WAIT_BARS    = 5;      // max candles to wait for pullback
const PULLBACK_EMA_TOL_ATR  = 0.35;  // price within ATR×0.35 of EMA50 = "touched"
const SMC_WINDOW_PB         = 250;   // same as backtestEngineV2

interface PendingPullback {
  direction:   'BUY' | 'SELL';
  bosBar:      number;
  lots:        number;
  slDistance:  number;   // original SL distance in price units
  // key zones (from smc result)
  obLow?:      number;
  obHigh?:     number;
  fvgTop?:     number;
  fvgBottom?:  number;
}

interface ManagedTrade {
  type:      'BUY' | 'SELL';
  entryPrice: number;
  sl:        number;
  tp:        number;
  lots:      number;
  openBar:   number;
}

function dollarPnl(dir: 'BUY' | 'SELL', entry: number, exit: number, lots: number): number {
  return (dir === 'BUY' ? 1 : -1) * (exit - entry) * 100 * lots;
}

interface TradeRecord {
  type:       'BUY' | 'SELL';
  entryPrice: number;
  exitPrice:  number;
  sl:         number;
  tp:         number;
  profit:     number;
  openBar:    number;
  closeBar:   number;
}

interface YearResult {
  label:       string;
  trades:      number;
  winRate:     number;
  netProfit:   number;
  profitFactor: number;
  maxDrawdown: number;
  expectancy:  number;
}

interface ComboResult {
  label:       string;
  totalTrades: number;
  winRate:     number;
  netProfit:   number;
  profitFactor: number;
  maxDrawdown: number;
  expectancy:  number;
  yearResults: YearResult[];
}

// ===== PULLBACK BACKTEST LOOP =====
// A custom backtest loop that adds pullback-entry logic on top of the
// full M5 decision engine.

function runPullbackBacktest(
  candles:     OHLCV[],
  dataSource:  string,
  minConfs:    number,
): ComboResult {
  // Pre-compute EMA50 for the whole candle array
  const closes = candles.map(c => c.close);
  const ema50  = calcEMA(closes, 50);
  const atrArr = calcATR(candles, 14);

  let balance = BASE_CFG.initialBalance;
  let peakBal = balance;
  let maxDD   = 0;
  const allTrades: TradeRecord[] = [];
  const equityByBar: number[] = new Array(candles.length).fill(balance);

  let inTrade: ManagedTrade | null  = null;
  let pending: PendingPullback | null = null;

  for (let i = SMC_WINDOW_PB; i < candles.length; i++) {
    const candle = candles[i];

    // ── Manage open trade ─────────────────────────────────────────────
    if (inTrade) {
      let closed    = false;
      let exitPrice = candle.close;

      if (inTrade.type === 'BUY') {
        if (candle.low  <= inTrade.sl) { exitPrice = inTrade.sl; closed = true; }
        else if (candle.high >= inTrade.tp) { exitPrice = inTrade.tp; closed = true; }
      } else {
        if (candle.high >= inTrade.sl) { exitPrice = inTrade.sl; closed = true; }
        else if (candle.low  <= inTrade.tp) { exitPrice = inTrade.tp; closed = true; }
      }

      if (closed) {
        const profit = dollarPnl(inTrade.type, inTrade.entryPrice, exitPrice, inTrade.lots);
        balance     += profit;
        peakBal      = Math.max(peakBal, balance);
        const dd     = ((peakBal - balance) / peakBal) * 100;
        maxDD        = Math.max(maxDD, dd);
        allTrades.push({
          type: inTrade.type, entryPrice: inTrade.entryPrice, exitPrice,
          sl: inTrade.sl, tp: inTrade.tp, profit: +profit.toFixed(2),
          openBar: inTrade.openBar, closeBar: i,
        });
        inTrade = null;
        pending = null;  // cancel any pending pullback when a trade closes
      }
      equityByBar[i] = balance;
      continue;
    }

    // ── Check pending pullback entry ───────────────────────────────────
    if (pending) {
      const barsWaited = i - pending.bosBar;
      if (barsWaited > PULLBACK_WAIT_BARS) {
        // Timeout — cancel pending entry
        pending = null;
      } else {
        const atr = atrArr[i] ?? atrArr[atrArr.length - 1];
        const e50 = ema50[i] ?? ema50[ema50.length - 1];
        const tol = atr * PULLBACK_EMA_TOL_ATR;

        // Check if price returned to a zone
        let triggered = false;
        let entryPrice = 0;

        if (pending.direction === 'BUY') {
          // Touched bullish OB zone
          if (pending.obLow !== undefined && candle.low <= pending.obHigh! && candle.low >= pending.obLow! - tol) {
            triggered  = true;
            entryPrice = Math.max(pending.obLow!, candle.close);
          }
          // Touched bullish FVG zone
          else if (pending.fvgBottom !== undefined && candle.low <= pending.fvgTop! && candle.low >= pending.fvgBottom! - tol) {
            triggered  = true;
            entryPrice = Math.max(pending.fvgBottom!, candle.close);
          }
          // Touched EMA50
          else if (Math.abs(candle.low - e50) <= tol) {
            triggered  = true;
            entryPrice = candle.close;
          }

          if (triggered && entryPrice > 0) {
            const sl   = entryPrice - pending.slDistance;
            const tp   = entryPrice + pending.slDistance * 2;   // Fixed 2R
            inTrade = { type: 'BUY', entryPrice, sl, tp, lots: pending.lots, openBar: i };
            pending = null;
          }
        } else {
          // Touched bearish OB zone
          if (pending.obHigh !== undefined && candle.high >= pending.obLow! && candle.high <= pending.obHigh! + tol) {
            triggered  = true;
            entryPrice = Math.min(pending.obHigh!, candle.close);
          }
          // Touched bearish FVG zone
          else if (pending.fvgTop !== undefined && candle.high >= pending.fvgBottom! && candle.high <= pending.fvgTop! + tol) {
            triggered  = true;
            entryPrice = Math.min(pending.fvgTop!, candle.close);
          }
          // Touched EMA50
          else if (Math.abs(candle.high - e50) <= tol) {
            triggered  = true;
            entryPrice = candle.close;
          }

          if (triggered && entryPrice > 0) {
            const sl   = entryPrice + pending.slDistance;
            const tp   = entryPrice - pending.slDistance * 2;   // Fixed 2R
            inTrade = { type: 'SELL', entryPrice, sl, tp, lots: pending.lots, openBar: i };
            pending = null;
          }
        }
      }
      equityByBar[i] = balance;
      continue;
    }

    // ── New entry evaluation ───────────────────────────────────────────
    const windowCandles = candles.slice(i - SMC_WINDOW_PB + 1, i + 1);
    const decision = runDecisionEngine(windowCandles, 'M5', balance, BASE_CFG.riskPerTrade, {
      minConfirmations:    minConfs,
      useAtrHighVolFilter: false,   // ALWAYS off per spec
    });

    if (!decision.allowed || decision.direction === 'NEUTRAL') {
      equityByBar[i] = balance;
      continue;
    }

    const capital = decision.tradeParams!;

    // Derive the SL distance (in price units) from the capital manager output
    const slDist = Math.abs(capital.entryPrice - capital.stopLoss);

    // Extract nearest OB and FVG zones from the SMC result
    const smc   = decision.smc;
    const dir   = decision.direction;
    let obLow: number | undefined, obHigh: number | undefined;
    let fvgTop: number | undefined, fvgBottom: number | undefined;

    for (const ob of smc.orderBlocks) {
      if (dir === 'BUY'  && ob.type === 'BULLISH') { obLow  = ob.low;  obHigh = ob.high;  break; }
      if (dir === 'SELL' && ob.type === 'BEARISH') { obLow  = ob.low;  obHigh = ob.high;  break; }
    }
    for (const fvg of smc.fairValueGaps) {
      if (dir === 'BUY'  && fvg.type === 'BULLISH') { fvgTop = fvg.top; fvgBottom = fvg.bottom; break; }
      if (dir === 'SELL' && fvg.type === 'BEARISH') { fvgTop = fvg.top; fvgBottom = fvg.bottom; break; }
    }

    // Set pending pullback (actual entry happens in subsequent bars)
    pending = {
      direction: dir,
      bosBar:    i,
      lots:      capital.lotSize,
      slDistance: slDist,
      obLow, obHigh, fvgTop, fvgBottom,
    };

    equityByBar[i] = balance;
  }

  // ── Aggregate stats ─────────────────────────────────────────────────
  return buildComboResult(`Pullback Entry (conf=${minConfs})`, allTrades, BASE_CFG.initialBalance, maxDD, candles, dataSource);
}

// ===== RESULT BUILDER =====

function buildComboResult(
  label: string,
  trades: TradeRecord[],
  initialBalance: number,
  maxDD: number,
  candles: OHLCV[],
  _dataSource: string,
): ComboResult {
  const winning = trades.filter(t => t.profit > 0);
  const losing  = trades.filter(t => t.profit <= 0);

  const grossProfit = winning.reduce((a, t) => a + t.profit, 0);
  const grossLoss   = Math.abs(losing.reduce((a, t) => a + t.profit, 0));
  const netProfit   = grossProfit - grossLoss;
  const winRate     = trades.length > 0 ? (winning.length / trades.length) * 100 : 0;
  const pf          = grossLoss > 0 ? grossProfit / grossLoss : grossProfit > 0 ? 99 : 0;

  const avgWin    = winning.length > 0 ? grossProfit / winning.length : 0;
  const avgLoss   = losing.length  > 0 ? grossLoss   / losing.length  : 0;
  const winPct    = trades.length  > 0 ? winning.length / trades.length : 0;
  const expectancy = (winPct * avgWin) - ((1 - winPct) * avgLoss);

  // Per-year breakdown
  const yearResults: YearResult[] = YEAR_SLICES.map(slice => {
    const start = new Date(slice.startDate).getTime();
    const end   = new Date(slice.endDate + 'T23:59:59Z').getTime();

    const yTrades = trades.filter(t => {
      const time = new Date(candles[t.openBar]?.time ?? '').getTime();
      return time >= start && time <= end;
    });

    if (yTrades.length === 0) {
      return { label: slice.label, trades: 0, winRate: 0, netProfit: 0, profitFactor: 0, maxDrawdown: 0, expectancy: 0 };
    }

    const yWin = yTrades.filter(t => t.profit > 0);
    const yLos = yTrades.filter(t => t.profit <= 0);
    const yGP  = yWin.reduce((a, t) => a + t.profit, 0);
    const yGL  = Math.abs(yLos.reduce((a, t) => a + t.profit, 0));
    const yPF  = yGL > 0 ? yGP / yGL : yGP > 0 ? 99 : 0;
    const yWR  = (yWin.length / yTrades.length) * 100;
    const yNet = yGP - yGL;

    const yAvgWin  = yWin.length > 0 ? yGP / yWin.length : 0;
    const yAvgLoss = yLos.length > 0 ? yGL / yLos.length  : 0;
    const yWinPct  = yWin.length / yTrades.length;

    // Per-year max drawdown (sequential equity tracking)
    let yBal = 0; let yPeak = 0; let yDD = 0;
    for (const t of yTrades) {
      yBal += t.profit;
      yPeak = Math.max(yPeak, yBal);
      yDD   = Math.max(yDD, yBal < yPeak ? ((yPeak - yBal) / (initialBalance + yPeak)) * 100 : 0);
    }

    return {
      label:        slice.label,
      trades:       yTrades.length,
      winRate:      +yWR.toFixed(1),
      netProfit:    +yNet.toFixed(2),
      profitFactor: +yPF.toFixed(2),
      maxDrawdown:  +yDD.toFixed(2),
      expectancy:   +((yWinPct * yAvgWin) - ((1 - yWinPct) * yAvgLoss)).toFixed(2),
    };
  });

  return {
    label,
    totalTrades:  trades.length,
    winRate:      +winRate.toFixed(1),
    netProfit:    +netProfit.toFixed(2),
    profitFactor: +pf.toFixed(2),
    maxDrawdown:  +maxDD.toFixed(2),
    expectancy:   +expectancy.toFixed(2),
    yearResults,
  };
}

// ===== CONVERT BacktestOutputV2 → ComboResult =====

function convertV2(label: string, r: BacktestOutputV2, candles: OHLCV[]): ComboResult {
  // Re-compute per-year breakdown from tradeRecords
  const trades: TradeRecord[] = r.tradeRecords.map(t => ({
    type:       t.type,
    entryPrice: t.entryPrice,
    exitPrice:  t.exitPrice,
    sl:         t.sl,
    tp:         t.tp,
    profit:     t.profit,
    openBar:    t.openBar,
    closeBar:   t.closeBar,
  }));

  const yearResults: YearResult[] = YEAR_SLICES.map(slice => {
    const start = new Date(slice.startDate).getTime();
    const end   = new Date(slice.endDate + 'T23:59:59Z').getTime();

    const yTrades = trades.filter(t => {
      const time = new Date(candles[t.openBar]?.time ?? '').getTime();
      return time >= start && time <= end;
    });

    if (yTrades.length === 0) {
      return { label: slice.label, trades: 0, winRate: 0, netProfit: 0, profitFactor: 0, maxDrawdown: 0, expectancy: 0 };
    }

    const yWin = yTrades.filter(t => t.profit > 0);
    const yLos = yTrades.filter(t => t.profit <= 0);
    const yGP  = yWin.reduce((a, t) => a + t.profit, 0);
    const yGL  = Math.abs(yLos.reduce((a, t) => a + t.profit, 0));
    const yPF  = yGL > 0 ? yGP / yGL : yGP > 0 ? 99 : 0;
    const yWR  = (yWin.length / yTrades.length) * 100;
    const yNet = yGP - yGL;

    const yAvgWin  = yWin.length > 0 ? yGP / yWin.length : 0;
    const yAvgLoss = yLos.length > 0 ? yGL / yLos.length  : 0;
    const yWinPct  = yWin.length / yTrades.length;

    let yBal = 0; let yPeak = 0; let yDD = 0;
    for (const t of yTrades) {
      yBal += t.profit;
      yPeak = Math.max(yPeak, yBal);
      yDD   = Math.max(yDD, yBal < yPeak ? ((yPeak - yBal) / (BASE_CFG.initialBalance + yPeak)) * 100 : 0);
    }

    return {
      label:        slice.label,
      trades:       yTrades.length,
      winRate:      +yWR.toFixed(1),
      netProfit:    +yNet.toFixed(2),
      profitFactor: +yPF.toFixed(2),
      maxDrawdown:  +yDD.toFixed(2),
      expectancy:   +((yWinPct * yAvgWin) - ((1 - yWinPct) * yAvgLoss)).toFixed(2),
    };
  });

  return { label, totalTrades: r.totalTrades, winRate: r.winRate,
    netProfit: r.netProfit, profitFactor: r.profitFactor, maxDrawdown: r.maxDrawdown,
    expectancy: r.expectancy, yearResults };
}

// ===== PRINT HELPERS =====

const HR  = '═'.repeat(76);
const hr  = '─'.repeat(76);

function pad(v: string | number, w: number): string { return String(v).padEnd(w); }
function rpad(v: string | number, w: number): string { return String(v).padStart(w); }
function money(n: number): string { return (n >= 0 ? '+$' : '-$') + Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
function pct(n: number):   string { return n.toFixed(1) + '%'; }
function num(n: number, d = 2): string { return n.toFixed(d); }

function printComboSummary(r: ComboResult, id: string) {
  const pfOk  = r.profitFactor >= 1.3;
  const ddOk  = r.maxDrawdown  < 15;
  const trOk  = r.totalTrades  >= 30;
  const pass  = pfOk && ddOk && trOk;

  console.log(`\n[${id}] ${r.label}`);
  console.log(hr);
  console.log(`  Total Trades   : ${rpad(r.totalTrades, 6)}`);
  console.log(`  Win Rate       : ${rpad(pct(r.winRate), 6)}`);
  console.log(`  Net Profit     : ${money(r.netProfit)}`);
  console.log(`  Profit Factor  : ${rpad(num(r.profitFactor), 6)}  ${pfOk ? '✅ ≥ 1.3' : '❌ < 1.3'}`);
  console.log(`  Max Drawdown   : ${rpad(pct(r.maxDrawdown), 6)}  ${ddOk ? '✅ < 15%' : '❌ ≥ 15%'}`);
  console.log(`  Expectancy/trd : ${money(r.expectancy)}`);
  console.log(`  Verdict        : ${pass ? '✅ PASSES ALL CRITERIA' : '⚠️  DOES NOT PASS ALL CRITERIA'}`);
}

function printYearTable(r: ComboResult) {
  console.log(`\n  Per-Year Breakdown — ${r.label}`);
  console.log('  ' + '─'.repeat(72));
  console.log('  ' + pad('Year', 6) + pad('Trades', 8) + pad('WR', 8) + pad('Net P&L', 14) + pad('PF', 8) + pad('MaxDD', 8) + 'Exp/trd');
  console.log('  ' + '─'.repeat(72));
  let posYears = 0;
  for (const y of r.yearResults) {
    const pfFlg = y.profitFactor >= 1.0 ? '' : ' ⚠️';
    const ddFlg = y.maxDrawdown  < 15   ? '' : ' 🔴';
    if (y.netProfit > 0) posYears++;
    console.log('  ' + pad(y.label, 6) + pad(y.trades, 8) + pad(pct(y.winRate), 8) +
      pad(money(y.netProfit), 14) + pad(num(y.profitFactor), 8) +
      pad(pct(y.maxDrawdown) + ddFlg, 12) + money(y.expectancy) + pfFlg);
  }
  const total = r.yearResults.filter(y => y.trades > 0).length;
  console.log('  ' + '─'.repeat(72));
  console.log(`  Positive years : ${posYears} / ${total}`);
}

function printComparisonMatrix(results: Array<{ id: string; r: ComboResult }>) {
  console.log('\n' + HR);
  console.log('  COMPARISON MATRIX — All 4 Combinations');
  console.log(hr);
  console.log('  ' + pad('ID', 4) + pad('Strategy', 36) + pad('Trades', 8) + pad('WR', 7) +
    pad('Net Profit', 14) + pad('PF', 7) + pad('MaxDD', 8) + 'Exp/trd');
  console.log('  ' + '─'.repeat(90));
  for (const { id, r } of results) {
    const stars = (r.profitFactor >= 1.3 && r.maxDrawdown < 15 && r.totalTrades >= 30) ? ' ★' : '';
    console.log('  ' + pad(id, 4) + pad(r.label + stars, 36) + pad(r.totalTrades, 8) +
      pad(pct(r.winRate), 7) + pad(money(r.netProfit), 14) +
      pad(num(r.profitFactor), 7) + pad(pct(r.maxDrawdown), 8) + money(r.expectancy));
  }
  console.log('  ' + '─'.repeat(90));
  console.log('  ★ = Passes all 3 criteria (PF ≥ 1.3, MaxDD < 15%, Trades ≥ 30)');
}

function printFinalRecommendation(results: Array<{ id: string; r: ComboResult }>) {
  console.log('\n' + HR);
  console.log('  FINAL RECOMMENDATION');
  console.log(hr);
  console.log('  Selection Criteria:');
  console.log('    1. Profit Factor ≥ 1.3');
  console.log('    2. Max Drawdown  < 15%');
  console.log('    3. Total Trades  ≥ 30 (statistically meaningful)');
  console.log('    4. Positive performance in majority of years');
  console.log('');

  // Score each combo
  const scored = results.map(({ id, r }) => {
    let score = 0;
    const reasons: string[] = [];

    if (r.profitFactor >= 1.3)   { score += 30; reasons.push(`PF ${num(r.profitFactor)} ≥ 1.3`); }
    else                          { reasons.push(`PF ${num(r.profitFactor)} < 1.3 ✗`); }

    if (r.maxDrawdown < 15)       { score += 25; reasons.push(`DD ${pct(r.maxDrawdown)} < 15%`); }
    else                          { reasons.push(`DD ${pct(r.maxDrawdown)} ≥ 15% ✗`); }

    if (r.totalTrades >= 30)      { score += 15; reasons.push(`Trades ${r.totalTrades} ≥ 30`); }
    else if (r.totalTrades >= 15) { score += 5;  reasons.push(`Trades ${r.totalTrades} (marginal)`); }
    else                          { reasons.push(`Trades ${r.totalTrades} < 15 ✗`); }

    const posYears = r.yearResults.filter(y => y.trades > 0 && y.netProfit > 0).length;
    const totYears = r.yearResults.filter(y => y.trades > 0).length;
    if (posYears >= Math.ceil(totYears * 0.6)) {
      score += 20;
      reasons.push(`Positive ${posYears}/${totYears} years`);
    } else {
      reasons.push(`Positive only ${posYears}/${totYears} years ✗`);
    }

    // Bonus for higher PF
    if (r.profitFactor >= 1.5) { score += 10; reasons.push('Bonus: PF ≥ 1.5'); }

    return { id, r, score, reasons };
  });

  scored.sort((a, b) => b.score - a.score);

  for (let rank = 0; rank < scored.length; rank++) {
    const { id, r, score, reasons } = scored[rank];
    const medal = rank === 0 ? '🥇' : rank === 1 ? '🥈' : rank === 2 ? '🥉' : '  ';
    console.log(`  ${medal} Rank ${rank + 1}: [${id}] ${r.label}  (Score: ${score}/100)`);
    for (const rs of reasons) console.log(`        • ${rs}`);
    console.log('');
  }

  const best = scored[0];
  console.log('  ► RECOMMENDED COMBINATION:');
  console.log(`    [${best.id}] ${best.r.label}`);
  console.log(`    Net Profit: ${money(best.r.netProfit)} | PF: ${num(best.r.profitFactor)} | DD: ${pct(best.r.maxDrawdown)}`);

  if (best.score < 50) {
    console.log('');
    console.log('  ⚠️  WARNING: No combination fully passes all 4 criteria.');
    console.log('     Consider refining additional parameters before live deployment.');
  }
}

// ===== MAIN =====

async function main() {
  const csvPath = parseCsvArg();

  console.log('\n' + HR);
  console.log('  GOLD SCALPER PRO v4 — Comparative Backtest Report');
  console.log('  XAUUSD · M5 · Risk 1%/trade · Initial Balance $10,000');
  console.log('  ATR High Volatility Filter: OFF (disabled — default=false)');
  console.log(HR);
  console.log('');
  console.log('  Loading CSV data: ' + csvPath);

  const mainProvider = new CsvDataProvider(csvPath);
  await mainProvider.load();
  mainProvider.printLoadSummary();
  const allCandles = mainProvider.getAllCandles();

  console.log(`\n  Candles: ${allCandles.length.toLocaleString()}`);
  if (allCandles.length > 0) {
    console.log(`  Range  : ${allCandles[0].time} → ${allCandles[allCandles.length - 1].time}`);
  }

  // ── Calibrate Wyckoff ────────────────────────────────────────────
  console.log('\n  Calibrating Wyckoff M5 config from real data...');
  const calibratedCfg = calibrateM5Config(allCandles);
  setCalibratedM5Config(calibratedCfg);
  console.log(`  Done. maxRangePct=${(calibratedCfg.maxRangePct * 100).toFixed(3)}% | springMargin=${calibratedCfg.springMargin.toFixed(2)}`);

  // ── Run 4 combinations ───────────────────────────────────────────
  const dataSource = mainProvider.sourceLabel;

  const combos: Array<{ id: string; label: string; minConfs: number; pullback: boolean }> = [
    { id: 'A', label: 'Conf=3, Direct Entry',   minConfs: 3, pullback: false },
    { id: 'B', label: 'Conf=2, Direct Entry',   minConfs: 2, pullback: false },
    { id: 'C', label: 'Conf=3, Pullback Entry', minConfs: 3, pullback: true  },
    { id: 'D', label: 'Conf=2, Pullback Entry', minConfs: 2, pullback: true  },
  ];

  const comboResults: Array<{ id: string; r: ComboResult }> = [];

  for (const combo of combos) {
    process.stdout.write(`\n  [${combo.id}] Running ${combo.label}... `);

    let result: ComboResult;

    if (!combo.pullback) {
      // Direct Entry — use existing runBacktestV2 engine
      const raw = runBacktestV2(
        BASE_CFG,
        allCandles,
        dataSource,
        { minConfirmations: combo.minConfs, useAtrHighVolFilter: false },
      );
      result = convertV2(combo.label, raw, allCandles);
    } else {
      // Pullback Entry — custom loop
      result = runPullbackBacktest(allCandles, dataSource, combo.minConfs);
      result.label = combo.label;
    }

    comboResults.push({ id: combo.id, r: result });
    console.log(`done. Trades: ${result.totalTrades} | WR: ${pct(result.winRate)} | Net: ${money(result.netProfit)} | PF: ${num(result.profitFactor)}`);
  }

  // ── Print full report ─────────────────────────────────────────────
  console.log('\n\n' + HR);
  console.log('  DETAILED RESULTS — Each Combination');
  console.log(HR);

  for (const { id, r } of comboResults) {
    printComboSummary(r, id);
    printYearTable(r);
  }

  printComparisonMatrix(comboResults);
  printFinalRecommendation(comboResults);

  console.log('\n' + HR + '\n');
}

main().catch(err => { console.error('Error:', err); process.exit(1); });

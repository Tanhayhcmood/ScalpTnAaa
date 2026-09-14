// Adaptive Trend Engine — EMA structure, slope, momentum and regime.
// The public trend/strength fields remain compatible with existing consumers.

import { OHLCV } from './goldEngine.js';

export interface TrendResult {
  ema50: number;
  ema100: number;
  ema200: number;
  trend: 'BULLISH' | 'BEARISH' | 'NEUTRAL';
  strength: 'STRONG' | 'MODERATE' | 'WEAK';
  ema20: number;
  adx: number;
  slope50: number;
  slope100: number;
  rsi: number;
  score: number;
  quality: number;
  state: 'TRENDING' | 'PULLBACK' | 'DEVELOPING' | 'TRANSITION' | 'RANGE';
  pullback: boolean;
}

function calcEMA(closes: number[], period: number): number {
  if (closes.length < period) return closes[closes.length - 1] ?? 0;
  const k = 2 / (period + 1);
  let ema = closes.slice(0, period).reduce((a, b) => a + b, 0) / period;
  for (let i = period; i < closes.length; i++) ema = closes[i] * k + ema * (1 - k);
  return +ema.toFixed(4);
}

function emaSeries(closes: number[], period: number): number[] {
  if (!closes.length) return [];
  if (closes.length < period) return closes.map(() => closes[closes.length - 1]);
  const values = new Array<number>(closes.length).fill(0);
  let ema = closes.slice(0, period).reduce((a, b) => a + b, 0) / period;
  values[period - 1] = ema;
  const k = 2 / (period + 1);
  for (let i = period; i < closes.length; i++) {
    ema = closes[i] * k + ema * (1 - k);
    values[i] = ema;
  }
  for (let i = 0; i < period - 1; i++) values[i] = values[period - 1];
  return values;
}

function calcATR(candles: OHLCV[], period = 14): number {
  if (candles.length < 2) return 0;
  const trs: number[] = [];
  for (let i = 1; i < candles.length; i++) {
    const c = candles[i], p = candles[i - 1];
    trs.push(Math.max(c.high - c.low, Math.abs(c.high - p.close), Math.abs(c.low - p.close)));
  }
  if (trs.length < period) return trs.reduce((a, b) => a + b, 0) / Math.max(1, trs.length);
  let atr = trs.slice(0, period).reduce((a, b) => a + b, 0) / period;
  for (let i = period; i < trs.length; i++) atr = (atr * (period - 1) + trs[i]) / period;
  return atr;
}

function calcRSI(closes: number[], period = 14): number {
  if (closes.length < period + 1) return 50;
  const gains: number[] = [], losses: number[] = [];
  for (let i = 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1];
    gains.push(Math.max(d, 0));
    losses.push(Math.max(-d, 0));
  }
  let gain = gains.slice(0, period).reduce((a, b) => a + b, 0) / period;
  let loss = losses.slice(0, period).reduce((a, b) => a + b, 0) / period;
  for (let i = period; i < gains.length; i++) {
    gain = (gain * (period - 1) + gains[i]) / period;
    loss = (loss * (period - 1) + losses[i]) / period;
  }
  if (loss === 0) return 100;
  return +(100 - 100 / (1 + gain / loss)).toFixed(2);
}

function calcADX(candles: OHLCV[], period = 14): number {
  if (candles.length < period * 2) return 20;
  const trs: number[] = [], plus: number[] = [], minus: number[] = [];
  for (let i = 1; i < candles.length; i++) {
    const c = candles[i], p = candles[i - 1];
    trs.push(Math.max(c.high - c.low, Math.abs(c.high - p.close), Math.abs(c.low - p.close)));
    const up = c.high - p.high, down = p.low - c.low;
    plus.push(up > down && up > 0 ? up : 0);
    minus.push(down > up && down > 0 ? down : 0);
  }
  let tr = trs.slice(0, period).reduce((a, b) => a + b, 0);
  let dp = plus.slice(0, period).reduce((a, b) => a + b, 0);
  let dm = minus.slice(0, period).reduce((a, b) => a + b, 0);
  const dx: number[] = [];
  for (let i = period; i < trs.length; i++) {
    tr = tr - tr / period + trs[i];
    dp = dp - dp / period + plus[i];
    dm = dm - dm / period + minus[i];
    const diP = tr > 0 ? 100 * dp / tr : 0;
    const diM = tr > 0 ? 100 * dm / tr : 0;
    const total = diP + diM;
    dx.push(total > 0 ? 100 * Math.abs(diP - diM) / total : 0);
  }
  if (dx.length < period) return 20;
  return +(dx.slice(-period).reduce((a, b) => a + b, 0) / period).toFixed(2);
}

function signedComponent(value: number, scale: number, weight: number): number {
  if (scale <= 0) return 0;
  return Math.max(-1, Math.min(1, value / scale)) * weight;
}

export function analyzeTrend(candles: OHLCV[]): TrendResult {
  const closes = candles.map(c => c.close);
  const last = closes[closes.length - 1] ?? 0;
  if (closes.length < 210) {
    return {
      ema20: last, ema50: last, ema100: last, ema200: last,
      trend: 'NEUTRAL', strength: 'WEAK', adx: 0, slope50: 0,
      slope100: 0, rsi: 50, score: 0, quality: 0, state: 'RANGE', pullback: false,
    };
  }

  const atr = Math.max(calcATR(candles), 1e-6);
  const ema20 = calcEMA(closes, 20), ema50 = calcEMA(closes, 50);
  const ema100 = calcEMA(closes, 100), ema200 = calcEMA(closes, 200);
  const s50 = emaSeries(closes, 50), s100 = emaSeries(closes, 100);
  const slope50 = (s50[s50.length - 1] - s50[s50.length - 11]) / atr;
  const slope100 = (s100[s100.length - 1] - s100[s100.length - 11]) / atr;
  const rsi = calcRSI(closes), adx = calcADX(candles);
  const score = Math.max(-100, Math.min(100,
    signedComponent(last - ema50, atr * 1.25, 15) +
    signedComponent(ema20 - ema50, atr, 10) +
    signedComponent(ema50 - ema100, atr * 1.5, 25) +
    signedComponent(ema100 - ema200, atr * 2, 20) +
    signedComponent(slope50, 0.75, 15) +
    signedComponent(slope100, 0.5, 10) +
    signedComponent(rsi - 50, 20, 5)
  ));
  const roundedScore = +score.toFixed(1);
  const quality = +Math.min(100, Math.abs(roundedScore) * 0.8 + Math.max(0, adx - 15) * 1.5).toFixed(1);
  const bullishStructure = ema50 > ema100 && ema100 > ema200;
  const bearishStructure = ema50 < ema100 && ema100 < ema200;
  const bullishCore = last > ema50 && bullishStructure;
  const bearishCore = last < ema50 && bearishStructure;
  const bullishPullback = bullishStructure && slope50 > 0 && slope100 > 0 &&
    last >= ema100 - 1.25 * atr && last <= ema50 + 0.75 * atr && roundedScore >= 8;
  const bearishPullback = bearishStructure && slope50 < 0 && slope100 < 0 &&
    last <= ema100 + 1.25 * atr && last >= ema50 - 0.75 * atr && roundedScore <= -8;

  let trend: TrendResult['trend'] = 'NEUTRAL';
  let pullback = false;
  if ((bullishCore && roundedScore >= 25) || bullishPullback) {
    trend = 'BULLISH'; pullback = !bullishCore;
  } else if ((bearishCore && roundedScore <= -25) || bearishPullback) {
    trend = 'BEARISH'; pullback = !bearishCore;
  }
  const directionScore = Math.abs(roundedScore);
  const strength: TrendResult['strength'] = trend !== 'NEUTRAL' && directionScore >= 55 && adx >= 25
    ? 'STRONG'
    : trend !== 'NEUTRAL' && directionScore >= 30 && adx >= 18 ? 'MODERATE' : 'WEAK';
  const state: TrendResult['state'] = trend !== 'NEUTRAL'
    ? (pullback ? 'PULLBACK' : strength === 'STRONG' ? 'TRENDING' : 'DEVELOPING')
    : Math.abs(roundedScore) >= 20 ? 'TRANSITION' : 'RANGE';

  return {
    ema20, ema50, ema100, ema200, trend, strength, adx,
    slope50: +slope50.toFixed(3), slope100: +slope100.toFixed(3), rsi,
    score: roundedScore, quality, state, pullback,
  };
}

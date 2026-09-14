// Gold price simulation and technical analysis engine

export interface OHLCV {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

// Seeded random number generator for reproducibility
class SeededRandom {
  private seed: number;
  constructor(seed: number) {
    this.seed = seed;
  }
  next(): number {
    this.seed = (this.seed * 1664525 + 1013904223) & 0xffffffff;
    return ((this.seed >>> 0) / 0xffffffff);
  }
  nextGaussian(): number {
    const u1 = this.next();
    const u2 = this.next();
    return Math.sqrt(-2 * Math.log(u1 + 1e-10)) * Math.cos(2 * Math.PI * u2);
  }
}

// Generate realistic gold OHLCV candles using geometric Brownian motion
export function generateCandles(timeframe: 'M1' | 'M5' | 'M15', count: number): OHLCV[] {
  const rng = new SeededRandom(Date.now() % 100000);
  const minuteMap: Record<string, number> = { M1: 1, M5: 5, M15: 15 };
  const minutesPerCandle = minuteMap[timeframe];
  const volatilityMap: Record<string, number> = { M1: 0.0003, M5: 0.0007, M15: 0.0012 };
  const vol = volatilityMap[timeframe];

  const candles: OHLCV[] = [];
  let price = 2650 + rng.next() * 100; // realistic XAU/USD base
  const now = Date.now();

  for (let i = count - 1; i >= 0; i--) {
    const timestamp = new Date(now - i * minutesPerCandle * 60 * 1000);

    // Simulate intraday session effects (more volatile during London/NY overlap)
    const hour = timestamp.getUTCHours();
    const sessionMultiplier = (hour >= 8 && hour <= 17) ? 1.5 : 0.7;

    // GBM price move
    const drift = 0.0001 * (rng.next() - 0.48); // slight upward bias
    const move = price * (drift + vol * sessionMultiplier * rng.nextGaussian());

    const open = price;
    const close = Math.max(1800, Math.min(3200, open + move));
    const range = Math.abs(close - open) * (1 + rng.next() * 2);
    const high = Math.max(open, close) + range * rng.next();
    const low = Math.min(open, close) - range * rng.next();

    candles.push({
      time: timestamp.toISOString(),
      open: +open.toFixed(2),
      high: +high.toFixed(2),
      low: +low.toFixed(2),
      close: +close.toFixed(2),
      volume: Math.floor(500 + rng.next() * 2000),
    });

    price = close;
  }

  return candles;
}

// EMA calculation
export function calcEMA(prices: number[], period: number): number[] {
  const k = 2 / (period + 1);
  const ema: number[] = [];
  for (let i = 0; i < prices.length; i++) {
    if (i < period - 1) {
      ema.push(prices[i]);
    } else if (i === period - 1) {
      ema.push(prices.slice(0, period).reduce((a, b) => a + b, 0) / period);
    } else {
      ema.push(prices[i] * k + ema[i - 1] * (1 - k));
    }
  }
  return ema;
}

// RSI calculation
export function calcRSI(prices: number[], period: number): number[] {
  const rsi: number[] = [];
  for (let i = 0; i < prices.length; i++) {
    if (i < period) {
      rsi.push(50);
      continue;
    }
    // Use period+1 prices to compute period real differences (no zero-padding)
    const slice = prices.slice(i - period, i + 1);
    const changes = slice.slice(1).map((p, j) => p - slice[j]);
    const gains = changes.filter(c => c > 0).reduce((a, b) => a + b, 0) / period;
    const losses = Math.abs(changes.filter(c => c < 0).reduce((a, b) => a + b, 0)) / period;
    if (losses === 0) { rsi.push(100); continue; }
    const rs = gains / losses;
    rsi.push(+(100 - 100 / (1 + rs)).toFixed(2));
  }
  return rsi;
}

// Bollinger Bands
export function calcBB(prices: number[], period: number, dev: number): { upper: number[]; middle: number[]; lower: number[] } {
  const upper: number[] = [], middle: number[] = [], lower: number[] = [];
  for (let i = 0; i < prices.length; i++) {
    if (i < period - 1) {
      upper.push(prices[i]); middle.push(prices[i]); lower.push(prices[i]);
      continue;
    }
    const slice = prices.slice(i - period + 1, i + 1);
    const sma = slice.reduce((a, b) => a + b, 0) / period;
    const std = Math.sqrt(slice.map(p => (p - sma) ** 2).reduce((a, b) => a + b, 0) / period);
    upper.push(+(sma + dev * std).toFixed(2));
    middle.push(+sma.toFixed(2));
    lower.push(+(sma - dev * std).toFixed(2));
  }
  return { upper, middle, lower };
}

// ATR calculation
export function calcATR(candles: OHLCV[], period: number): number[] {
  const tr = candles.map((c, i) => {
    if (i === 0) return c.high - c.low;
    const prev = candles[i - 1];
    return Math.max(c.high - c.low, Math.abs(c.high - prev.close), Math.abs(c.low - prev.close));
  });
  const atr: number[] = [];
  for (let i = 0; i < tr.length; i++) {
    if (i < period - 1) { atr.push(tr[i]); continue; }
    if (i === period - 1) {
      atr.push(tr.slice(0, period).reduce((a, b) => a + b, 0) / period);
    } else {
      atr.push((atr[i - 1] * (period - 1) + tr[i]) / period);
    }
  }
  return atr;
}

export type SignalDirection = 'BUY' | 'SELL' | 'NEUTRAL';

export interface TimeframeSignalData {
  signal: SignalDirection;
  emaSignal: SignalDirection;
  rsiSignal: SignalDirection;
  bbSignal: SignalDirection;
  score: number;
}

export interface IndicatorSnapshot {
  emaFast: number;
  emaSlow: number;
  rsi: number;
  bbUpper: number;
  bbMiddle: number;
  bbLower: number;
  atr: number;
  currentPrice: number;
}

export function analyzeTimeframe(
  candles: OHLCV[],
  cfg: {
    emaFastPeriod: number;
    emaSlowPeriod: number;
    rsiPeriod: number;
    rsiOverbought: number;
    rsiOversold: number;
    bbPeriod: number;
    bbDeviation: number;
    atrPeriod: number;
    minSignalScore: number;
  }
): { signal: TimeframeSignalData; indicators: IndicatorSnapshot } {
  const closes = candles.map(c => c.close);
  const emaFast = calcEMA(closes, cfg.emaFastPeriod);
  const emaSlow = calcEMA(closes, cfg.emaSlowPeriod);
  const rsi = calcRSI(closes, cfg.rsiPeriod);
  const bb = calcBB(closes, cfg.bbPeriod, cfg.bbDeviation);
  const atr = calcATR(candles, cfg.atrPeriod);

  const n = closes.length - 1;
  const price = closes[n];

  // EMA signal
  let emaSignal: SignalDirection = 'NEUTRAL';
  if (emaFast[n] > emaSlow[n] && emaFast[n - 1] <= emaSlow[n - 1]) emaSignal = 'BUY';
  else if (emaFast[n] < emaSlow[n] && emaFast[n - 1] >= emaSlow[n - 1]) emaSignal = 'SELL';
  else if (emaFast[n] > emaSlow[n]) emaSignal = 'BUY';
  else if (emaFast[n] < emaSlow[n]) emaSignal = 'SELL';

  // RSI signal
  let rsiSignal: SignalDirection = 'NEUTRAL';
  if (rsi[n] < cfg.rsiOversold) rsiSignal = 'BUY';
  else if (rsi[n] > cfg.rsiOverbought) rsiSignal = 'SELL';

  // BB signal
  let bbSignal: SignalDirection = 'NEUTRAL';
  if (price <= bb.lower[n]) bbSignal = 'BUY';
  else if (price >= bb.upper[n]) bbSignal = 'SELL';

  // Score: how many signals agree
  const signals = [emaSignal, rsiSignal, bbSignal];
  const buyCount = signals.filter(s => s === 'BUY').length;
  const sellCount = signals.filter(s => s === 'SELL').length;
  const score = Math.max(buyCount, sellCount) / 3;

  // Apply minSignalScore threshold before emitting a directional signal
  let signal: SignalDirection = 'NEUTRAL';
  if (score >= cfg.minSignalScore) {
    if (buyCount >= 2) signal = 'BUY';
    else if (sellCount >= 2) signal = 'SELL';
  }

  return {
    signal: { signal, emaSignal, rsiSignal, bbSignal, score: +score.toFixed(2) },
    indicators: {
      emaFast: +emaFast[n].toFixed(2),
      emaSlow: +emaSlow[n].toFixed(2),
      rsi: +rsi[n].toFixed(2),
      bbUpper: +bb.upper[n].toFixed(2),
      bbMiddle: +bb.middle[n].toFixed(2),
      bbLower: +bb.lower[n].toFixed(2),
      atr: +atr[n].toFixed(2),
      currentPrice: +price.toFixed(2),
    },
  };
}

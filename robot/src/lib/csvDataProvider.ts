// CSV Data Provider — Real Historical XAUUSD Candles
//
// Supported CSV formats (auto-detected):
//
//   Format A — MetaTrader 5 / Dukascopy export:
//     <Date>,<Time>,<Open>,<High>,<Low>,<Close>,<Volume>
//     2021.01.04,00:05,1900.12,1901.45,1899.87,1900.98,523
//
//   Format B — ISO timestamp, no separate time column:
//     timestamp,open,high,low,close,volume
//     2021-01-04T00:05:00.000Z,1900.12,1901.45,1899.87,1900.98,523
//
//   Format C — Unix epoch milliseconds:
//     time,open,high,low,close,volume
//     1609718700000,1900.12,1901.45,1899.87,1900.98,523
//
// Validation applied per candle:
//   ① OHLC integrity: high >= max(open,close) AND low <= min(open,close)
//   ② Positive prices: all OHLC > 0
//   ③ Realistic range: 0 < (high - low) < 200 (excessive spike = corrupt bar)
//   ④ Non-negative volume
//   ⑤ Monotonic timestamps: each bar must be strictly after the previous
//
// Missing candle detection:
//   After sorting, gaps > 3× expected interval are logged as warnings
//   (weekends/holidays are normal; gaps inside a session are suspect).
//
// Any row that fails validation is SKIPPED with a counter — it does not
// abort the load. A load that skips > 5% of rows emits a WARNING.

import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import type { OHLCV } from './goldEngine.js';

// ===== VALIDATION =====

interface ValidationResult {
  valid: boolean;
  reason?: string;
}

function validateCandle(c: OHLCV, prev: OHLCV | null, interval: number): ValidationResult {
  // ① Positive prices
  if (c.open <= 0 || c.high <= 0 || c.low <= 0 || c.close <= 0) {
    return { valid: false, reason: 'non-positive price' };
  }

  // ② OHLC integrity
  const maxOC = Math.max(c.open, c.close);
  const minOC = Math.min(c.open, c.close);
  if (c.high < maxOC - 0.001 || c.low > minOC + 0.001) {
    return { valid: false, reason: `OHLC violation: H=${c.high} L=${c.low} O=${c.open} C=${c.close}` };
  }

  // ③ Realistic range (< $200 per bar on M5 is sane for gold)
  const candleRange = c.high - c.low;
  if (candleRange <= 0 || candleRange > 200) {
    return { valid: false, reason: `unrealistic range: ${candleRange.toFixed(2)}` };
  }

  // ④ Non-negative volume
  if (c.volume < 0) {
    return { valid: false, reason: 'negative volume' };
  }

  // ⑤ Monotonic timestamps
  if (prev) {
    const prevMs = new Date(prev.time).getTime();
    const currMs = new Date(c.time).getTime();
    if (currMs <= prevMs) {
      return { valid: false, reason: `non-monotonic timestamp: ${c.time} <= ${prev.time}` };
    }
  }

  return { valid: true };
}

// ===== CSV FORMAT DETECTION =====

type CsvFormat = 'mt5' | 'iso' | 'unix';

interface ParsedRow {
  timestampMs: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

function detectFormat(headerLine: string): CsvFormat {
  const lower = headerLine.toLowerCase();
  if (lower.includes('time') && /^\d{13}/.test(lower.split(',')[0])) return 'unix';
  if (lower.includes('t') && lower.includes('-') && lower.includes(':')) return 'iso';
  // MT5: date like 2021.01.04 and time like 00:05
  if (/\d{4}\.\d{2}\.\d{2}/.test(headerLine)) return 'mt5';
  return 'iso'; // fallback
}

function parseMt5Date(date: string, time: string): number {
  // 2021.01.04 + 00:05 → ISO
  const iso = date.replace(/\./g, '-') + 'T' + time + ':00.000Z';
  return new Date(iso).getTime();
}

function parseRow(fields: string[], format: CsvFormat, lineNo: number): ParsedRow | null {
  try {
    let timestampMs: number;
    let o: number, h: number, l: number, c: number, v: number;

    if (format === 'mt5') {
      // fields: date, time, open, high, low, close, volume  OR  date, time, open, high, low, close, tickvol, vol, spread
      if (fields.length < 7) return null;
      timestampMs = parseMt5Date(fields[0].trim(), fields[1].trim());
      o = parseFloat(fields[2]); h = parseFloat(fields[3]);
      l = parseFloat(fields[4]); c = parseFloat(fields[5]);
      v = parseFloat(fields[6]);
    } else if (format === 'unix') {
      if (fields.length < 6) return null;
      timestampMs = parseInt(fields[0], 10);
      o = parseFloat(fields[1]); h = parseFloat(fields[2]);
      l = parseFloat(fields[3]); c = parseFloat(fields[4]);
      v = parseFloat(fields[5]);
    } else {
      // ISO: timestamp,open,high,low,close,volume
      if (fields.length < 6) return null;
      timestampMs = new Date(fields[0].trim()).getTime();
      o = parseFloat(fields[1]); h = parseFloat(fields[2]);
      l = parseFloat(fields[3]); c = parseFloat(fields[4]);
      v = parseFloat(fields[5]);
    }

    if (isNaN(timestampMs) || isNaN(o) || isNaN(h) || isNaN(l) || isNaN(c) || isNaN(v)) {
      return null;
    }

    return { timestampMs, open: o, high: h, low: l, close: c, volume: v };
  } catch {
    return null;
  }
}

// ===== MISSING CANDLE DETECTION =====

interface GapReport {
  afterTime: string;
  beforeTime: string;
  gapMinutes: number;
  expectedMinutes: number;
}

function detectGaps(candles: OHLCV[], intervalMs: number): GapReport[] {
  const gaps: GapReport[] = [];
  const thresh = intervalMs * 3; // 3× interval = suspicious gap
  for (let i = 1; i < candles.length; i++) {
    const prevMs = new Date(candles[i - 1].time).getTime();
    const currMs = new Date(candles[i].time).getTime();
    const diff   = currMs - prevMs;
    if (diff > thresh) {
      gaps.push({
        afterTime:       candles[i - 1].time,
        beforeTime:      candles[i].time,
        gapMinutes:      Math.round(diff / 60_000),
        expectedMinutes: Math.round(intervalMs / 60_000),
      });
    }
  }
  return gaps;
}

// ===== CONFIDENCE DISTRIBUTION (runs on loaded candles) =====
// Exported so the runner can use it without importing decisionEngine itself.

export interface ConfidenceDistribution {
  total:   number;
  neutral: number;
  lt40:    number;
  s40_50:  number;
  s50_60:  number;
  s60_70:  number;
  s70_80:  number;
  s80_85:  number;
  gte85:   number;
  maxConf: number;
  maxConfBar: number;
  maxConfComponents: Record<string, number> | null;
  maxConfRegime: string | null;
  maxConfDirection: string | null;
  firstBlockedReason: string | null;
  gateBreakdown: Record<string, number>;
}

// ===== MAIN CLASS =====

export class CsvDataProvider {
  private candles: OHLCV[] = [];
  private loaded = false;

  readonly sourceLabel: string;

  private readonly startMs: number | null;
  private readonly endMs:   number | null;

  public loadStats = {
    totalRows:    0,
    skipped:      0,
    loaded:       0,
    gaps:         [] as GapReport[],
    warnings:     [] as string[],
    intervalMs:   300_000, // default M5
    format:       'unknown' as string,
  };

  constructor(
    private readonly csvPath: string,
    startDate?: string,
    endDate?: string,
  ) {
    this.sourceLabel = `CSV: ${path.basename(csvPath)}`;
    this.startMs = startDate ? new Date(startDate).getTime() : null;
    this.endMs   = endDate   ? new Date(endDate + 'T23:59:59Z').getTime() : null;
  }

  /**
   * Load and parse the CSV file.
   * Must be called before getCandles().
   */
  async load(): Promise<void> {
    if (this.loaded) return;

    if (!fs.existsSync(this.csvPath)) {
      throw new Error(`CsvDataProvider: file not found: ${this.csvPath}`);
    }

    const raw: ParsedRow[] = [];
    let format: CsvFormat = 'iso';
    let headerSkipped  = false;
    let formatDetected = false;

    const rl = readline.createInterface({
      input: fs.createReadStream(this.csvPath, { encoding: 'utf8' }),
      crlfDelay: Infinity,
    });

    for await (const line of rl) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#')) continue;

      // First content line: decide whether it is a header row.
      // We must check this BEFORE format detection so that format is always
      // derived from the first DATA line, not from a text header.  Detecting
      // format from a header such as "<DATE>,<TIME>,<Open>,..." returns 'iso'
      // (the fallback), causing every subsequent MT5-format data row to fail.
      if (!headerSkipped) {
        const firstField = trimmed.split(',')[0].trim();
        if (isNaN(Number(firstField)) && !firstField.match(/^\d{4}[\.\-]/)) {
          // First field is text → this is a header row; skip it and detect
          // format from the first actual data line instead.
          headerSkipped = true;
          continue;
        }
        headerSkipped = true;
      }

      // Detect format from the first data line (header already consumed above).
      if (!formatDetected) {
        format = detectFormat(trimmed);
        this.loadStats.format = format;
        formatDetected = true;
      }

      this.loadStats.totalRows++;
      const fields = trimmed.split(',');
      const parsed = parseRow(fields, format, this.loadStats.totalRows);

      if (!parsed) {
        this.loadStats.skipped++;
        continue;
      }

      // Date range filter
      if (this.startMs && parsed.timestampMs < this.startMs) continue;
      if (this.endMs   && parsed.timestampMs > this.endMs)   continue;

      raw.push(parsed);
    }

    // Sort by timestamp (some exports are unordered)
    raw.sort((a, b) => a.timestampMs - b.timestampMs);

    // Detect interval from first 10 rows
    if (raw.length >= 2) {
      const diffs: number[] = [];
      for (let i = 1; i < Math.min(10, raw.length); i++) {
        diffs.push(raw[i].timestampMs - raw[i - 1].timestampMs);
      }
      diffs.sort((a, b) => a - b);
      this.loadStats.intervalMs = diffs[Math.floor(diffs.length / 2)]; // median
    }

    // Convert to OHLCV and validate
    let prev: OHLCV | null = null;
    for (const row of raw) {
      const candle: OHLCV = {
        time:   new Date(row.timestampMs).toISOString(),
        open:   +row.open.toFixed(2),
        high:   +row.high.toFixed(2),
        low:    +row.low.toFixed(2),
        close:  +row.close.toFixed(2),
        volume: Math.max(0, Math.round(row.volume)),
      };

      const vr = validateCandle(candle, prev, this.loadStats.intervalMs);
      if (!vr.valid) {
        this.loadStats.skipped++;
        continue;
      }

      this.candles.push(candle);
      this.loadStats.loaded++;
      prev = candle;
    }

    // Gap detection
    this.loadStats.gaps = detectGaps(this.candles, this.loadStats.intervalMs);
    // Weekend = Friday 21:00 UTC → Sunday 22:00 UTC ≈ 2700–3000 min
    // Flag only gaps that look like missing bars inside a trading day (< 2000 min)
    const tradingGaps = this.loadStats.gaps.filter(g => g.gapMinutes < 2000);

    if (tradingGaps.length > 0) {
      this.loadStats.warnings.push(
        `${tradingGaps.length} intra-session gap(s) detected (< 2000 min, not weekend). May indicate missing bars.`,
      );
    }

    // High skip-rate warning
    if (this.loadStats.totalRows > 0) {
      const skipRate = this.loadStats.skipped / this.loadStats.totalRows;
      if (skipRate > 0.05) {
        this.loadStats.warnings.push(
          `High skip rate: ${(skipRate * 100).toFixed(1)}% of rows were invalid.`,
        );
      }
    }

    if (this.candles.length === 0) {
      throw new Error(`CsvDataProvider: no valid candles loaded from ${this.csvPath}`);
    }

    this.loaded = true;
  }

  getCandles(timeframe: 'M5' | 'M15', count: number): OHLCV[] {
    if (!this.loaded) {
      throw new Error('CsvDataProvider: call load() before getCandles()');
    }
    // Return the last `count` candles (most recent)
    if (this.candles.length <= count) return [...this.candles];
    return this.candles.slice(this.candles.length - count);
  }

  /** Return all loaded candles regardless of count (for full backtest scan). */
  getAllCandles(): OHLCV[] {
    if (!this.loaded) throw new Error('CsvDataProvider: call load() before getAllCandles()');
    return [...this.candles];
  }

  printLoadSummary(): void {
    const s = this.loadStats;
    console.log(`  File    : ${this.csvPath}`);
    console.log(`  Format  : ${s.format}`);
    console.log(`  Rows    : ${s.totalRows} total, ${s.loaded} loaded, ${s.skipped} skipped`);
    console.log(`  Interval: ${s.intervalMs / 60_000} min`);
    console.log(`  Range   : ${this.candles[0]?.time ?? '—'} → ${this.candles[this.candles.length - 1]?.time ?? '—'}`);
    console.log(`  Candles : ${this.candles.length}`);
    if (s.gaps.length > 0) {
      const shown = s.gaps.slice(0, 5);
      console.log(`  Gaps    : ${s.gaps.length} total (first 5):`);
      for (const g of shown) {
        console.log(`    ${g.afterTime} → ${g.beforeTime} (${g.gapMinutes} min)`);
      }
    }
    for (const w of s.warnings) {
      console.log(`  WARNING : ${w}`);
    }
  }
}

// Data Provider Interface — Pluggable Candle Source
//
// Decouples the backtest engine from the data source.
// Two implementations:
//   SyntheticDataProvider — wraps generateCandles() (GBM)
//   CsvDataProvider       — loads real XAUUSD history from CSV
//
// Both return exactly the same OHLCV interface so backtestEngine
// never needs to know the data source.

import { OHLCV, generateCandles } from './goldEngine.js';

// ===== INTERFACE =====

export interface DataProvider {
  /**
   * Return an array of OHLCV candles for the requested period.
   *
   * @param timeframe  'M5' | 'M15'
   * @param count      Number of candles needed (including warmup).
   *                   Provider MUST return at least this many candles.
   * @returns          Candles sorted oldest → newest.
   */
  getCandles(timeframe: 'M5' | 'M15', count: number): OHLCV[];

  /** Human-readable source label used in reports. */
  readonly sourceLabel: string;
}

// ===== CONFIG FLAG =====

export type DataSourceMode = 'synthetic' | 'csv';

export interface DataProviderConfig {
  mode: DataSourceMode;
  /** Path to CSV file — required when mode === 'csv'. */
  csvPath?: string;
  /**
   * Optional date range filter when mode === 'csv'.
   * ISO 8601 strings, e.g. '2021-01-01' / '2021-12-31'.
   */
  startDate?: string;
  endDate?: string;
}

// ===== SYNTHETIC PROVIDER =====

export class SyntheticDataProvider implements DataProvider {
  readonly sourceLabel = 'Synthetic GBM (generateCandles)';

  getCandles(timeframe: 'M5' | 'M15', count: number): OHLCV[] {
    return generateCandles(timeframe, count);
  }
}

// ===== FACTORY =====

/**
 * Build the correct provider from config.
 * Import CsvDataProvider lazily to avoid loading 'fs' in non-CSV builds.
 */
export async function buildDataProvider(cfg: DataProviderConfig): Promise<DataProvider> {
  if (cfg.mode === 'synthetic') {
    return new SyntheticDataProvider();
  }

  if (!cfg.csvPath) {
    throw new Error("DataProviderConfig: csvPath is required when mode === 'csv'");
  }

  // Dynamic import keeps the CSV module out of tree-shake
  const { CsvDataProvider } = await import('./csvDataProvider.js');
  const provider = new CsvDataProvider(cfg.csvPath, cfg.startDate, cfg.endDate);
  await provider.load();
  return provider;
}

export type { OHLCV };

// Entry Filter — Minimum 3 Independent Confirmations
// Optimized for XAUUSD scalping on M1 and M5 timeframes.
//
// A trade may only open when at least 3 of the 4 independent systems
// confirm the SAME direction:
//
//   ① SMC    — Break of Structure / CHoCH / Order Block / Sweep  (primary)
//   ② Trend  — EMA 50/100/200 alignment                          (directional bias)
//   ③ PA     — Price Action patterns and level context            (entry timing)
//   ④ Wyckoff — Phase + Spring/Upthrust + volume confirmation     (institutional intent)
//
// Each system casts exactly ONE vote (BUY | SELL | NEUTRAL).
// Votes are counted independently — no weighting here.
// If fewer than MIN_CONFIRMATIONS agree on the same direction, the trade
// is blocked and smcSignal is forced to NEUTRAL by the caller.
//
// Design principles:
//   • SMC is the PRIMARY trigger — it always contributes one vote,
//     but cannot win alone.  A NEUTRAL SMC signal means 0 votes total.
//   • All votes must point to the SAME direction — a split vote
//     (e.g. 2 BUY + 2 SELL) is treated as insufficient evidence.
//   • The filter is a binary gate: it does not modify scores, only
//     allows or blocks the trade.  Score adjustment is handled upstream.

export const MIN_CONFIRMATIONS = 3; // out of 4 systems — default value

// ===== CONFIGURABLE MIN_CONFIRMATIONS =====
// Pass minConfirmations as a 5th argument to override the default of 3.
// Allowed values: 2 or 3.  Lower value = more trades, less confirmation.
// Test A: minConfirmations=3 (default)
// Test B: minConfirmations=2 (relaxed)
export const MIN_CONFIRMATIONS_OPTIONS = [2, 3] as const;

// ===== OUTPUT =====

export interface EntryFilterResult {
  allowed:          boolean;                // true → trade may open
  direction:        'BUY' | 'SELL' | 'NEUTRAL';
  confirmationCount: number;               // how many systems agree (0–4)
  confirmations: {
    smc:        boolean;
    trend:      boolean;
    priceAction: boolean;
    wyckoff:    boolean;
  };
}

// ===== MAIN ENTRY POINT =====
//
// Parameters:
//   smcSignal    — output of computeSmcSignal after the EMA gate
//   emaTrend     — 'BULLISH' | 'BEARISH' | 'NEUTRAL' from analyzeTrend
//   paSignal     — 'BUY' | 'SELL' | 'NEUTRAL' from analyzePriceAction
//   wyckoffSignal— 'BUY' | 'SELL' | 'NEUTRAL' from analyzeWyckoff
//
// Mapping convention (Trend uses different vocabulary):
//   BULLISH → BUY,  BEARISH → SELL,  NEUTRAL → NEUTRAL

export function applyEntryFilter(
  smcSignal:        'BUY' | 'SELL' | 'NEUTRAL',
  emaTrend:         'BULLISH' | 'BEARISH' | 'NEUTRAL',
  paSignal:         'BUY' | 'SELL' | 'NEUTRAL',
  wyckoffSignal:    'BUY' | 'SELL' | 'NEUTRAL',
  minConfirmations: number = MIN_CONFIRMATIONS,
): EntryFilterResult {

  const blocked: EntryFilterResult = {
    allowed:          false,
    direction:        'NEUTRAL',
    confirmationCount: 0,
    confirmations: { smc: false, trend: false, priceAction: false, wyckoff: false },
  };

  // SMC must be non-NEUTRAL — it is the required primary trigger
  if (smcSignal === 'NEUTRAL') return blocked;

  const direction = smcSignal; // BUY or SELL

  // Map EMA vocabulary → signal vocabulary
  const trendVote: 'BUY' | 'SELL' | 'NEUTRAL' =
    emaTrend === 'BULLISH' ? 'BUY' :
    emaTrend === 'BEARISH' ? 'SELL' : 'NEUTRAL';

  // Count confirmations — each system votes independently
  const smcVote      = true;                        // SMC always votes when non-NEUTRAL
  const trendConfirm = trendVote     === direction;
  const paConfirm    = paSignal      === direction;
  const wycConfirm   = wyckoffSignal === direction;

  const count =
    (smcVote      ? 1 : 0) +
    (trendConfirm ? 1 : 0) +
    (paConfirm    ? 1 : 0) +
    (wycConfirm   ? 1 : 0);

  const allowed = count >= minConfirmations;

  return {
    allowed,
    direction: allowed ? direction : 'NEUTRAL',
    confirmationCount: count,
    confirmations: {
      smc:         smcVote,
      trend:       trendConfirm,
      priceAction: paConfirm,
      wyckoff:     wycConfirm,
    },
  };
}

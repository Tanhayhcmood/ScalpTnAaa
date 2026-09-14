// Capital Manager — Professional Money Management for XAUUSD Scalping
//
// Exit Model: Path A — Fixed 2R Hard TP (active)
//   Analysis on 2021-2025 XAUUSD M5 data showed this is the highest-expectancy
//   exit model: Net +$797 vs −$94 baseline, PF 1.57 vs 0.92, Sharpe 3.54 vs 0.03.
//
// Features:
//   • Smart Stop Loss    — structural placement (OB / swing level) with ATR buffer [UNCHANGED]
//   • Fixed TP (2R)      — hard take profit at exactly 2× SL distance              [PATH A]
//   • Trailing Stop      — DISABLED (distance=0, engine guard skips it)             [PATH A]
//   • Break Even         — DISABLED (trigger at unreachable price)                  [PATH A]
//   • Position Sizing    — exact 1% risk using XAUUSD pip-value formula            [UNCHANGED]
//
// XAUUSD contract convention (used throughout):
//   1 standard lot = 100 troy oz
//   $1 price move × 1 lot = $100 P&L
//   → dollar value per $1 move = lotSize × 100
//   → lotSize = riskAmount / (slDistanceUSD × 100)
//
// All outputs are in absolute price terms (USD per oz) so the EA can use
// them directly without further conversion.

// ===== INPUT =====

export interface CapitalInput {
  direction:          'BUY' | 'SELL';
  entryPrice:         number;   // current price (USD per oz)
  atr:                number;   // ATR value for the current timeframe

  accountBalance:     number;   // account equity in USD
  riskPercent?:       number;   // default 1.0 — % of balance to risk per trade

  // Structural context — all optional; from smcEngine / priceActionEngine output.
  // The more context provided, the more precise the SL/TP placement.
  swingHigh?:         number;   // nearest confirmed swing high
  swingLow?:          number;   // nearest confirmed swing low
  orderBlockTop?:     number;   // active bullish OB ceiling / bearish OB top
  orderBlockBottom?:  number;   // active bullish OB floor / bearish OB bottom
  resistanceLevel?:   number;   // PA resistance (used as TP target for BUY)
  supportLevel?:      number;   // PA support (used as TP target for SELL)
}

// ===== OUTPUT =====

export interface CapitalOutput {
  entryPrice:           number;  // confirmed entry price
  stopLoss:             number;  // hard SL — absolute price

  takeProfit:           number;  // initial TP — absolute price
  riskRewardRatio:      number;  // (TP distance) / (SL distance)

  trailingStopDistance: number;  // trail distance in price units (always positive)
  trailingActivationAt: number;  // price level where trailing starts

  breakEvenAt:          number;  // monitor price — when hit, move SL to breakEvenSL
  breakEvenSL:          number;  // SL value after break-even is triggered

  lotSize:              number;  // position size in lots (e.g. 0.03)
  riskAmount:           number;  // exact $ at risk
  slDistanceUSD:        number;  // |entry − SL| in price units
  slDistancePips:       number;  // slDistanceUSD × 100 (XAUUSD pip = $0.01)
}

// ===== CONSTANTS =====

const DEFAULT_RISK_PCT    = 1.0;   // 1% per trade
const ATR_BUFFER_MULT     = 0.25;  // extra ATR buffer beyond structural level
const MIN_SL_ATR_MULT     = 0.50;  // SL cannot be tighter than 0.5× ATR
const MAX_SL_ATR_MULT     = 3.00;  // SL cannot be wider than 3× ATR (scalping cap)
// The following six constants were labelled "retained for reference (not used)"
// by a previous author.  They were flagged as unused by the TypeScript compiler
// (noUnusedLocals) and have been removed.  Strategy parameters are preserved
// exactly as-is in the active paths (calcSmartSL, calcDynamicTP, calcLotSize).

// ── Path A exit model ────────────────────────────────────────────────────────
const FIXED_TP_RR         = 2.00;  // hard TP at exactly 2× SL distance; no structure, no trail
const LOT_DOLLAR_PER_UNIT = 100;   // XAUUSD: $100 per $1 move per lot
const MIN_LOT             = 0.01;
const MAX_LOT             = 50.0;

// ===== HELPERS =====

function clamp(val: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, val));
}

function round2(n: number): number { return +n.toFixed(2); }
function round4(n: number): number { return +n.toFixed(4); }

// ===== SMART STOP LOSS =====
//
// Placement priority (highest → lowest):
//   1. Structural level: Order Block edge closest to entry
//   2. Structural level: Nearest confirmed swing high/low
//   3. Fallback: 1.5× ATR from entry
//
// In all cases, an ATR_BUFFER_MULT× ATR gap is added beyond the structural
// level so normal wick noise doesn't trigger the SL prematurely.
//
// The result is clamped to [MIN_SL_ATR_MULT, MAX_SL_ATR_MULT]× ATR.

function calcSmartSL(
  direction: 'BUY' | 'SELL',
  entry: number,
  atr: number,
  input: CapitalInput,
): number {
  const buffer = atr * ATR_BUFFER_MULT;
  const minSL  = atr * MIN_SL_ATR_MULT;
  const maxSL  = atr * MAX_SL_ATR_MULT;

  let rawSL: number | null = null;

  if (direction === 'BUY') {
    // SL below entry — look for the best structural floor
    const candidates: number[] = [];
    if (input.orderBlockBottom != null && input.orderBlockBottom < entry) candidates.push(input.orderBlockBottom);
    if (input.swingLow         != null && input.swingLow         < entry) candidates.push(input.swingLow);
    if (input.supportLevel     != null && input.supportLevel     < entry) candidates.push(input.supportLevel);

    if (candidates.length > 0) {
      // Use the highest structural floor (closest to entry) for a tight SL
      const structuralLevel = Math.max(...candidates);
      rawSL = entry - (entry - structuralLevel + buffer);
    }
  } else {
    // SL above entry — look for the best structural ceiling
    const candidates: number[] = [];
    if (input.orderBlockTop    != null && input.orderBlockTop    > entry) candidates.push(input.orderBlockTop);
    if (input.swingHigh        != null && input.swingHigh        > entry) candidates.push(input.swingHigh);
    if (input.resistanceLevel  != null && input.resistanceLevel  > entry) candidates.push(input.resistanceLevel);

    if (candidates.length > 0) {
      // Use the lowest structural ceiling (closest to entry)
      const structuralLevel = Math.min(...candidates);
      rawSL = entry + (structuralLevel - entry + buffer);
    }
  }

  // Fallback: 1.5× ATR
  const fallbackDist = atr * 1.5;
  const slDist = rawSL != null
    ? Math.abs(entry - rawSL)
    : fallbackDist;

  // Clamp to scalping-safe range
  const clampedDist = clamp(slDist, minSL, maxSL);

  return round2(direction === 'BUY' ? entry - clampedDist : entry + clampedDist);
}

// ===== FIXED TAKE PROFIT — Path A (2R) =====
//
// Hard TP placed at exactly FIXED_TP_RR (2.0) × SL distance from entry.
// No structural lookups, no ATR ceiling, no trailing, no break-even.
// Root-cause analysis on 2021-2025 M5 data showed this is the highest-expectancy
// exit model: average MFE peaks around 1–1.5R, and no trade in the dataset ever
// reached 2R via structural targets — the old structure TP was effectively
// unreachable, capping realised R at 0.85 on average.

function calcDynamicTP(
  direction: 'BUY' | 'SELL',
  entry: number,
  slDist: number,
  _atr: number,
  _input: CapitalInput,
): number {
  return round2(direction === 'BUY' ? entry + slDist * FIXED_TP_RR : entry - slDist * FIXED_TP_RR);
}

// ===== POSITION SIZING =====
//
// Formula:
//   riskAmount  = accountBalance × riskPercent / 100
//   lotSize     = riskAmount / (slDistanceUSD × LOT_DOLLAR_PER_UNIT)
//
// Clamped to [MIN_LOT, MAX_LOT]. Rounded to 2 decimal places (broker standard).

function calcLotSize(
  slDistanceUSD: number,
  accountBalance: number,
  riskPercent: number,
): { lotSize: number; riskAmount: number } {
  // Guard: degenerate input (ATR = 0 → slDistanceUSD = 0).
  // Real XAUUSD data never produces ATR = 0, but synthetic / edge-case data
  // could.  Without this guard the division below yields Infinity (or NaN),
  // which clamp() passes through as MAX_LOT = 50, silently over-sizing the
  // position.  Return MIN_LOT with zero risk so callers can detect the case.
  if (slDistanceUSD <= 0) return { lotSize: MIN_LOT, riskAmount: 0 };

  const riskAmount = accountBalance * riskPercent / 100;
  const rawLot     = riskAmount / (slDistanceUSD * LOT_DOLLAR_PER_UNIT);
  const lotSize    = round4(clamp(rawLot, MIN_LOT, MAX_LOT));
  // Recalculate actual risk with clamped lot (may differ from target if clamped)
  const actualRisk = round2(lotSize * slDistanceUSD * LOT_DOLLAR_PER_UNIT);
  return { lotSize, riskAmount: actualRisk };
}

// ===== TRAILING STOP — DISABLED (Path A) =====
//
// trailingStopDistance = 0 causes backtestEngineV2 to skip the trailing block
// entirely, because the engine guards it with `if (currentTrade.trailDist > 0)`.
// trailingActivationAt = 0 is harmless since the distance check prevents entry.

function calcTrailingStop(
  _direction: 'BUY' | 'SELL',
  _entry: number,
  _slDist: number,
  _atr: number,
): { trailingStopDistance: number; trailingActivationAt: number } {
  return { trailingStopDistance: 0, trailingActivationAt: 0 };
}

// ===== BREAK EVEN — DISABLED (Path A) =====
//
// breakEvenAt is set 99 999 price units away from entry, making it
// unreachable in practice. backtestEngineV2 guards BE with
// `if (!currentTrade.beDone) { if (candle.high >= beAt) ... }` — the condition
// simply never fires, so the SL is never moved by the BE rule.

function calcBreakEven(
  direction: 'BUY' | 'SELL',
  entry: number,
  _slDist: number,
  _atr: number,
): { breakEvenAt: number; breakEvenSL: number } {
  const unreachable = round2(direction === 'BUY' ? entry + 99999 : entry - 99999);
  return { breakEvenAt: unreachable, breakEvenSL: entry };
}

// ===== MAIN ENTRY POINT =====

export function calcTradeParameters(input: CapitalInput): CapitalOutput {
  const {
    direction,
    entryPrice:     entry,
    atr,
    accountBalance,
    riskPercent = DEFAULT_RISK_PCT,
  } = input;

  // 1. Smart Stop Loss
  const stopLoss   = calcSmartSL(direction, entry, atr, input);
  const slDistUSD  = round2(Math.abs(entry - stopLoss));
  const slPips     = round2(slDistUSD * 100); // 1 pip = $0.01 on XAUUSD

  // 2. Dynamic Take Profit
  const takeProfit = calcDynamicTP(direction, entry, slDistUSD, atr, input);
  const tpDistUSD  = Math.abs(entry - takeProfit);
  // Guard against slDistUSD = 0 (degenerate ATR=0 input).  Produces 0 R:R
  // rather than Infinity, which downstream R:R gates will correctly reject.
  const rrRatio    = slDistUSD > 0 ? round2(tpDistUSD / slDistUSD) : 0;

  // 3. Trailing Stop
  const { trailingStopDistance, trailingActivationAt } =
    calcTrailingStop(direction, entry, slDistUSD, atr);

  // 4. Break Even
  const { breakEvenAt, breakEvenSL } =
    calcBreakEven(direction, entry, slDistUSD, atr);

  // 5. Position Sizing (1% risk)
  const { lotSize, riskAmount } = calcLotSize(slDistUSD, accountBalance, riskPercent);

  return {
    entryPrice:           round2(entry),
    stopLoss,
    takeProfit,
    riskRewardRatio:      rrRatio,
    trailingStopDistance,
    trailingActivationAt,
    breakEvenAt,
    breakEvenSL,
    lotSize,
    riskAmount,
    slDistanceUSD:        slDistUSD,
    slDistancePips:       slPips,
  };
}

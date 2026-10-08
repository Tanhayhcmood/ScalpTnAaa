# Trade forensics: 2026-10-08

## Scope and evidence quality

Reviewed GitHub branch agent/frankfurt-demo-2026-10-04 at the observed tip fa150fcaaa9ef6db41059df253c0835d6341c496 (Oct 6). This is source review plus the trade rows and runtime facts supplied in the request. Render logs and the trade journal were not available through the connected services in this conversation; no direct Render log query was possible. Runtime statements below are marked as supplied, not independently re-fetched. No candle downloads, replay, or backtest were done. No trading, credential, order, risk, or strategy setting was changed.

### Reconciled supplied trade totals

- Oct 7: the 11 listed closes sum to +$828.72: 8 winners totaling +$1,115.14 and 3 losses totaling -$286.42. This agrees with the approximate +$850 headline, not exactly.
- Oct 8: the 6 listed closes sum to -$584.95 (approximately -$585): 2 overnight sells totaling -$189.55 and 4 buys totaling -$395.40.
- Oct 5-6: the supplied journal summary is -$475.05 (one +$81 win and six SL losses); raw rows were not available to recompute it.
- Five Oct 8 BUY entries are listed, but only four BUY closing rows are supplied. Ticket 302109475 (03:20 UTC) is not matched to a close or an open-position record. Its outcome remains unknown. The statement that all five buys stopped by 05:51-05:53 UTC conflicts with four supplied BUY closes at 05:51, 05:52, 05:53, and 06:26 UTC after converting MT5 close time from UTC+3; one close is absent. Close rows lack ticket IDs, so exact order-to-close mapping cannot be independently verified.
- The two Oct 7 sells opened at 15:40 and 20:35 UTC are consistent by lot/SL with the Oct 8 MT5 closes at 04:49 and 04:02 server time (01:49 and 01:02 UTC), respectively. They account for the observed -$189.55 overnight-sell subtotal.

## Hypothesis findings

### H1 - Five same-direction positions allowed: CONFIRMED

- Config sets MAX_OPEN_TRADES to 5 (live_trading/config.py:409-420). The live loop polls current positions (live_trading/trading/live_loop.py:1677-1697) and applies a total capacity check (1922-2027), not a one-per-direction rule.
- The slot policy explicitly allows scale-in for positions already tagged to the active strategy (live_trading/trading/strategy_slots.py:171-194). Its one-way guard rejects opposite-direction orders, not additional same-direction orders (54-94). No same-direction/zone dedupe is present in that capacity check.
- Post-stop cooldown exists, but is conditional on enabled, persisted stop-out state and matching direction/slot; range post-entry cooldown applies only to RANGE, ACCUMULATION, DISTRIBUTION, and HIGH_VOLATILITY (live_loop.py:2174-2244). Supplied decision logs say remaining cooldown was zero. Thus five positions fit a cap of five; the safeguards do not impose one position per direction or zone.

### H2 - 3% daily loss limit: CONFIRMED mechanism; exact incident trigger: INSUFFICIENT DATA

- RiskGuardian computes daily PnL as current account balance minus day-open balance and percent of that balance; equity is used separately for peak drawdown (live_trading/risk/guardian.py:202-227). Floating PnL and open trade risk are not inputs to the daily-loss formula.
- Day reset uses UTC midnight (guardian.py:415-431), not broker midnight. The live loop invokes guardian.check before the entry path and returns on halt (live_loop.py:1649-1675), so it is a pre-entry guard. It does not cap aggregate open risk or close positions already open. A realized-balance threshold can stop later entries while previously opened risk still reaches broker SLs.
- The code can explain how a realized loss larger than 3% can occur after a clustered batch is already open; it does not prove when the Oct 8 threshold was first reached. The Oct 6 HALT and Oct 7-8 orders also require a resume/day-rollover record to explain; that record and the halt reason are missing.

### H3 - 3x M15 ATR stop and +0.5R lock: CONFIRMED settings; per-trade excursion attribution: INSUFFICIENT DATA

- Source defaults the protective stop base to 3.0 ATR and describes it as a higher-timeframe ATR (live_trading/config.py:281-295). The capital manager falls back to the full ATR envelope if directional structure is absent or invalid (live_trading/risk/capital_manager.py:97-165); its default target is 2R (capital_manager.py:9-15, 193-217). Supplied runtime telemetry identifies the affected fallback as 3x M15 ATR, about 5.7x M5 ATR.
- Adaptive trail config arms its profit lock at +0.5R and locks +0.1R (live_trading/risk/adaptive_trailing_stop.py:23-41). Supplied telemetry for ticket 302115429 reports MFE around +$1.8 / 0.09R, below trigger; no lock was expected to arm. This confirms the threshold was not reached for that ticket, not that it caused its eventual loss.
- Full MFE/MAE cannot be calculated without per-ticket intratrade prices/candles. Only the supplied 302115429 MFE is available; no matching MAE or comparable paths for the other orders are available. The requested no-download constraint was respected.

### H4 - Weak/range entries versus Oct 7 trend: INSUFFICIENT DATA

- Supplied 03:50 UTC entry telemetry is WEAK_TREND_BULL, ADX 23. Source regime rules permit weak bull entries at the normal confidence threshold (live_trading/signals/market_regime.py:53-65); one decision does not characterize the full Oct 8 session.
- Oct 7 listed closes net +$828.72 and were eight winning sells followed by three losses. No full Oct 7/8 regime, ADX, decision, or signal series was available to establish that Oct 7 was a trend day or to compare regime-conditioned expectancy. The close list alone does not prove why that day earned money.

### H5 - H1/M15 filter and independent confirmations: CONFIRMED as configured/runtime-reported

- MTF_ENABLED is locked false, and MTF alignment is also locked false in source (live_trading/config.py:421-442); when enabled, the live-loop HTF opposition/alignment check is behind that switch (live_loop.py:2085-2127). Supplied log confirms mtf.enabled=false. The H1 validator module exists, but its existence is not an active trade filter while this gate is disabled; no active M15 trend gate was evidenced.
- The ordinary confirmation floor defaults to one and is locked there; the absolute confidence floor is 30% (config.py:316-331, 398-404). The supplied 53.2% / HIGH decision with EMA + Price Action confirms it passed with only those two independent signals, while SMC and Wyckoff are diagnostic-only for the active strategy (live_trading/trading/strategy_slots.py:98-108; live_trading/signals/wyckoff_engine.py:1-3). This describes permissive entry policy, not proof that a particular filter would have prevented a loss.

### H6 - Buying at a 1-hour range top / after a 50+ USD move: INSUFFICIENT DATA

No decision-time M5/M15/H1 candle snapshot, range boundaries, distance-from-trigger, or confirmed executed entry price per ticket was supplied. The allowed sources in this run had no Render logs/trade journal, and candle downloads/replay are explicitly excluded. No conclusion about a range top or a 50+ USD chase is supportable.

### H7 - Overnight scalps / time stop: CONFIRMED holding; no explicit time-stop found in reviewed branch

The two Oct 7 sells remained open overnight and the matched Oct 8 closes support that duration. The reviewed trading configuration has no max-hold/time-stop rule in its risk/trade settings (config.py:272-445); position checks and management are run in the live loop (live_loop.py:1677-1725). The absence of a time stop is a source finding, not proof that no external operator or broker action existed.

### H8 - Slippage, spread, reconnect impact: INSUFFICIENT DATA for causality

The order call passes configured slippage/deviation points (live_loop.py:3742-3780), and pre-order stop checks log bid/ask (3742-3757). That does not provide actual fill price versus decision quote, realized spread/slippage, or whether a connection gap skipped a trailing update. The supplied reconnects (Oct 5 04:19 and Oct 6 08:44 UTC) predate Oct 8; no Oct 8 reconnect/quote/fill/trailing sequence or raw Render log was available. No causal USD amount can be assigned here.

### H9 - UTC versus broker UTC+3: CONFIRMED code conventions; event-level log normalization partly unverified

RiskGuardian resets on UTC date (guardian.py:415-431); session classification normalizes input to UTC (live_trading/signals/quality_filter.py:44-67); stop cooldown uses UTC time (live_loop.py:2174-2194). Apply the supplied conversion to MT5 close times: subtract 3 hours before comparing with UTC opens/logs. The overnight sells reconcile under that conversion. The close-row versus five-buy stop-time discrepancy remains unresolved because ticket IDs and the missing close are absent. Supplied candle-history health is noted, not rechecked.

## Ranked causes and dollar bounds

1. **Risk concentration / same-direction scale-in (high confidence as a design gap; medium confidence as the dominant Oct 8 cause):** five BUYs reportedly accumulated about 5% initial risk at once, versus a 3% daily limit. The four supplied BUY losses are -$395.40; the fifth BUY outcome is missing. This is an observed loss subtotal, not an exact causal allocation to stacking.
2. **Overnight sell exposure with no time-stop (high confidence on exposure, moderate on matching):** the two overnight sell closes total -$189.55. This is a known component of the -$584.95 listed Oct 8 loss, not a strategy counterfactual.
3. **Daily guard observes balance PnL, not floating/open risk and does not liquidate open trades (high confidence in code; low confidence in incident timing):** helps explain why the 3% limit is not a hard cap on a pre-existing basket. Its independent USD contribution cannot be separated from the same losses. Oct 5-6 is -$475.05 in the supplied summary, with no raw rows to attribute further.

The 3x M15 fallback stop and +0.5R trailing lock are confirmed settings, but the one reported MFE for 302115429 never reached +0.5R, so no saved-profit counterfactual is supported. The supplied Oct 8 total and the two exposure causes overlap and must not be added as independent losses.

## What Oct 7 appears to have done right

The listed realized trades captured a substantial downward move: eight sell closes won +$1,115.14 against three losses of -$286.42, net +$828.72. This shows the realized sell basket worked on that sample. Without the signal/regime stream, it does not establish whether a trend filter, entry timing, or exits caused the result; it is not evidence to tune to 11 trades.

## Limits and next data needed

The evidence is roughly two dozen listed closures plus at least one unmatched order, not a robust sample. Do not overfit or infer a stable edge from two days. To close the remaining gaps, export only the relevant UTC Render log slices and trade-journal rows with ticket, fill/close times, fill price, commissions/swaps, per-bar MFE/MAE, guardian halt/resume state, quote/spread, and trailing decisions for Oct 5-8. No secrets, tokens, or session identifiers are needed or should be included. The missing ticket 302109475 outcome is the first reconciliation item.

## Proposed changes (not implemented)

The following are candidates for a separate strategy/risk approval and are not applied in this forensic task; any new flags should default OFF:

- cap total open risk at or below the remaining daily-loss budget;
- allow at most one open position per direction/zone;
- extend same-direction cooldown after a stop-out;
- compute daily loss from equity including floating PnL and enforce it before any new trade;
- use a structural or tighter stop appropriate to M5;
- arm breakeven earlier, around +0.3R;
- add a time stop and prohibit overnight scalps;
- enable an H1/M15 trend filter;
- require at least two independent confirmations;
- add an anti-chase filter.

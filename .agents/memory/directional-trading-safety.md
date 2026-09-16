---
name: Directional trading safety
description: The live gold scalper's one-way position policy and why opposite entries are unsafe.
---

The gold scalping strategy is directional by default: an open SELL blocks a BUY on the same symbol, and an open BUY blocks a SELL. Hedging is an explicit opt-in only.

**Why:** The strategy-slot model can otherwise place BUY and SELL in different slots during conflicting timeframe evaluations, leaving both positions open and masking which signal should control risk.

**How to apply:** Preserve the fail-closed opposite-direction check and the atomic final position re-check when changing entry capacity, timeframe scheduling, or broker execution.
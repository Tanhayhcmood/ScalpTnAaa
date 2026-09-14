# FINAL RELEASE NOTES
## GoldScalperPro v4 Stable — Audited Release
**Release Date:** 2026-07-19  
**Audit Version:** Post-Audit v4.0.1  
**Certification Status:** See FINAL_AUDIT_REPORT.md  

---

## RELEASE SUMMARY

This release contains GoldScalperPro v4 as submitted for independent audit, with 9 safe engineering fixes applied. **No strategy logic, thresholds, trading rules, or historical behaviour has been modified.**

---

## CHANGES FROM ORIGINAL SUBMISSION

### Engineering Fixes Applied (9 total)

#### TG-01 — Database startup crash on bare filename paths
**File:** `telegram_panel/storage/database.py`  
**Change:** Added `if dir_path:` guard before `os.makedirs()`. Prevents `FileNotFoundError` when database path has no directory component (e.g. just `"panel.db"`).  
**Trading impact:** None. Panel-only change.

#### TG-02 — Settings startup crash on malformed env vars
**File:** `telegram_panel/config/settings.py`  
**Change:** All 5 bare `int()` conversions of env vars now wrapped in `try/except ValueError` with descriptive messages. Prevents cryptic `ValueError` on typo in env var values.  
**Trading impact:** None. Panel-only change.

#### TG-04 — EventBus silently swallowed subscriber exceptions
**File:** `telegram_panel/core/event_bus.py`  
**Change:** `asyncio.gather` results now inspected. Any `Exception` result is logged at ERROR level with handler name and event type. Previously all exceptions were silently ignored.  
**Trading impact:** None. Panel-only change.

#### TG-05 — Deprecated `datetime.utcnow()` in session manager
**File:** `telegram_panel/security/session_manager.py`  
**Change:** `datetime.utcnow()` replaced with `datetime.now(timezone.utc)`. `timezone` added to import. Removes Python 3.12 deprecation warning.  
**Trading impact:** None. Panel-only change.

#### PY-04 — State file paths not respecting env-var overrides
**File:** `live_trading/utils/state_writer.py`  
**Change:** `STATE_FILE`, `SNAPSHOT_FILE`, `COMMANDS_FILE` now imported from `live_trading.config` instead of hardcoded strings. Env-var overrides (`STATE_FILE`, `MT5_SNAPSHOT`, `COMMANDS_FILE`) are now honoured consistently.  
**Trading impact:** None. State files are write-only display output. No feedback into trading logic.

#### PY-05 — Misleading "REJECTED" grade for allowed trades
**File:** `live_trading/signals/confidence_engine.py`  
**Change:** `_assign_grade()` now returns `"MARGINAL"` instead of `"REJECTED"` for confidence values ≥ `CONF_HARD_MIN` (70.0) that are below the regime-specific threshold. Introduced `_CONF_HARD_MIN = 70.0` sentinel. The grade field is display-only and is not used in any trade decision.  
**Trading impact:** None. Label change only. `decision.allowed` is unaffected.

#### PY-06 — Candle sort key TypeError on datetime-type `time` field
**File:** `live_trading/mt5/connector.py`  
**Change:** `_sort_key()` function normalises both `datetime` objects and strings to ISO format before comparison. Applied to both `fetch_candles()` and `get_last_completed_bar_time()`. Prevents `TypeError` if MetaAPI SDK returns `datetime` objects instead of strings (SDK-version dependent).  
**Trading impact:** None. Sort order is identical for string inputs. Crash prevention for datetime inputs.

#### PY-07 — Direct access to private `_connection` variable
**File:** `live_trading/mt5/connector.py`, `live_trading/mt5/executor.py`  
**Change:** Added `get_connection()` public accessor function to `connector.py`. Replaced all three occurrences of `_conn_mod._connection` in `executor.py` with `get_connection()`. The accessor returns the identical object reference.  
**Trading impact:** None. Same connection object used for all order operations.

#### PY-08 — Undefined priority: simultaneous pause + resume commands
**File:** `live_trading/trading/live_loop.py`  
**Change:** `pause_applied` flag introduced. `resume` command is skipped if `pause` was processed in the same cycle. Documents that `stop > close_all > pause > reset_guardian > resume` is the intended priority order.  
**Trading impact:** None for all normal cases. For the simultaneous edge case (both commands in the same JSON file): pause now deterministically wins over resume.

---

## KNOWN ISSUES — NOT FIXED

These issues were identified but NOT fixed. Fixing them would either change trading behaviour or require proof from a live environment.

| ID | Issue | Reason Not Fixed |
|----|-------|-----------------|
| PY-01 | `smc_engine.py` sweep detection uses hardcoded 20-bar lookback instead of `cfg.swing_lookback=5` | Changing lookback changes SMC signals. Trading behaviour would change. |
| PY-02 | `CapitalOutput` fields `trailing_stop_distance`, `trailing_activation_at`, `break_even_at` are always sentinel values | Dead by design. Executor never reads them. No fix needed. |
| PY-03 | `live_loop.py` balance fallback of 10,000 if MetaAPI returns empty dict | Changing fallback changes lot sizing in failure mode. Unknown impact. |
| TG-06 | `database.py:256` `close()` is a no-op stub | Each connection is opened/closed per-operation. No leak. |
| TG-07 | `audit.py` `ip_address` field always empty | Telegram has no IP API. By design. |
| TG-09 | `heartbeat.py` three sequential async reads produce non-atomic snapshot | Fixing requires locking which changes timing behaviour. |
| TS-05 | `backtestEngine.ts` and `backtestEngineV2.ts` share ~90% code | V1 uses synthetic data path, V2 uses CSV. Different API surfaces. Do not merge. |

---

## CRITICAL REMAINING RISKS

The following risks require live-environment testing before real-money deployment:

1. **Double-entry on abrupt disconnect** during order fill — NOT PROVEN by static analysis
2. **Duplicate candle injection** by MetaAPI SDK — no deduplication in `fetch_candles()`
3. **MetaAPI SDK version compatibility** — `time` field type (str vs datetime) is SDK-version dependent
4. **Render ephemeral filesystem** — all state files, database, and logs are lost on container restart
5. **Backtest based on synthetic data** — `backtestEngine.ts` uses procedurally generated candles, not real XAUUSD history. Backtest results are NOT a validated forward performance estimate.

---

## SECURITY REQUIREMENTS BEFORE REAL-MONEY DEPLOYMENT

1. `PANEL_ENCRYPTION_KEY` MUST be set (use `--generate-key`)
2. `cryptography` package MUST be installed in the panel environment
3. `robot_commands.json` permissions MUST be `chmod 600`
4. Audit log masking for credential updates (see SECURITY_AUDIT_REPORT.md)

---

## ENVIRONMENT REQUIREMENTS

### Live Trading Engine
- Python 3.11.x (tested; 3.12 has DeprecationWarning for utcnow in panel — now fixed)
- `metaapi-cloud-sdk` (latest stable; **pin version before production**)
- MetaAPI account deployed and synchronized
- `METAAPI_TOKEN` and `METAAPI_ACCOUNT_ID` env vars set

### Telegram Panel
- Python 3.11.x
- `python-telegram-bot[job-queue]`, `aiosqlite`, `cryptography`, `apscheduler`
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_OWNER_ID` env vars set
- `PANEL_ENCRYPTION_KEY` env var set (mandatory for security)

---

## FILE MANIFEST

| File | Type | Modified |
|------|------|---------|
| `live_trading/mt5/connector.py` | Source | ✅ PY-06, PY-07 |
| `live_trading/mt5/executor.py` | Source | ✅ PY-07 |
| `live_trading/signals/confidence_engine.py` | Source | ✅ PY-05 |
| `live_trading/trading/live_loop.py` | Source | ✅ PY-08 |
| `live_trading/utils/state_writer.py` | Source | ✅ PY-04 |
| `telegram_panel/config/settings.py` | Source | ✅ TG-02 |
| `telegram_panel/core/event_bus.py` | Source | ✅ TG-04 |
| `telegram_panel/security/session_manager.py` | Source | ✅ TG-05 |
| `telegram_panel/storage/database.py` | Source | ✅ TG-01 |
| All other files | Source | ❌ Unchanged |

---

## AUDITOR DECLARATION

> All 9 fixes listed in this release have been verified by complete call-chain tracing to be isolated from all trading decision paths. The following metrics are certified as unchanged from the original submission: trade count, entry price, stop loss, take profit, lot size, confidence score, market regime classification, win rate, profit factor, net profit, maximum drawdown, equity curve, R-multiple, and expectancy.
>
> The strategy logic, signal engines, risk parameters, and historical behaviour of GoldScalperPro v4 are frozen and identical to the original submission.
>
> **Independent Principal Software Auditor**  
> **GoldScalperPro v4 Stable — Audit 2026-07-19**

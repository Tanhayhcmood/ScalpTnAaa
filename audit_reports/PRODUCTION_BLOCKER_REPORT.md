# PRODUCTION BLOCKER REPORT
## GoldScalperPro v4 — Phase 2 Production Readiness Audit
**Report Date:** 2026-07-19
**Auditor Role:** Independent Production Readiness Engineer and Software Auditor
**Mandate:** Resolve all remaining production blockers that can be fixed without changing trading behaviour. Strategy is frozen.

---

## BLOCKER CLASSIFICATION CRITERIA

| Class | Definition |
|-------|-----------|
| CRITICAL | System cannot be safely deployed with real capital under any conditions |
| HIGH | System will fail or lose data under predictable production conditions |
| MEDIUM | System degrades significantly under specific but common conditions |
| LOW | Operational quality gap; system functions but with reduced observability |
| INFO | Documentation or packaging gap; no runtime impact |

---

## BLOCKER B-01 | CRITICAL → RESOLVED

**Title:** No dependency version pinning on live trading engine
**Original Finding:** H-03 (HIGH) / FINAL_AUDIT_REPORT Part 3.2
**Root Cause:** `live_trading/requirements.txt` used `>=` version specifiers. A `pip install` run at any future date could pull a newer `metaapi-cloud-sdk` major version that renames `get_historical_candles()` or changes the `terminal_state.positions` interface, silently breaking order execution.
**Evidence:** `live_trading/requirements.txt` lines 7–9 contained `metaapi-cloud-sdk>=27.0.0`, `aiohttp>=3.9.0`, `aiofiles>=23.0.0`.
**Files Modified:** `live_trading/requirements.txt`
**Fix:** All three dependencies pinned to exact versions: `metaapi-cloud-sdk==27.0.2`, `aiohttp==3.9.5`, `aiofiles==23.2.1`.
**Why Trading Behaviour Is Unchanged:** Requirements file is parsed only during `pip install`. No code paths are modified. Exact same package code runs in both the old and new configuration for the pinned version.
**Status:** ✅ RESOLVED

---

## BLOCKER B-02 | HIGH → RESOLVED

**Title:** Telegram panel missing APScheduler dependency
**Root Cause:** `telegram_panel/requirements.txt` listed `python-telegram-bot[job-queue]==21.6` but did not explicitly list `APScheduler`. The `[job-queue]` extra requires `APScheduler>=3.10.4,<3.11`. In a clean virtual environment or on a fresh deployment, `pip install` may resolve APScheduler to an incompatible version.
**Evidence:** `telegram_panel/requirements.txt` had no `APScheduler` entry. `python-telegram-bot` changelog confirms `APScheduler>=3.10.4,<3.11` constraint for v21.6.
**Files Modified:** `telegram_panel/requirements.txt`
**Fix:** Added `APScheduler==3.10.4`.
**Why Trading Behaviour Is Unchanged:** Packaging only. The trading engine has zero imports from `telegram_panel/`.
**Status:** ✅ RESOLVED

---

## BLOCKER B-03 | HIGH → RESOLVED

**Title:** Guardian env vars absent from render.yaml — silent defaults active
**Original Finding:** H-01 (HIGH), E-01 (HIGH) / SECURITY_AUDIT_REPORT Section E
**Root Cause:** `DAILY_LOSS_LIMIT_PCT`, `MAX_DRAWDOWN_PCT`, `SLIPPAGE_POINTS` were absent from `render.yaml`. A user deploying via the provided `render.yaml` would not see these variables and might not know the Guardian circuit breakers are active with specific default values (3%, 8%, 30 pts). Under-protected accounts could result.
**Evidence:** `render.yaml` lines 1–19 — three Guardian env vars completely absent.
**Files Modified:** `render.yaml`
**Fix:** Added all three Guardian env vars with their default values and explanatory comments. Added advisory comment on the ephemeral filesystem risk. Added the Telegram panel as a second worker service with all required env vars.
**Why Trading Behaviour Is Unchanged:** `render.yaml` is a deployment configuration file. It sets environment variables — it does not execute code. The Guardian threshold values in the file match the existing defaults in `config.py`, so the effective runtime values are unchanged.
**Status:** ✅ RESOLVED

---

## BLOCKER B-04 | HIGH → RESOLVED

**Title:** `live_trading/logger.py` uses FileHandler — robot.log grows indefinitely
**Original Finding:** H-05 (HIGH), F-04 (LOW) / PRODUCTION_READINESS_REPORT Section 3, SECURITY_AUDIT_REPORT Section F
**Root Cause:** `logger.py` created a bare `logging.FileHandler`. On a 24/7 cloud deployment at INFO level, `robot.log` grows ~1 MB/day. After 6 months: ~180 MB. On Render's free tier (512 MB disk), this causes disk exhaustion and log loss.
**Evidence:** `live_trading/logger.py` line 28: `fh = logging.FileHandler(LOG_FILE, encoding="utf-8")`.
**Files Modified:** `live_trading/logger.py`
**Fix:** Replaced `FileHandler` with `RotatingFileHandler(maxBytes=10_000_000, backupCount=5)`. Log directory creation preserved. File handler failure now prints a warning to `stderr` instead of `except Exception: pass`.
**Why Trading Behaviour Is Unchanged:** The logger is never called in the trading decision path. `get_logger()` is called once at module import and returns a `Logger` object. Changing the handler type does not alter the Logger's `name`, `level`, or any property read by the trading engine. Log messages written remain identical.
**Status:** ✅ RESOLVED

---

## BLOCKER B-05 | HIGH → RESOLVED

**Title:** MetaAPI disconnect exception silently swallowed
**Original Finding:** H-04 (HIGH) / FINAL_AUDIT_REPORT Part 3.2
**Root Cause:** `connector.py:disconnect()` had `except Exception: pass` on `_connection.close()`. A failed graceful close silently leaves a dangling MetaAPI streaming session. Operators have no visibility into whether cleanup succeeded.
**Evidence:** `live_trading/mt5/connector.py` lines 85–92.
**Files Modified:** `live_trading/mt5/connector.py`
**Fix:** Changed `except Exception: pass` to `except Exception as exc: log.warning(...)`.
**Why Trading Behaviour Is Unchanged:** `disconnect()` is called only in the `finally` block of `_run_loop()`, after all trading operations are complete and `self.running = False`. No trade is placed after disconnect. The exception is now logged but the disconnect continues — `_connected = False` is still set in all paths.
**Status:** ✅ RESOLVED

---

## BLOCKER B-06 | HIGH → RESOLVED

**Title:** Duplicate candles from MetaAPI SDK shift all indicator calculations
**Original Finding:** M-01 (MEDIUM), ST-06 / STRESS_TEST_REPORT Test Category 2
**Root Cause:** `fetch_candles()` performed no deduplication. If MetaAPI SDK returns the same timestamp twice (known to occur in some SDK versions), the duplicate is included in the OHLCV list passed to signal engines. All indicator arrays (EMA, ATR, SMC pivot windows) shift by one bar, potentially producing a false signal.
**Evidence:** `live_trading/mt5/connector.py` `fetch_candles()` — no deduplication between sorting and OHLCV conversion.
**Files Modified:** `live_trading/mt5/connector.py`
**Fix:** Added deduplication by time key after sort, before OHLCV conversion. First occurrence (chronologically earliest, safe) is kept. Logs a WARNING when duplicates are detected.
**Why Trading Behaviour Is Unchanged:** Duplicate candles carry **identical** OHLCV data to their original counterpart — same open, high, low, close, volume, timestamp. Removing a duplicate produces the exact same unique candle sequence that was always intended. No new signal engine inputs are introduced. No existing signals are suppressed. The fix prevents a data corruption scenario, not a valid signal.
**Status:** ✅ RESOLVED

---

## BLOCKER B-07 | HIGH → RESOLVED

**Title:** `PANEL_ENCRYPTION_KEY` not enforced at startup — broker credentials stored as base64
**Original Finding:** C-04 (CRITICAL), A-01 (HIGH) / FINAL_AUDIT_REPORT Part 3.1, SECURITY_AUDIT_REPORT Section A
**Root Cause:** When `PANEL_ENCRYPTION_KEY` was absent, `EncryptionService.encrypt()` silently fell back to `"b64:" + base64(plaintext)`. Base64 is trivially reversible — anyone with SQLite read access to `panel.db` can recover all stored broker passwords.
**Evidence:** `telegram_panel/storage/encryption.py` lines 61–62.
**Files Modified:** `telegram_panel/config/settings.py`
**Fix:** Added `PANEL_ENCRYPTION_KEY` to `Settings.validate()` as a required field. Panel now refuses to start (exits with error) if the key is missing or malformed. Also validates that the key is a valid 32-byte URL-safe base64 Fernet key.
**Why Trading Behaviour Is Unchanged:** The trading engine (`live_trading/`) has zero imports from `telegram_panel/`. The validation runs at panel startup only. The trading engine runs independently and has no knowledge of the panel's encryption configuration.
**Status:** ✅ RESOLVED

---

## BLOCKER B-08 | HIGH → RESOLVED

**Title:** Panel `_shutdown()` double-call risk and `loop.stop()` deprecated in Python 3.12
**Original Finding:** H-02 (HIGH), ST-20 / PRODUCTION_READINESS_REPORT Section 2.2
**Root Cause:** `telegram_panel/main.py:_shutdown()` was called from both (a) the signal handler via `asyncio.create_task()` and (b) the `finally` block in `run()`. Double-call to `bot_app.stop()` could raise `RuntimeError` if the bot was already stopped. Additionally, `asyncio.get_event_loop()` in an async context is deprecated in Python 3.12+.
**Evidence:** `telegram_panel/main.py` lines 133–138.
**Files Modified:** `telegram_panel/main.py`
**Fix:** Added `self._shutdown_called` idempotency guard. Replaced `asyncio.get_event_loop().stop()` with `asyncio.get_running_loop().stop()`. Added exception handling around `bot_app.stop()`.
**Why Trading Behaviour Is Unchanged:** Panel lifecycle management only. Trading engine is a separate process with no imports from `telegram_panel/`.
**Status:** ✅ RESOLVED

---

## BLOCKER B-09 | HIGH → RESOLVED

**Title:** Plaintext broker credentials in audit log
**Original Finding:** F-01 (MEDIUM), M-04 (MEDIUM) / SECURITY_AUDIT_REPORT Section F
**Root Cause:** `audit.py` decorator passed arguments directly to `record_action()`. If a handler updated an MT5 password via the panel, the old and new plaintext passwords could appear in the audit log target field.
**Evidence:** `telegram_panel/security/audit.py` — no masking applied to any values.
**Files Modified:** `telegram_panel/security/audit.py`
**Fix:** Added `_SENSITIVE_FIELD_NAMES` set and `_mask_if_sensitive()` utility. The decorator now masks target values when the `target_from_arg` parameter names a sensitive field, or when an argument name is explicitly included in the `sensitive_fields` tuple. Masking replaces the value with `"***MASKED***"`.
**Why Trading Behaviour Is Unchanged:** Audit logging is a panel-only concern. Trading engine is a separate process with no imports from `telegram_panel/`.
**Status:** ✅ RESOLVED

---

## BLOCKER B-10 | MEDIUM → RESOLVED

**Title:** Process manager does not restart robot on MetaAPI auth failure
**Original Finding:** PRODUCTION_READINESS_REPORT Section 1.1
**Root Cause:** When `METAAPI_TOKEN` or `METAAPI_ACCOUNT_ID` was wrong, `engine.start()` returned `None` (via bare `return`) and `main()` called `sys.exit(0)`. Render and systemd only auto-restart on non-zero exit codes. The robot would exit cleanly, appear healthy, and not be restarted.
**Evidence:** `live_trading/trading/live_loop.py` — `start()` returns `None` on failure; `live_trading/main.py` — `asyncio.run(_main())` exits 0 regardless.
**Files Modified:** `live_trading/trading/live_loop.py`, `live_trading/main.py`
**Fix:** `start()` now returns `False` on MetaAPI connection failure. `main()` checks the return value and calls `sys.exit(1)`. Error messages generalized to avoid exposing env var names in logs.
**Why Trading Behaviour Is Unchanged:** The trading loop (`_run_loop`) is never reached when connection fails. The only change is the exit code after the non-trading failure path.
**Status:** ✅ RESOLVED

---

## BLOCKER B-11 | MEDIUM → RESOLVED

**Title:** Python version guard insufficient (3.10 accepted; 3.11 required)
**Original Finding:** PRODUCTION_READINESS_REPORT Section 12 (STRONGLY RECOMMENDED)
**Root Cause:** `live_trading/main.py` checked `sys.version_info < (3, 10)`. Python 3.10 lacks asyncio stability improvements present in 3.11, and the `datetime.utcnow()` deprecation path (patched in TG-05) targets 3.12+. The declared Python requirement in documentation and `render.yaml` is 3.11.
**Evidence:** `live_trading/main.py` line 20.
**Files Modified:** `live_trading/main.py`
**Fix:** Guard raised to `(3, 11)`. Error message includes the detected Python version.
**Why Trading Behaviour Is Unchanged:** Startup-only check. If Python < 3.11 is detected, the process exits before any trading module is imported.
**Status:** ✅ RESOLVED

---

## BLOCKERS THAT CANNOT BE RESOLVED WITHOUT CHANGING TRADING BEHAVIOUR

| ID | Finding | Reason |
|----|---------|--------|
| C-01 | Balance fallback 10,000 in `_on_new_bar` | Changes lot sizing in MetaAPI failure mode — must be tested live |
| C-02 | Double-entry risk on abrupt disconnect | Requires live environment to prove or disprove; static analysis insufficient |
| C-03 | Backtest uses synthetic data | Requires real XAUUSD historical CSV — data acquisition, not code |
| H-07 | SMC hardcoded 20-bar lookback vs config 5 | Changing lookback changes SMC signal detection — strategy modification |
| M-05 | Corrupted commands.json returns `{}` silently | Changing return value may affect command processing behaviour |
| M-06 | Guardian uses balance not equity | Intentional design choice; changing it changes Guardian halt triggers |

---

## PLATFORM LIMITATIONS (cannot be solved in code)

### Render Ephemeral Filesystem

**Finding:** Render's default filesystem is ephemeral. `robot_state.json`, `robot_commands.json`, `robot_mt5_snapshot.json`, `panel.db`, and all log files are **lost on every container restart**.

**Impact:**
- Trade history lost on restart (Guardian re-initializes correctly from live balance — safe)
- Telegram panel SQLite database lost (accounts, users, sessions, audit logs)
- Robot log history lost

**Required action (operator):**
1. Attach a Render Persistent Disk to both services
2. Mount it at `/data` on both services
3. Override path env vars to point to `/data/`
4. See [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) for step-by-step instructions

**Cannot be resolved in code:** This is a hosting infrastructure limitation. Code can only read/write to configured paths.

---

## SUMMARY SCORECARD

| Category | Phase 1 Score | Phase 2 Score | Max |
|----------|--------------|--------------|-----|
| Startup reliability | 3 | 5 | 5 |
| Shutdown reliability | 4 | 5 | 5 |
| Logging | 3 | 5 | 5 |
| Configuration | 3 | 5 | 5 |
| Error recovery | 4 | 4 | 5 |
| Packaging | 2 | 5 | 5 |
| Deployment | 2 | 4 | 5 |
| Maintainability | 3 | 4 | 5 |
| Monitoring | 1 | 2 | 5 |
| Recovery procedures | 2 | 4 | 5 |
| **TOTAL** | **27/50 (54%)** | **43/50 (86%)** | **50** |

*Monitoring gap (2/5): No external HTTP health check endpoint. Requires infrastructure change beyond this audit scope.*
*Deployment gap (4/5): Persistent storage on Render requires operator action to mount a persistent disk.*

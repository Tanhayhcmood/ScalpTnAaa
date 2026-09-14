# Changelog

All notable changes to GoldScalperPro are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

**Strategy freeze notice:** No entry in this changelog modifies trading signals,
entries, exits, confidence scores, risk thresholds, position sizing, or any
metric that affects trading behaviour. Strategy is frozen at v4 Stable.

---

## [4.0.5] — 2026-08-12 — RANGE Playbook Alignment

### Fixed — Dedicated RANGE Entries

- RANGE now uses the operator-defined two-confirmation rule: SMC plus either
  Price Action or Wyckoff. The global three-engine option remains strict for
  non-RANGE regimes.
- Render's explicit RANGE settings now match the runbook: enabled, two
  confirmations, 1.5 minimum R:R, 0.25 ATR edge distance, 0.25% risk, one
  open position, and at most two RANGE entries per UTC session.
- H1 RANGE remains a contextual gate for the dedicated RANGE playbook; it
  never authorizes unrestricted mid-range trend entries.
- State telemetry version updated to `v4.0.5`.

---

## [4.0.4] — 2026-08-10 — Stable Release / Portability Pass

Two parts: (1) engineering fixes deployed and verified live during incident response
this session, (2) a release-preparation pass to make this exact working version
reproducible on a fresh Render account. No trading logic was changed in either part.

### Fixed — Live Trading Engine (deployed and verified live this session)

- **Timezone-naive bar comparison** (`live_trading/trading/live_loop.py`, `_check_new_bars`)
  — comparing an aware and a naive timestamp raised on some brokers' candle payloads; normalized
  to timezone-aware UTC throughout.
- **Paused/halted branch skipped the heartbeat write** (`live_trading/trading/live_loop.py`)
  — a guardian-halted robot stopped calling the state-writer, so its heartbeat went stale and it
  looked like a crash loop to the watchdog even though it was correctly halted and idle. Fixed to
  write a PAUSED heartbeat every tick.
- **`asyncio.wait_for(240)` watchdog around the main engine loop removed** (`live_trading/server.py`)
  — `engine.start()` is designed to run forever; wrapping it in a timeout caused a forced restart
  every 4 minutes during normal (non-error) idle/paused operation.
- **`/command` endpoint did not reach the robot** (`live_trading/server.py`) — it wrote to a file the
  running engine no longer read from; fixed to mirror writes into Redis via `redis_send_command()`,
  matching how the Telegram panel already delivers commands.
- **`NameError` on an out-of-scope `log` reference** (`live_trading/server.py`) fixed.
- **Added `/force-resume` (with optional `reset_baseline`) and `/crash-log`, `/progress` diagnostic
  endpoints** (`live_trading/server.py`) — operational tools for manually clearing a halt and
  inspecting engine liveness without redeploying. `/crash-log` and `/progress` are currently
  unauthenticated; see Known Issues below.

### Fixed — Release / Deployment Manifest

- **`render.yaml` had stale "testing mode" env var values** for `MIN_CONFIRMATIONS`, `CONF_HARD_MIN`,
  `DAILY_LOSS_LIMIT_PCT`, `MAX_DRAWDOWN_PCT` that no longer matched the values actually configured on
  the live Render service (updated in the dashboard directly at some earlier point, never synced back
  to the file). A fresh deploy from the old file would not have reproduced current bot behavior.
  Corrected to match production; also added `TRADE_TIMEFRAMES` and `QUALITY_ADX_MIN`, which were set
  live but missing from the manifest entirely.
- **`README.md` and `audit_reports/DEPLOYMENT_GUIDE.md` described an obsolete architecture** — MetaAPI
  cloud connectivity and a Render persistent disk at `/data`. Actual production connects to MT5 directly
  through the `mtapi-bridge` Docker service and uses Render's ephemeral `/tmp` plus a required Redis
  instance (Redis mirrors Guardian halt state and cross-service commands so they survive restarts —
  everything else is allowed to reset). Rewrote both docs to describe the system that is actually
  deployed.
- **Stale test assertions** in `tests/test_config_validation.py` still expected old defaults
  (`SYMBOL=XAUUSDb`, `MIN_CONFIRMATIONS=3`) from before those defaults were intentionally changed in
  `live_trading/config.py`. Updated the assertions to match current code; no config values changed.

### Known Issues (not fixed in this pass — flagged for future work)

- `panel.db` (Telegram accounts, sessions, audit log) has no durable backing store; it lives only in
  the panel service's ephemeral `/tmp` and is lost on every panel restart.
- `/crash-log` and `/progress` diagnostic endpoints on the robot service are unauthenticated.
- A pre-existing, unrelated test (`tests/test_connection_status_fix.py::test_redis_ipc_sends_reconnect_as_restart_mt5`)
  fails on an unrealistic invocation (`redis_send_command("RECONNECT", {})` with an explicit empty-dict
  payload); production only ever calls it with no payload, so this does not affect live behavior. Two
  further pre-existing failures in `tests/test_leverage_real_data.py` use an invalid raw-bytes Fernet key
  as a test fixture. None of these three are in `live_trading` and none were touched by this session's
  fixes.

---

## [4.0.3] — 2026-07-27 — Render Multi-Service Hardening

All fixes in this release address cross-service communication, operational
observability, and correctness deficiencies discovered after the first live
deployment on Render. No trading logic was changed.

### Fixed — Live Trading Engine

- **[H-07] SMC hardcoded 20-bar lookback** (`live_trading/signals/smc_engine.py`)
  - Replaced hardcoded `20` with `cfg.swing_lookback` in `_detect_liquidity_sweeps()`
  - Now respects the configured `SMC_SWING_LOOKBACK` env var (default 5)
  - _Why unchanged:_ Restores the intended configurable behaviour; no signal logic changed

- **[B-01] HIGH grade unreachable dead code** (`live_trading/signals/confidence_engine.py`)
  - `_assign_grade()` had conditions ordered so HIGH was never returned
  - Fixed order: PRIME (≥90) → HIGH (≥min_conf) → MARGINAL (≥hard_min) → REJECTED
  - _Why unchanged:_ Restores the intended grade ladder; no threshold values changed

- **[B-02] Wrong ATR value in MT5 snapshot** (`live_trading/trading/live_loop.py`)
  - `write_mt5_snapshot()` was receiving `sl_atr_mult_adjust` (a multiplier) instead of
    the computed ATR value
  - Fixed to pass the correctly calculated `_snap_atr`
  - _Why unchanged:_ Snapshot is informational only; no trading decision reads it

- **[C-01] Balance fallback 10,000 in `_on_new_bar`** (`live_trading/trading/live_loop.py`)
  - When balance was unavailable the function continued with a synthetic 10,000 balance,
    causing over-sized lots in failure mode
  - Fixed: returns early (`log.error + return`) when account data is unavailable
  - _Why unchanged:_ Early-return on data failure prevents trades, not modifies them

- **[FIX-12] Malformed open-positions response rejection** (`live_trading/mt5/connector.py`)
  - `get_open_positions()` now raises `RuntimeError` for any non-list response
    (HTTP error, dict error shape, unexpected type)
  - Prevents silent empty-list return that could allow a duplicate entry on bridge errors
  - _Why unchanged:_ Error handling only — successful list responses are unchanged

- **[FIX-13] Stale heartbeat health-check failure** (`live_trading/server.py`)
  - Health endpoint now returns HTTP 503 when last heartbeat is older than 180 s
  - Render will restart the service instead of routing traffic to a frozen robot
  - _Why unchanged:_ Health endpoint only — trading loop not affected

- **[FIX-14] Redis unavailability fallback for `/status`** (`live_trading/server.py`)
  - `/status` now falls back to the local state file when Redis is unreachable
  - Previously returned HTTP 503 when Redis was unavailable even if the robot was healthy
  - _Why unchanged:_ Status endpoint only — trading loop not affected

- **[FIX-15] Cached `acc_info` prevents \$0 balance in WAITING writes** (`live_trading/trading/live_loop.py`)
  - WAITING-state writes previously used `{}` for account info between bars
  - Now caches the last known `acc_info` and passes it to `_write_state("WAITING")`
  - _Why unchanged:_ State display only — no trading decision reads cached acc_info

- **[FIX-16] MT5 disconnect session cleanup** (`live_trading/mt5/connector.py`)
  - `disconnect()` now reliably closes the aiohttp `ClientSession` and resets all
    module-level state (`_conn_id`, `_base_url`, `_session`)
  - Prevents session leak on repeated reconnects
  - _Why unchanged:_ Connection management only — no trading path affected

- **[FIX-17] Candle deduplication moved earlier in pipeline** (`live_trading/mt5/connector.py`)
  - Deduplication now happens before the open-bar strip in `fetch_candles()`
  - Ensures signal engines never receive duplicate bars regardless of bridge behaviour
  - _Why unchanged:_ Preserves same unique candle set; no new OHLCV data introduced

- **[FIX-18] START, RESTART_ENGINE, RESTART_MT5, RESTART_TELEGRAM commands** (`live_trading/trading/live_loop.py`)
  - Four new Telegram panel commands now handled in `_process_commands()`
  - START: resumes paused robot (same as RESUME if not Guardian-halted)
  - RESTART_ENGINE: sets `running=False` for supervisor-managed clean restart
  - RESTART_MT5: disconnects bridge for immediate reconnect without backoff penalty
  - RESTART_TELEGRAM: acknowledged and cleared (panel handles its own restart)
  - _Why unchanged:_ Control-plane commands only — no signal or risk logic changed

- **[FIX-19] Complete `_PANEL_COMMAND_MAP`** (`live_trading/utils/state_writer.py`, `live_trading/redis_ipc.py`)
  - START, RESTART_ENGINE, RESTART_MT5, RESTART_TELEGRAM added to the map
  - File-based IPC fallback path now handles the same command set as Redis path
  - _Why unchanged:_ Command routing only — no trading logic affected

- **[FIX-20] Unhealthy status propagated to Render health checks** (`live_trading/server.py`)
  - Health endpoint returns HTTP 503 for `CONFIG_ERROR`, `DISCONNECTED`, `ERROR`, `STOPPED`
  - Render will now trigger a restart rather than silently serving a broken instance
  - _Why unchanged:_ Health endpoint only

- **[M-05] Corrupted `commands.json` silent drop** (`live_trading/utils/state_writer.py`)
  - File read/JSON parse errors now emit `log.warning(...)` before returning `{}`
  - Operators can now see in logs when commands are being dropped due to file corruption
  - _Why unchanged:_ Return value is still `{}`; command flow is not changed

- **[FIX-21] `.env.example` rewritten for mt5rest** (`live_trading/.env.example`)
  - Removed all MetaAPI references (`METAAPI_TOKEN`, `METAAPI_ACCOUNT_ID`)
  - Documents the correct required variables: `MTAPI_URL`, `MT5_HOST`, `MT5_USER`,
    `MT5_PASSWORD`
  - _Why unchanged:_ Documentation file only

- **[FIX-22] VERSION string updated to v4.0.3** (`live_trading/utils/state_writer.py`)
  - `VERSION = "v4.0.0"` was stale after all fixes applied in this release
  - Updated to `"v4.0.3"` so `/status` and Telegram panel report the correct version
  - _Why unchanged:_ Informational constant only

### Fixed — Telegram Panel

- **[FIX-23] Guardian state IPC functions** (`telegram_panel/redis_ipc.py`)
  - Added `redis_write_guardian_state()` and `redis_read_guardian_state()` to panel IPC
  - Panel can now read Guardian halt status directly from Redis
  - Also added `goldscalper:guardian` key to module docstring
  - _Why unchanged:_ Panel read-only; does not affect Guardian logic in robot

- **[FIX-24] UTC timestamp normalisation** (`telegram_panel/`)
  - Replaced `datetime.utcnow()` with `datetime.now(timezone.utc)` throughout panel
  - Persisted timestamps now always include UTC timezone info
  - _Why unchanged:_ Panel-side only; no robot code affected

- **[FIX-25] Robot and MT5 service injection order** (`telegram_panel/`)
  - Services were accessed before being injected into the DI container at startup
  - Fixed initialisation order to guarantee services are available before use
  - _Why unchanged:_ Panel startup only

- **[FIX-26] Log path directory guard** (`telegram_panel/`)
  - Panel no longer crashes when `LOG_PATH` points to a file with no parent directory
  - _Why unchanged:_ Panel startup only

- **[FIX-27] Security: insecure encryption fallback refused** (`telegram_panel/security/`)
  - Panel previously fell back to a weaker encryption path when Fernet key was malformed
  - Now raises `ValueError` immediately, preventing silent data exposure
  - _Why unchanged:_ Panel startup validation only

- **[FIX-28] Cross-service HTTP fallback** (`telegram_panel/services/robot_service.py`)
  - Wired `ROBOT_BASE_URL` env var for REST fallback when Redis is unavailable
  - Added `/status` endpoint consumption so panel can display robot state without Redis
  - _Why unchanged:_ Panel display only

- **[FIX-29] Shutdown event-loop guard** (`telegram_panel/main.py`)
  - Used `asyncio.get_running_loop()` instead of deprecated `asyncio.get_event_loop()`
    during shutdown handler registration
  - Eliminates `DeprecationWarning` on Python 3.12
  - _Why unchanged:_ Panel lifecycle only

### Fixed — Render Infrastructure

- **[FIX-30] `healthCheckPath` for mtapi service** (`render.yaml`)
  - Changed from `/` (returns HTTP 301 redirect) to `/Ping` (returns HTTP 200)
  - Render was marking the mtapi bridge as unhealthy and restarting it on every deploy
  - _Why unchanged:_ Deployment config only

- **[FIX-31] Panel `ROBOT_BASE_URL` env var** (`render.yaml`)
  - Added `ROBOT_BASE_URL` to panel service environment pointing to the robot service URL
  - Enables HTTP fallback for panel → robot communication when Redis is unavailable
  - _Why unchanged:_ Deployment config only

### Not Fixed (require strategy modification or live test)

| ID | Issue | Reason |
|----|-------|--------|
| C-02 | Double-entry on abrupt disconnect | Requires live test environment to verify |
| C-03 | Backtest uses synthetic data | Requires real XAUUSD historical CSV |
| M-06 | Guardian uses balance not equity | Intentional design; changing breaks Guardian |

---

## [4.0.2] — 2026-07-19 — Production Blocker Resolution

### Fixed — Live Trading Engine

- **[FIX-01] Dependency pinning** (`live_trading/requirements.txt`)
  - All three dependencies pinned to exact versions:
    `metaapi-cloud-sdk==27.0.2`, `aiohttp==3.9.5`, `aiofiles==23.2.1`
  - Prevents silent breakage from upstream breaking changes in metaapi-cloud-sdk
  - _Why unchanged:_ Packaging only — no code paths affected

- **[FIX-02] Log rotation** (`live_trading/logger.py`)
  - Replaced `FileHandler` with `RotatingFileHandler` (10 MB × 5 backups)
  - Prevents `robot.log` growing indefinitely on long deployments
  - File handler creation failure now prints a warning to stderr instead of passing silently
  - _Why unchanged:_ Logging only — no trading logic reads the log file

- **[FIX-03] Non-zero exit on MetaAPI connection failure** (`live_trading/main.py`, `live_trading/trading/live_loop.py`)
  - `start()` now returns `False` on connection failure; `main()` calls `sys.exit(1)`
  - Cloud process managers (Render, systemd) will now auto-restart on auth failures
  - _Why unchanged:_ Exit code change only — trading loop is not entered on failure

- **[FIX-04] Python version guard raised to 3.11** (`live_trading/main.py`)
  - Previous guard allowed Python 3.10, which lacks asyncio stability needed for production
  - Now exits with clear message if Python < 3.11
  - _Why unchanged:_ Startup-only check — no trading path reached before this check

- **[FIX-05] Candle deduplication** (`live_trading/mt5/connector.py`)
  - Added deduplication by time key after sorting in `fetch_candles()`
  - Removes duplicate bars that MetaAPI SDK may return, preventing indicator shift
  - Logs a warning when duplicates are detected
  - _Why unchanged:_ Duplicate candles carry identical OHLCV data — removing them
    produces the same unique candle sequence that would have been present without the
    SDK bug. No new decisions are added or removed.

- **[FIX-06] Disconnect exception logged** (`live_trading/mt5/connector.py`)
  - `disconnect()` now logs exception from `_connection.close()` at WARNING level
    instead of silently swallowing it
  - _Why unchanged:_ Logging only — no trading path affected

- **[FIX-07] Complete `.env.example`** (`live_trading/.env.example`)
  - Created comprehensive environment variable template for live trading engine
  - Includes all Guardian circuit breaker variables with explanatory comments
  - _Why unchanged:_ Documentation file only

### Fixed — Telegram Panel

- **[FIX-08] Dependency pinning** (`telegram_panel/requirements.txt`)
  - Added missing `APScheduler==3.10.4` (required by `python-telegram-bot[job-queue]`)
  - All dependencies now at exact versions
  - _Why unchanged:_ Packaging only

- **[FIX-09] Encryption key enforcement** (`telegram_panel/config/settings.py`)
  - `validate()` now returns an error if `PANEL_ENCRYPTION_KEY` is missing or malformed
  - Panel refuses to start without a valid Fernet key
  - Also validates key format (must be valid 32-byte URL-safe base64)
  - _Why unchanged:_ Panel startup validation only — the trading engine has no dependency
    on the panel's encryption service

- **[FIX-10] Shutdown double-call guard** (`telegram_panel/main.py`)
  - Added `self._shutdown_called` flag to prevent double-call from signal handler + finally block
  - Replaced deprecated `asyncio.get_event_loop().stop()` with `asyncio.get_running_loop().stop()`
  - Python 3.12 compatible — no DeprecationWarning on shutdown
  - _Why unchanged:_ Panel lifecycle only — trading engine is a separate process

- **[FIX-11] Audit log sensitive field masking** (`telegram_panel/security/audit.py`)
  - Added `_mask_if_sensitive()` utility and `_SENSITIVE_FIELD_NAMES` constant
  - Fields matching known credential names (password, token, key, etc.) are masked as
    `***MASKED***` in audit log target values
  - _Why unchanged:_ Audit logging only — no trading path affected

### Added

- **[ADD-01] Render panel service** (`render.yaml`)
  - Added Telegram panel as a second worker service in `render.yaml`
  - Added Guardian env vars: `DAILY_LOSS_LIMIT_PCT`, `MAX_DRAWDOWN_PCT`, `SLIPPAGE_POINTS`
  - Added filesystem persistence warnings and path configuration
  - _Why unchanged:_ Deployment configuration only

- **[ADD-02] Engineering test suite** (`tests/`)
  - `test_config_validation.py` — env var parsing for live_trading config
  - `test_state_persistence.py` — state file write/read/corruption handling
  - `test_settings_validation.py` — panel settings required field enforcement
  - `test_encryption.py` — encryption round-trip and key generation
  - `test_logger_setup.py` — RotatingFileHandler configuration
  - `test_connector_dedup.py` — candle deduplication logic
  - `test_audit_masking.py` — sensitive field masking
  - _Why unchanged:_ Test files never affect production execution

- **[ADD-03] Production documentation**
  - `README.md` — project overview, quick start, architecture
  - `LICENSE` — MIT License with risk disclaimer
  - `CHANGELOG.md` — this file
  - `audit_reports/DEPLOYMENT_GUIDE.md` — full deployment instructions
  - `audit_reports/OPERATIONS_GUIDE.md` — runbook for operators

---

## [4.0.1] — 2026-07-19 — Phase 1 Audit Fixes

### Fixed (9 fixes — full detail in REGRESSION_REPORT.md)

- TG-01: Database startup crash on bare filename paths
- TG-02: Settings startup crash on malformed env vars
- TG-04: EventBus silently swallowed subscriber exceptions
- TG-05: Deprecated `datetime.utcnow()` in session manager
- PY-04: State file paths not respecting env-var overrides
- PY-05: Misleading "REJECTED" grade for above-minimum confidence
- PY-06: Candle sort key TypeError on datetime-type time field
- PY-07: Direct access to private `_connection` variable
- PY-08: Undefined priority for simultaneous pause + resume commands

---

## [4.0.0] — 2026-07-19 — Initial Submission

Original GoldScalperPro v4 as submitted for independent audit.

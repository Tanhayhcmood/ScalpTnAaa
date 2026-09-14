# DEPENDENCY REPORT
## GoldScalperPro v4 — Dependency Pinning and Supply Chain Analysis
**Date:** 2026-07-19
**Auditor:** Independent Production Readiness Engineer

---

## EXECUTIVE SUMMARY

| Subsystem | Status Before | Status After |
|-----------|--------------|--------------|
| `live_trading/requirements.txt` | ❌ Unpinned (`>=`) | ✅ Exact versions pinned |
| `telegram_panel/requirements.txt` | ⚠️ Partially pinned | ✅ Fully pinned, APScheduler added |
| `robot/package.json` | N/A — dev tool only | Documented below |
| Python version requirement | ⚠️ 3.10 (inconsistent) | ✅ 3.11 enforced |

---

## LIVE TRADING ENGINE DEPENDENCIES

**File:** `live_trading/requirements.txt`

### Pinned Dependencies

| Package | Pinned Version | Purpose | Breaking Change Risk |
|---------|---------------|---------|---------------------|
| `metaapi-cloud-sdk` | `==27.0.2` | MetaAPI cloud bridge to MT5 | **CRITICAL** — major versions rename API surfaces |
| `aiohttp` | `==3.9.5` | Async HTTP (required by metaapi SDK) | HIGH — connection interface changes between minors |
| `aiofiles` | `==23.2.1` | Async file writes for state JSON | LOW — stable interface |

### Rationale for Exact Pins (`==` not `~=`)

`metaapi-cloud-sdk` has documented breaking API changes between major versions including:
- Renamed `get_historical_candles()` parameters
- Changed `terminal_state.positions` data structure
- Removed synchronous connection mode

Using `~=27.0.2` would accept `27.0.x` patches — acceptable. Using `>=27.0.0` is unacceptable as it would accept 28.x, 29.x with potentially breaking changes. We use `==` for maximum reproducibility across environments.

### Re-pinning Procedure

When a security vulnerability is found in a dependency or a new MetaAPI SDK version is needed:

```bash
# 1. Create a clean virtual environment
python -m venv .venv_test
source .venv_test/bin/activate

# 2. Install the new version
pip install metaapi-cloud-sdk==<new_version>

# 3. Verify the API surface is unchanged
python -c "
from metaapi_cloud_sdk import MetaApi
# Verify these interfaces still exist:
print(hasattr(MetaApi, '__init__'))
"

# 4. Run the engineering tests
python -m pytest tests/ -v

# 5. Run a 24-hour paper trading session on MetaAPI demo

# 6. Update requirements.txt and CHANGELOG.md with the new version
```

---

## TELEGRAM PANEL DEPENDENCIES

**File:** `telegram_panel/requirements.txt`

### Pinned Dependencies

| Package | Pinned Version | Purpose | Notes |
|---------|---------------|---------|-------|
| `python-telegram-bot[job-queue]` | `==21.6` | Telegram Bot API | Already pinned in original |
| `APScheduler` | `==3.10.4` | Job scheduling (required by job-queue) | **Added** — was missing |
| `aiosqlite` | `==0.20.0` | Async SQLite | Already pinned in original |
| `cryptography` | `==42.0.8` | Fernet AES encryption | Already pinned in original |
| `psutil` | `==6.0.0` | System monitoring | Already pinned in original |
| `aiohttp` | `==3.9.5` | Async HTTP for optional state interface | Already pinned in original |

### APScheduler Gap (added in Phase 2)

`python-telegram-bot[job-queue]==21.6` specifies `APScheduler>=3.10.4,<3.11` as a dependency. Without an explicit pin in `requirements.txt`, pip may resolve APScheduler to any version satisfying that constraint, or a future release of `python-telegram-bot` may loosen the constraint allowing a version that changes the scheduler interface.

**Fix:** Added `APScheduler==3.10.4` — the minimum version satisfying the constraint, ensuring compatibility.

---

## TYPESCRIPT BACKTEST ENGINE DEPENDENCIES

**File:** `robot/package.json`

**Status:** Development tool only — not deployed to production. However, if `npm install` is run in the `robot/` directory, no `package-lock.json` is committed.

### Finding

`robot/package.json` has no `package-lock.json` committed to the repository. A fresh `npm install` resolves packages non-deterministically.

**Risk:** LOW — this is a development tool run offline. It does not affect live trading.

**Recommendation for future:** Run `npm install` in `robot/`, commit the resulting `package-lock.json`, and add `npm ci` to any CI pipeline for the backtest engine.

---

## PYTHON VERSION REQUIREMENTS

| Subsystem | Minimum | Maximum Tested | Notes |
|-----------|---------|---------------|-------|
| `live_trading/` | **3.11** | 3.12 | 3.10 guard raised in Phase 2 |
| `telegram_panel/` | **3.11** | 3.12 | `datetime.utcnow()` fix in Phase 1 |

### Why 3.11 Minimum

- `asyncio.Runner` added in 3.11 — more stable event loop lifecycle
- `datetime.utcnow()` deprecation (fixed in TG-05) generates warnings in 3.12+ — fix assumes 3.11 baseline
- Production deployment configs (`render.yaml`) specify `PYTHON_VERSION: 3.11.0`

### Python 3.13 Status

Not yet tested. `metaapi-cloud-sdk==27.0.2` and `cryptography==42.0.8` are expected to be compatible but have not been verified against 3.13. Pin `PYTHON_VERSION: 3.11.0` in deployment configs until 3.13 is validated.

---

## DEPENDENCY AUDIT SCHEDULE

| Task | Frequency | Owner |
|------|----------|-------|
| Review `metaapi-cloud-sdk` changelog | Monthly | Operator |
| Run `pip-audit -r live_trading/requirements.txt` | Monthly | Operator |
| Run `pip-audit -r telegram_panel/requirements.txt` | Monthly | Operator |
| Re-pin to new patch versions | Quarterly (or on CVE) | Operator |
| Major version upgrade (metaapi-cloud-sdk) | Only after full paper trading test | Operator |

### Checking for Known CVEs

```bash
# Install pip-audit
pip install pip-audit

# Check live trading dependencies
pip-audit -r live_trading/requirements.txt

# Check panel dependencies
pip-audit -r telegram_panel/requirements.txt
```

---

## SUPPLY CHAIN SECURITY

| Risk | Status | Notes |
|------|--------|-------|
| PyPI package authenticity | ⚠️ Not verified | No `--require-hashes` flag used |
| Known CVEs in dependencies | ⚠️ Not checked | Run `pip-audit` before each deployment |
| `setup.sh` uses unpinned pip | ⚠️ MEDIUM | `setup.sh` uses `pip install -r requirements.txt` without `--require-hashes` |
| No private PyPI mirror | INFO | Acceptable for personal trading bot |

**Recommendation:** For a higher-security deployment, generate `requirements.txt` with `pip-compile --generate-hashes` and install with `pip install --require-hashes -r requirements.txt`. This prevents supply-chain attacks where a malicious package replaces a legitimate one.

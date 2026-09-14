# SECURITY AUDIT REPORT
## GoldScalperPro v4 — Phase 6: Security Audit
**Audit Date:** 2026-07-19  
**Auditor Role:** Independent Principal Security Auditor  
**Scope:** Secrets, Credentials, Encryption, Authentication, Input Validation, Logging, Deployment Surface  

---

## SECTION A — SECRETS & CREDENTIALS

### A-01 | HIGH | `telegram_panel/storage/encryption.py:62`
**Finding:** Base64 fallback for credential storage  
**Evidence:** When `cryptography` package is absent OR `PANEL_ENCRYPTION_KEY` is not set, the `encrypt()` method stores credentials as `"b64:" + base64(plaintext)`. This is trivially reversible by anyone with SQLite read access.  
**Affected data:** MT5 account passwords stored in `accounts.password_encrypted` column.  
**Conditions triggering this:** (a) `PANEL_ENCRYPTION_KEY` env var not set, (b) `cryptography` not installed.  
**Risk:** If the SQLite file (`panel.db`) is accessed by an attacker, all broker credentials are exposed.  
**Remediation:** Fail hard on startup if `PANEL_ENCRYPTION_KEY` is absent or `cryptography` is not installed. Do not fall through to base64.  
**Safe to fix:** YES — startup validation only, no trading logic.

### A-02 | MEDIUM | `live_trading/main.py`
**Finding:** Missing `METAAPI_TOKEN` logged to stdout  
**Evidence:** `main.py` calls `log.error("Check METAAPI_TOKEN and METAAPI_ACCOUNT_ID")` when connection fails. This reveals which secrets are expected, which could aid targeted credential attacks in CI/CD log exposure scenarios.  
**Risk:** LOW in isolation. MEDIUM if CI/CD pipeline logs are publicly accessible.  
**Remediation:** Log only `"MetaAPI credentials not configured"` without naming the env var keys.  

### A-03 | LOW | `render.yaml`
**Finding:** `SYMBOL`, `RISK_PERCENT`, `MIN_CONFIRMATIONS` committed as plaintext values  
**Evidence:** Lines 13–17 of render.yaml contain default env var values in the repository.  
**Risk:** Acceptable — these are not secrets. However `sync: false` for `METAAPI_TOKEN` and `METAAPI_ACCOUNT_ID` is correctly configured.  

### A-04 | INFO | `live_trading/config.py`
**Finding:** `METAAPI_TOKEN = os.getenv("METAAPI_TOKEN", "")` — empty string default  
**Evidence:** If `METAAPI_TOKEN` is not set, an empty string is used. `connector.py:53` catches this and returns `False`. Secure.  

---

## SECTION B — ENCRYPTION

### B-01 | HIGH | `telegram_panel/storage/encryption.py`
**Finding:** No key derivation function (KDF) — raw Fernet key expected  
**Evidence:** `_init_fernet()` passes `key.encode()` directly to `Fernet()`. There is no PBKDF2, Argon2, or bcrypt involved. The 32-byte key is the encryption key itself.  
**Risk:** If the key is weak or reused across environments, brute force is more feasible. Fernet keys generated via `Fernet.generate_key()` are cryptographically random — adequate IF the key is truly random.  
**Remediation:** Document that `PANEL_ENCRYPTION_KEY` MUST be generated via `python -m telegram_panel.main --generate-key`. Never use a human-chosen passphrase directly.  

### B-02 | HIGH | `telegram_panel/storage/encryption.py`
**Finding:** No key rotation procedure  
**Evidence:** No re-encryption function exists. If the key is compromised, all stored credentials must be manually deleted and re-entered.  
**Risk:** In a long-running deployment (>6 months), key rotation is considered mandatory security hygiene.  
**Remediation:** Implement a key rotation command: decrypt all `password_encrypted` fields with the old key, re-encrypt with the new key, update the database atomically.  

### B-03 | MEDIUM | `telegram_panel/storage/encryption.py`
**Finding:** `decrypt()` returns `None` silently on failure  
**Evidence:** Line 80 returns `None` with only a log entry. Callers do not consistently handle `None` return — they may pass `None` to MetaAPI credential fields.  
**Risk:** Silent fallback to `None` credentials causes MetaAPI connection failure, not a security breach. But the failure mode is opaque.  

### B-04 | INFO | `telegram_panel/storage/encryption.py`
**Finding:** Fernet provides authenticated encryption (AES-128-CBC + HMAC-SHA256)  
**Evidence:** Fernet spec — the `decrypt()` will raise `InvalidToken` if ciphertext is tampered. This is correctly caught.  
**Risk:** None. IV/nonce is managed by Fernet internally.  

---

## SECTION C — AUTHENTICATION & AUTHORISATION

### C-01 | LOW | `telegram_panel/api/middleware/auth.py`
**Finding:** In-memory permission cache TTL = 30 seconds  
**Evidence:** Cache is keyed by `telegram_id` with a 30-second TTL. A revoked admin retains access for up to 30 seconds after revocation.  
**Risk:** In a Telegram bot context where commands are manually sent, 30 seconds is operationally negligible.  
**Remediation:** Acceptable as-is for this use case.  

### C-02 | MEDIUM | `telegram_panel/api/middleware/auth.py`
**Finding:** Authorization is enforced at the handler decorator level — no middleware-level blanket authentication  
**Evidence:** Each handler uses `@require_auth` or `@require_permission` decorators. If a handler is added without the decorator, it is publicly accessible.  
**Risk:** Moderate — requires developer error to exploit. Any Telegram user who knows the bot token can message the bot.  
**Remediation:** Add a global pre-handler middleware that rejects all updates from non-whitelisted users before routing.  

### C-03 | LOW | `telegram_panel/security/session_manager.py`
**Finding:** Session expiry enforced on `get_or_create_session()` — sessions are not actively garbage-collected  
**Evidence:** Expired sessions remain in the database. `is_expired()` check is passive. The `session_repo` does not have a background cleanup job.  
**Risk:** SQLite table growth over time. Not a security risk since expired sessions are rejected on access.  

### C-04 | INFO | Authentication model
**Finding:** Authentication is based on `telegram_id` whitelist (owner + admins)  
**Evidence:** `settings.telegram.owner_id`, `settings.telegram.admin_ids`. Telegram IDs are permanent and non-guessable for random users.  
**Risk:** Telegram ID spoofing is not possible at the Telegram API level. Authentication model is appropriate.  

---

## SECTION D — INPUT VALIDATION

### D-01 | LOW | `telegram_panel/api/handlers/*.py`
**Finding:** Callback data values from Telegram inline keyboards are not re-validated server-side for object ownership  
**Evidence:** Handlers receive `callback_data` strings and use them directly to query the database (e.g., account ID in callback). A user could theoretically craft callback data to access a different account's data.  
**Risk:** All users are pre-authenticated via whitelist. An admin could access another admin's data. Cross-user data isolation is not enforced at the query level.  
**Remediation:** Validate that the requested resource (account ID) belongs to the requesting user's scope before returning data.  

### D-02 | LOW | `live_trading/utils/state_writer.py`
**Finding:** `robot_commands.json` is parsed with `json.load()` — no schema validation  
**Evidence:** `read_commands()` returns whatever JSON is in the file. If the JSON is malformed (partial write race), an empty dict is returned (exception caught). If the JSON is valid but contains unexpected keys, those keys are silently ignored.  
**Risk:** An attacker who can write to the filesystem could inject arbitrary command keys. However, only known keys (`pause`, `resume`, `stop`, `close_all`, `reset_guardian`) are acted upon in `_process_commands`.  
**Remediation:** Whitelist-validate command keys against the known set before processing.  

### D-03 | INFO | No shell injection risk
**Finding:** No `os.system()`, `subprocess.call()`, or shell-execution calls found in any module  
**Evidence:** Searched all Python files — no subprocess or shell execution.  

---

## SECTION E — DEPLOYMENT SURFACE

### E-01 | HIGH | `render.yaml`
**Finding:** Guardian env vars absent from `render.yaml`  
**Evidence:** `DAILY_LOSS_LIMIT_PCT`, `MAX_DRAWDOWN_PCT`, and `SLIPPAGE_POINTS` are not listed in `render.yaml`. They will silently use hardcoded defaults (3%, 8%, 30 points) from `config.py`. A user who deploys on Render and does not manually add these env vars may not realize the Guardian thresholds are active with those specific values.  
**Risk:** Not a security issue per se, but a misconfiguration risk that could cause unexpected Guardian halts or, more dangerously, under-protected accounts.  
**Remediation:** Add these three env vars to `render.yaml` with `sync: false` so the user is prompted to set them explicitly.  
**Safe to fix:** YES.  

### E-02 | MEDIUM | Filesystem IPC — `robot_state.json`, `robot_commands.json`
**Finding:** No file integrity verification on IPC JSON files  
**Evidence:** `live_loop.py` reads `robot_commands.json` via `read_commands()` which has no HMAC or signature check. Any process with filesystem write access can inject commands.  
**Risk:** On shared hosting or improperly configured containers, another process could inject `{"stop": true}` and halt trading. On Render/Docker with single-tenant containers this risk is low.  
**Remediation:** On single-tenant cloud workers (Render), this is acceptable. On shared systems, add filesystem permission restrictions (chmod 600).  

### E-03 | LOW | `Procfile`
**Finding:** No user privilege dropping  
**Evidence:** `web: python -m live_trading.main` runs as container default user. No `su` or `--user` argument.  
**Risk:** Acceptable in a container environment where isolation is provided by the container runtime.  

### E-04 | INFO | `telegram_panel/setup.sh`
**Finding:** Setup script uses `pip install` without `--require-hashes` or version pinning  
**Evidence:** `pip install -r requirements.txt` with unpinned requirements. A supply-chain attack on PyPI could inject malicious packages.  
**Risk:** Low in practice for well-known packages (`python-telegram-bot`, `aiosqlite`, `cryptography`) but present in principle.  

---

## SECTION F — LOGGING & AUDIT TRAIL

### F-01 | MEDIUM | `telegram_panel/security/audit.py`
**Finding:** `old_value` and `new_value` fields in audit log may contain sensitive data in plaintext  
**Evidence:** `log_action(old_value=..., new_value=...)` is called when settings are updated via the panel. If a user updates an MT5 password via the panel, the old and new (plaintext) passwords are passed to the audit logger.  
**Risk:** Audit log in SQLite contains plaintext sensitive values. Anyone with DB read access can recover old credentials.  
**Remediation:** Mask or hash `old_value`/`new_value` for actions involving credential fields (`password`, `token`, `key`).  
**Safe to fix:** YES — logging only, zero trading impact.  

### F-02 | LOW | `live_trading/logger.py`
**Finding:** File handler creation failure is silently swallowed  
**Evidence:** `except Exception: pass` at line 32. If the log directory is full or permissions are wrong, only console logging works. No alert is raised.  
**Risk:** Reduced observability. Not a security risk.  

### F-03 | INFO | Audit trail integrity  
**Finding:** Audit logs are standard SQLite rows — no cryptographic tamper evidence  
**Evidence:** `audit_logs` table has no hash chain or append-only constraint. A user with DB write access can modify or delete audit records.  
**Risk:** For regulatory environments this is insufficient. For a personal trading bot it is adequate.  

### F-04 | INFO | Log rotation  
**Finding:** `panel.log` has rotation (10 MB, 5 backups) but `live_trading/robot.log` rotation is not explicitly configured  
**Evidence:** `live_trading/logger.py` uses `FileHandler`, not `RotatingFileHandler`. On a long-running deployment, `robot.log` can grow indefinitely.  
**Risk:** Disk exhaustion on long deployments (months).  
**Remediation:** Switch to `RotatingFileHandler` in `live_trading/logger.py`.  

---

## SECURITY RISK SUMMARY

| ID | Severity | Area | Finding | Remediation |
|----|----------|------|---------|-------------|
| A-01 | HIGH | Encryption | Base64 fallback for credentials | Fail hard if key missing |
| B-01 | HIGH | Encryption | No KDF — raw key required | Document key generation |
| B-02 | HIGH | Encryption | No key rotation procedure | Implement re-encryption command |
| E-01 | HIGH | Deployment | Guardian env vars absent from render.yaml | Add to render.yaml |
| C-02 | MEDIUM | Auth | No global auth middleware | Add blanket pre-handler check |
| B-03 | MEDIUM | Encryption | decrypt() returns None silently | Validate callers handle None |
| D-01 | MEDIUM | Validation | Callback data not ownership-validated | Scope queries to user |
| F-01 | MEDIUM | Audit | Plaintext values in audit log | Mask sensitive fields |
| A-02 | MEDIUM | Secrets | Secret key names exposed in error logs | Generalize error message |
| C-01 | LOW | Auth | 30-second permission cache | Acceptable for use case |
| C-03 | LOW | Auth | Expired sessions not GC'd | Add background cleanup |
| D-02 | LOW | Validation | Commands file not schema-validated | Whitelist known keys |
| F-02 | LOW | Logging | Log handler failure silently swallowed | Alert on failure |
| F-04 | LOW | Logging | robot.log not size-bounded | Use RotatingFileHandler |
| E-03 | LOW | Deployment | No privilege dropping | Acceptable in containers |
| A-03 | INFO | Secrets | Non-secret values in render.yaml | No action needed |
| C-04 | INFO | Auth | telegram_id whitelist model adequate | No action needed |
| D-03 | INFO | Validation | No shell injection paths | No action needed |
| B-04 | INFO | Encryption | Fernet provides authenticated encryption | No action needed |
| F-03 | INFO | Audit | No cryptographic tamper evidence | Acceptable for use case |

---

## CRITICAL SECURITY PREREQUISITE FOR PRODUCTION

Before deploying with real money, the following MUST be satisfied:

1. **`PANEL_ENCRYPTION_KEY` MUST be set** and generated via `--generate-key`. Operating without it means all broker credentials are stored as recoverable base64.
2. **`cryptography` package MUST be installed** in the panel environment.  
3. **`robot_commands.json` permissions** MUST be set to `chmod 600` — readable only by the robot process owner.
4. **Audit log `old_value`/`new_value`** for credential updates MUST be masked.

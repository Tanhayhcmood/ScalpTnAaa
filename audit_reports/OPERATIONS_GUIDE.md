# OPERATIONS GUIDE
## GoldScalperPro v4 — Operator Runbook
**Date:** 2026-07-19
**Audience:** Production operators managing a live GoldScalperPro deployment

---

## 1. DAILY OPERATIONS

### 1.1 Morning Checks (before market open)

Run these checks every trading day before the market opens (Sunday 22:00 UTC for XAUUSD):

```bash
# 1. Robot is alive and connected
tail -20 /data/goldscalper/robot.log
# Expected: recent "Waiting for new M5 bar" or "Bar #N" entries

# 2. Guardian state is clear (not halted)
python3 -c "
import json
with open('/data/goldscalper/robot_state.json') as f:
    s = json.load(f)
g = s.get('guardian', {})
print(f'Guardian halted: {g.get(\"halted\", \"unknown\")}')
print(f'Daily PnL: {g.get(\"daily_pnl\", 0):+.2f}')
print(f'Drawdown: {g.get(\"drawdown_pct\", 0):.2f}%')
"

# 3. Disk space (if on a persistent disk)
df -h /data/goldscalper

# 4. No ERROR lines in last 100 log entries
grep -c "ERROR" /data/goldscalper/robot.log
```

### 1.2 During Trading Hours

Check Telegram bot for:
- Trade entry/exit notifications
- Guardian halt alerts
- Connection lost alerts

### 1.3 End of Day

```bash
# Review today's trades in robot_state.json
python3 -c "
import json
with open('/data/goldscalper/robot_state.json') as f:
    s = json.load(f)
trades = s.get('recent_trades', [])
today = [t for t in trades if t.get('bar_time', '').startswith('2024-')]  # adjust date
print(f'Trades today: {len(today)}')
for t in today:
    print(f\"  {t.get('direction')} conf={t.get('confidence')} rr={t.get('rr')}\")
"
```

---

## 2. TELEGRAM PANEL COMMAND REFERENCE

### Robot Control Commands

| Command | Action | Notes |
|---------|--------|-------|
| `/start` | Show dashboard | Entry point |
| `/status` | Robot status, balance, equity | |
| `/pause` | Stop new trade entries | Open positions unaffected |
| `/resume` | Resume trading | Blocked if Guardian halted |
| `/stop` | Graceful shutdown | Writes STOPPED state, closes MetaAPI |
| `/close_all` | Close all open positions | Requires confirmation |

### Risk Guardian Commands

| Command | Action | Notes |
|---------|--------|-------|
| `/reset_guardian` | Clear Guardian halt and resume | Use only after understanding halt reason |

### How to Reset a Guardian Halt

1. Check the halt reason: `/status` or inspect `robot_state.json` → `guardian.reason`
2. Understand WHY it halted (daily loss limit or drawdown — see logs)
3. If safe to resume: `/reset_guardian`
4. If NOT safe: leave halted. Daily loss halt auto-resets at UTC midnight.

### Guardian Halt Types

| Halt Type | Auto-Reset | Manual Reset |
|-----------|-----------|-------------|
| `DAILY_LOSS_LIMIT` | ✅ At UTC midnight | `/reset_guardian` |
| `MAX_DRAWDOWN` | ❌ Sticky | `/reset_guardian` only |

**Warning:** Never reset a `MAX_DRAWDOWN` halt without first understanding the cause. This halt is intentionally sticky — it fires when equity has dropped 8% below peak.

---

## 3. INCIDENT RESPONSE

### Incident: Robot Not Responding to Telegram Commands

**Diagnosis:**
```bash
# Check if robot process is running
systemctl status goldscalper-robot
# or: Render dashboard → check service status

# Check if commands file is being written
ls -la /data/goldscalper/robot_commands.json

# Check robot logs for errors
grep "ERROR\|CRITICAL" /data/goldscalper/robot.log | tail -20
```

**Resolution:**
1. If process is stopped: `systemctl start goldscalper-robot`
2. If process is running but unresponsive: check for a hung MetaAPI connection
3. Emergency: `systemctl restart goldscalper-robot` — safe; open positions protected by broker SL/TP

---

### Incident: Guardian Halted — Unexpected

**Diagnosis:**
```bash
python3 -c "
import json
with open('/data/goldscalper/robot_state.json') as f:
    s = json.load(f)
g = s.get('guardian', {})
print('Halt reason:', g.get('reason'))
print('Daily PnL:', g.get('daily_pnl'), '(', g.get('daily_pnl_pct'), '%)')
print('Drawdown:', g.get('drawdown_pct'), '%')
print('Equity peak:', g.get('equity_peak'))
print('Session balance:', g.get('session_open_balance'))
print('Triggered at:', g.get('triggered_at'))
"
```

**Resolution:**
1. Log into your broker platform and verify live positions
2. If losses are real and within acceptable range: `/reset_guardian` after understanding the cause
3. If losses are unexpectedly large: do NOT reset. Investigate the trades.

---

### Incident: MetaAPI Connection Lost

**Symptoms:** `robot.log` shows repeated "Reconnect attempt #N" entries. Panel shows DISCONNECTED.

**Robot behaviour:** Exponential backoff reconnect (30s, 60s, 120s, 240s, 300s cap). Open positions are protected by broker SL/TP.

**Diagnosis:**
```bash
# Check MetaAPI status
curl https://status.metaapi.cloud/api/v2/component-statuses | python -m json.tool

# Check connectivity
curl -I https://app.metaapi.cloud
```

**Resolution:**
1. If MetaAPI is down: wait. Robot will auto-reconnect when MetaAPI recovers.
2. If network issue: check VPS/Render network logs
3. If auth issue (token expired): update `METAAPI_TOKEN` in deployment env vars and restart

---

### Incident: Duplicate Order Suspected

This is the critical unproven risk (ST-02). If you suspect a duplicate order was opened:

1. **Immediately:** Log into your broker platform and check all open positions
2. Close any duplicate manually through the broker platform
3. Check `robot_state.json` → `recent_trades` for two entries with the same `bar_time`
4. Check `robot.log` for "SIGNAL" entries at the same time
5. Report the incident with timestamps and broker fill confirmation

---

### Incident: Panel Database Corrupted (panel.db)

**Symptoms:** Panel shows "Internal error" for all commands.

```bash
# Verify corruption
python3 -c "
import sqlite3
try:
    conn = sqlite3.connect('/data/goldscalper/panel.db')
    conn.execute('PRAGMA integrity_check')
    print('OK')
except Exception as e:
    print('CORRUPT:', e)
"
```

**Resolution:**
1. Stop the panel: `systemctl stop goldscalper-panel`
2. Restore from backup: `cp /data/goldscalper_backup_YYYYMMDD/panel.db /data/goldscalper/panel.db`
3. If no backup: delete `panel.db` — the panel will recreate an empty database. You will need to re-add all accounts.
4. Start the panel: `systemctl start goldscalper-panel`

---

## 4. BACKUP AND RECOVERY

### What to Back Up

| File | Frequency | Priority |
|------|----------|---------|
| `panel.db` | Daily | CRITICAL — accounts, encrypted credentials, audit log |
| `robot_state.json` | After each trade | HIGH — trade history |
| `robot.log` | Weekly | LOW — operational logs |
| `/etc/goldscalper/*.env` | On change | CRITICAL — credentials |

### Automated Backup Script

```bash
#!/bin/bash
# /opt/goldscalper-v4/scripts/backup.sh
# Run daily via cron: 0 3 * * * /opt/goldscalper-v4/scripts/backup.sh

BACKUP_DIR="/data/backups/goldscalper/$(date +%Y-%m-%d)"
DATA_DIR="/data/goldscalper"

mkdir -p "$BACKUP_DIR"
cp "$DATA_DIR/panel.db" "$BACKUP_DIR/"
cp "$DATA_DIR/robot_state.json" "$BACKUP_DIR/"
cp "$DATA_DIR/robot.log" "$BACKUP_DIR/"

# Keep only last 30 days
find /data/backups/goldscalper -maxdepth 1 -type d -mtime +30 -exec rm -rf {} +

echo "Backup completed: $BACKUP_DIR"
```

```bash
# Add to cron
crontab -e
# Add: 0 3 * * * /opt/goldscalper-v4/scripts/backup.sh
```

### Recovery from Backup

```bash
# Stop services
systemctl stop goldscalper-robot goldscalper-panel

# Restore
cp /data/backups/goldscalper/2024-01-15/panel.db /data/goldscalper/panel.db
cp /data/backups/goldscalper/2024-01-15/robot_state.json /data/goldscalper/robot_state.json

# Verify
python3 -c "import json; json.load(open('/data/goldscalper/robot_state.json'))"

# Start services
systemctl start goldscalper-robot goldscalper-panel
```

---

## 5. KEY ROTATION

The `PANEL_ENCRYPTION_KEY` should be rotated every 6–12 months or immediately if compromised.

### Key Rotation Procedure

**Warning:** All stored broker credentials must be re-entered after key rotation. There is no automated re-encryption tool.

```bash
# Step 1: Generate a new key
python3 -m telegram_panel.main --generate-key
# Copy the new key securely

# Step 2: Stop panel
systemctl stop goldscalper-panel

# Step 3: Backup panel.db
cp /data/goldscalper/panel.db /data/backups/panel_before_rotation_$(date +%Y%m%d).db

# Step 4: Update PANEL_ENCRYPTION_KEY in your env file or Render dashboard
# /etc/goldscalper/panel.env — set PANEL_ENCRYPTION_KEY=<new_key>

# Step 5: Start panel
systemctl start goldscalper-panel

# Step 6: Re-enter all broker accounts through the Telegram panel
# The panel will store new credentials with the new key
# Old encrypted values (b64: or old Fernet tokens) will fail to decrypt
# and must be re-entered

# Step 7: Test connectivity for each account via the panel
```

---

## 6. LOG ANALYSIS

### Log File Locations

| Log | Location | Rotation |
|-----|---------|---------|
| Robot log | `$LOG_FILE` (default: `live_trading/robot.log`) | 10 MB × 5 = 50 MB max |
| Panel log | `$PANEL_LOG_PATH` | 10 MB × 5 = 50 MB max |
| systemd (if using systemd) | `journalctl -u goldscalper-robot` | System journal rotation |

### Key Log Patterns to Monitor

```bash
# Successful trade entries
grep "🔔 SIGNAL" /data/goldscalper/robot.log

# Guardian halts
grep "GUARDIAN HALT\|AUTO-PAUSED" /data/goldscalper/robot.log

# MetaAPI connection issues
grep "MetaAPI\|Reconnect\|DISCONNECTED" /data/goldscalper/robot.log | tail -50

# Errors in last 24 hours
grep "ERROR\|CRITICAL" /data/goldscalper/robot.log | grep "$(date +%Y-%m-%d)"

# Duplicate candle warnings (indicates MetaAPI SDK version issue)
grep "duplicate candle" /data/goldscalper/robot.log

# Disk full warning for file logging
grep "WARNING.*could not set up file logging" /data/goldscalper/robot.log
```

### What a Healthy Log Looks Like

```
2024-01-15 10:00:01  INFO      ─── Bar #42 at 2024-01-15T10:00:00+00:00 ───
2024-01-15 10:00:03  DEBUG     Fetched 300 candles for XAUUSD/5m
2024-01-15 10:00:04  INFO      No trade → Insufficient confirmations: 2/3
2024-01-15 10:00:04  INFO      State: SCANNING
```

```
2024-01-15 10:15:01  INFO      ─── Bar #43 at 2024-01-15T10:15:00+00:00 ───
2024-01-15 10:15:03  INFO      🔔 SIGNAL BUY  conf=82.5%  lot=0.01  SL=1985.00 TP=2015.00  R:R=2.00  slippage≤30pts
2024-01-15 10:15:05  INFO      State: RUNNING
```

---

## 7. MONITORING

### Current Monitoring Gaps (known limitations)

| Gap | Impact | Workaround |
|-----|--------|-----------|
| No HTTP /health endpoint | Render/systemd cannot verify robot is alive (only not-crashed) | Check log timestamps every morning |
| No structured metrics | Cannot alert on trade frequency or error rate | Grep logs manually |
| No external monitoring service | No SMS/email alert if robot silently hangs | Set up UptimeRobot to ping your VPS |

### Minimum Viable External Health Check

Add this to the robot to create a heartbeat file:

```bash
# Check if robot wrote its state in the last 10 minutes
LAST_MODIFIED=$(stat -c %Y /data/goldscalper/robot_state.json 2>/dev/null || echo 0)
NOW=$(date +%s)
AGE=$((NOW - LAST_MODIFIED))
if [ $AGE -gt 600 ]; then
    echo "ALERT: robot_state.json not updated in ${AGE}s"
    # Send alert via curl to webhook, email, etc.
fi
```

Add to cron: `*/5 * * * * /opt/goldscalper-v4/scripts/health_check.sh`

---

## 8. STOPPING FOR MAINTENANCE

### Safe Stop (preserves open positions at broker)

```bash
# Via Telegram panel (recommended)
# Send /stop to your bot

# Or via systemd
systemctl stop goldscalper-robot
# Robot sends SIGTERM → _run_loop finally block runs → disconnect() called → STOPPED state written
```

### Emergency Stop (all positions at broker remain open with SL/TP)

```bash
# SIGKILL — no finally block runs
systemctl kill -s SIGKILL goldscalper-robot

# Open positions are protected by broker-side SL/TP
# Log into your broker platform to manage open positions manually
```

### Close All Before Maintenance

1. Via Telegram: `/close_all` → confirm
2. Verify all positions closed in broker platform
3. Then `/stop`

---

*GoldScalperPro v4 Stable — Operations Guide — 2026-07-19*

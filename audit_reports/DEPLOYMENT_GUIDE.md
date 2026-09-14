# DEPLOYMENT GUIDE
## GoldScalperPro v4 — Production Deployment
**Date:** 2026-08-10 (rewritten for v4.0.4-stable — supersedes the 2026-07-19 revision below, which
described an earlier MetaAPI-cloud / persistent-disk architecture that Render is not actually running)
**Scope:** Render.com (current production setup), VPS/Linux (systemd), Docker Compose

> **What changed since 2026-07-19:** the live system no longer uses MetaAPI cloud — it connects to MT5
> directly through the `mtapi-bridge` Docker service (`mt5rest`). It also does not use a Render persistent
> disk; state durability across restarts comes from a required Redis instance instead (see below). The two
> Python services run as Render **Web Services** (with `/health` endpoints), not Background Workers. The
> systemd/Docker Compose sections further down this file are unaffected by this correction and remain valid
> as generic alternatives, except where they also assume MetaAPI/`/data` — adjust env vars accordingly.

---

## PRE-DEPLOYMENT CHECKLIST

Complete ALL items before deploying with real capital.

### Mandatory
- [ ] Paper trading run on a demo MT5 account completed (no crashes, no double entries)
- [ ] `mtapi-bridge` deployed and reachable, logged into the target MT5 broker account
- [ ] `PANEL_ENCRYPTION_KEY` generated: `python -m telegram_panel.main --generate-key`
- [ ] `ROBOT_COMMAND_TOKEN` set to the same random secret on both the robot and panel services
- [ ] A Redis instance is provisioned in the **same region** as both web services (see Step 3)
- [ ] All env vars reviewed and set (do NOT use defaults blindly for risk settings)
- [ ] Engineering tests pass: `python -m pytest tests/ -v`
- [ ] Dependencies installed from pinned requirements on a clean environment

### Guardian Threshold Review
Before live deployment, explicitly decide on these values (do not use defaults without consideration —
the current production values are shown for reference, they are not necessarily right for a new account):

| Variable | Code default | Current production value | Your decision |
|----------|---------|---------|---------------|
| `DAILY_LOSS_LIMIT_PCT` | 3.0% | 4.0% | _______ |
| `MAX_DRAWDOWN_PCT` | 8.0% | 12.0% | _______ |
| `MIN_CONFIRMATIONS` | 2 | 2 | _______ |
| `CONF_HARD_MIN` | — | 32 | _______ |
| `SLIPPAGE_POINTS` | 30 | 30 | _______ |
| `RISK_PERCENT` | 1.0% | 1.0% | _______ |

---

## OPTION 1 — Render.com Deployment (Blueprint, `render.yaml`)

### Architecture on Render (as actually deployed)

```
Render Project (region must match for all services below)
├── goper-v4-robot   (Web Service — python live_trading/server.py, healthcheck /health)
├── anel   (Web Service — python telegram_panel/server.py, healthcheck /health)
├── ger-mtapi      (Docker Web Service — Wine + MT5 + mt5rest, healthcheck /Ping)
└── ger-redis      (Render managed Redis, internal network only)

No persistent disk is attached or required. Each Python service writes its working files
(robot_state.json, guardian_state.json, robot_commands.json, panel.db, logs) to its own ephemeral
/tmp — these are LOST on every restart/redeploy, and that is fine for state that only needs to
survive within a single running instance. The one thing that DOES need to survive a restart —
the RiskGuardian's halt/baseline state and cross-service commands — is mirrored to Redis by the
application itself, which is why Redis is a required dependency, not an optional cache.
```

### Step 1 — Push to GitHub

```bash
git init
git add .
git commit -m "GoldScalperPro v4 Stable"
git remote add origin https://github.com/your-org/goldscalper-v4.git
git push -u origin main
```

### Step 2 — Create Render Project

1. Go to [render.com](https://render.com) → New → Blueprint
2. Connect your GitHub repository
3. Render auto-detects `render.yaml` and creates all four services (robot, panel, mtapi bridge, Redis)

### Step 3 — Verify Region Consistency

**Critical:** all four services must be created in the same Render region. If the Redis instance ends up
in a different region than the robot/panel web services, its internal hostname will not resolve and both
`redis_send_command()` and the Guardian's Redis mirror will silently fail (commands and halt-state will
stop surviving restarts, without a hard crash). Check the region on each service in the Render dashboard
before going live.

### Step 4 — File Paths (already set in `render.yaml`, no action needed)

`render.yaml` already points every file-based path at `/tmp/...` for both the robot and the panel. There is
no persistent disk to mount and no extra path env vars to set — this is a deliberate choice, not a gap (see
architecture note above).

### Step 5 — Set Secrets

`render.yaml` declares these as `sync: false`, meaning Render will prompt for them in the dashboard the
first time you deploy the Blueprint — they are never committed to the repo:

**Robot service (`goper-v4-robot`):**
- `MT5_USER`, `MT5_PASSWORD` — your MT5 broker login/password
- `ROBOT_COMMAND_TOKEN` — a random shared secret (also set on the panel, must match)
- `REDIS_URL` — auto-filled by Render from the `ger-redis` service via `fromService`

**Panel service (`anel`):**
- `TELEGRAM_BOT_TOKEN` — from @BotFather
- `TELEGRAM_OWNER_ID` — your Telegram numeric user ID
- `PANEL_ENCRYPTION_KEY` — from `python -m telegram_panel.main --generate-key`
- `ROBOT_COMMAND_TOKEN` — must match the robot service's value
- `MT5_USER`, `MT5_PASSWORD` — same broker credentials as the robot

**mtapi bridge (`ger-mtapi`, Docker):**
- `MT5_USER`, `MT5_PASSWORD` — same broker credentials, used by the MT5 terminal running inside the container

### Step 6 — Deploy and Verify

1. Click Deploy
2. Watch robot service logs for: `✅ MetaAPI connected and synchronized`
3. Send `/start` to your Telegram bot
4. Verify bot responds with dashboard

---

## OPTION 2 — VPS / Linux (systemd)

### Prerequisites

```bash
# Python 3.11
sudo apt update
sudo apt install python3.11 python3.11-venv python3-pip
```

### Installation

```bash
# 1. Clone or copy project
cd /opt
git clone https://github.com/your-org/goldscalper-v4.git
cd goldscalper-v4

# 2. Create virtual environments
python3.11 -m venv venv_robot
python3.11 -m venv venv_panel

# 3. Install dependencies
./venv_robot/bin/pip install -r live_trading/requirements.txt
./venv_panel/bin/pip install -r telegram_panel/requirements.txt

# 4. Create data directory
mkdir -p /data/goldscalper
chmod 700 /data/goldscalper
```

### Environment File

```bash
# /etc/goldscalper/robot.env
METAAPI_TOKEN=your_token
METAAPI_ACCOUNT_ID=your_account_id
SYMBOL=XAUUSD
RISK_PERCENT=1.0
MIN_CONFIRMATIONS=3
DAILY_LOSS_LIMIT_PCT=3.0
MAX_DRAWDOWN_PCT=8.0
SLIPPAGE_POINTS=30
STATE_FILE=/data/goldscalper/robot_state.json
MT5_SNAPSHOT=/data/goldscalper/robot_mt5_snapshot.json
COMMANDS_FILE=/data/goldscalper/robot_commands.json
LOG_FILE=/data/goldscalper/robot.log
```

```bash
# /etc/goldscalper/panel.env
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_OWNER_ID=123456789
PANEL_ENCRYPTION_KEY=your_fernet_key
PANEL_DB_PATH=/data/goldscalper/panel.db
ROBOT_STATE_PATH=/data/goldscalper/robot_state.json
ROBOT_LOG_PATH=/data/goldscalper/robot.log
PANEL_LOG_PATH=/data/goldscalper/panel.log
```

```bash
chmod 600 /etc/goldscalper/robot.env
chmod 600 /etc/goldscalper/panel.env
```

### Systemd Service Files

```ini
# /etc/systemd/system/goldscalper-robot.service
[Unit]
Description=GoldScalperPro v4 Live Trading Robot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=goldscalper
Group=goldscalper
WorkingDirectory=/opt/goldscalper-v4
EnvironmentFile=/etc/goldscalper/robot.env
ExecStart=/opt/goldscalper-v4/venv_robot/bin/python -m live_trading.main
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal
# Auto-restart on non-zero exit (e.g. MetaAPI auth failure)
# Render equivalent of this is handled by Render's restart policy

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/goldscalper-panel.service
[Unit]
Description=GoldScalperPro v4 Telegram Control Panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=goldscalper
Group=goldscalper
WorkingDirectory=/opt/goldscalper-v4
EnvironmentFile=/etc/goldscalper/panel.env
ExecStart=/opt/goldscalper-v4/venv_panel/bin/python -m telegram_panel.main
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable goldscalper-robot goldscalper-panel
sudo systemctl start goldscalper-robot
sudo systemctl start goldscalper-panel

# Verify
sudo systemctl status goldscalper-robot
sudo journalctl -u goldscalper-robot -f
```

---

## OPTION 3 — Docker Compose

```yaml
# docker-compose.yml
version: "3.9"

services:
  robot:
    image: python:3.11-slim
    working_dir: /app
    volumes:
      - .:/app
      - goldscalper_data:/data
    env_file:
      - .env.robot
    command: >
      sh -c "pip install -r live_trading/requirements.txt &&
             python -m live_trading.main"
    restart: on-failure:5

  panel:
    image: python:3.11-slim
    working_dir: /app
    volumes:
      - .:/app
      - goldscalper_data:/data
    env_file:
      - .env.panel
    command: >
      sh -c "pip install -r telegram_panel/requirements.txt &&
             python -m telegram_panel.main"
    restart: on-failure:5

volumes:
  goldscalper_data:
    driver: local
```

```bash
# Run
docker-compose up -d

# Logs
docker-compose logs -f robot
docker-compose logs -f panel
```

---

## PERSISTENT STORAGE — CURRENT PRODUCTION APPROACH

**Correction (2026-08-10):** the current deployment does NOT use a Render persistent disk, and this is
intentional rather than a limitation to work around. Render's filesystem for each web service is ephemeral
(wiped on every restart/redeploy); the application handles this by mirroring the state that actually needs
to survive a restart to Redis, and treating everything else as safe to lose:

| Data | Where it lives | Impact of a restart |
|------|-----------------|---------------------|
| `guardian_state.json` (halt flag, daily/session baselines) | **Redis** (durable) + `/tmp` (local cache) | Survives restart — this is the one that matters for safety |
| `robot_commands.json` (panel → robot commands) | **Redis** (durable) + `/tmp` (local cache) | Survives restart |
| `robot_state.json`, `robot_mt5_snapshot.json` | `/tmp` only | Lost on restart; robot re-syncs from the broker within one bar — acceptable |
| `panel.db` (Telegram accounts, sessions, audit log) | `/tmp` only, SQLite | **Lost on every panel restart** — see note below |
| `robot.log` / `panel.log` | `/tmp` only | Lost on restart; use Render's own log retention/streaming for history |

**Known gap:** `panel.db` is not currently mirrored anywhere durable. If you need Telegram panel accounts,
sessions, or its audit log to survive a panel restart, you must either attach a Render persistent disk to
the panel service and set `PANEL_DB_PATH=/data/panel.db`, or migrate it to Postgres/Redis — this repository
does not yet do either. This is a real limitation of the current stable version, not something this
release-preparation pass fixed (fixing it would mean touching application behavior, which was out of scope
here); flagging it for whoever picks this up next.

---

## INSTALLATION VERIFICATION

After deploying, verify these are working before enabling live capital:

```bash
# 1. Robot is connected to the mtapi-bridge and MT5
# Look for this in robot logs (Render dashboard → goper-v4-robot → Logs):
✅ Connected to mt5rest bridge, MT5 account synchronized

# 2. Guardian is initialized
# Look for:
Guardian initialized: balance=XXXX equity=XXXX

# 3. Robot is scanning bars
# Look for (every timeframe tick):
─── Bar #N at 2024-01-15T10:00:00+00:00 ───

# 4. Panel responds
# Send /status to your Telegram bot — should show robot status

# 5. Health endpoints respond (used by Render's own health checks too)
curl https://<your-robot-service>.onrender.com/health
curl https://<your-panel-service>.onrender.com/health
curl https://<your-mtapi-service>.onrender.com/Ping

# 6. Guardian/command state is actually reaching Redis (not just /tmp)
# From the Render dashboard, open a shell on either service and run:
python -c "from live_trading.redis_ipc import _get_client; print(_get_client().ping())"
```

---

## UPGRADE PROCEDURE

```bash
# 1. Stop both services
sudo systemctl stop goldscalper-robot goldscalper-panel
# or: Render dashboard → Suspend services

# 2. Backup current state (Render/no persistent disk: pull it out of Redis first, e.g. via a
# one-off shell on the robot service, since /tmp will not survive the redeploy anyway)
python -c "from live_trading.redis_ipc import _get_client; import json; print(json.dumps(json.loads(_get_client().get('goldscalper:guardian') or '{}'), indent=2))" > guardian_state_backup_$(date +%Y%m%d).json
# VPS/systemd with a real /data mount:
# cp -r /data/goldscalper /data/goldscalper_backup_$(date +%Y%m%d)

# 3. Pull new code
cd /opt/goldscalper-v4
git pull origin main

# 4. Re-install dependencies (new versions may be pinned)
./venv_robot/bin/pip install -r live_trading/requirements.txt
./venv_panel/bin/pip install -r telegram_panel/requirements.txt

# 5. Run engineering tests
./venv_robot/bin/python -m pytest tests/ -v

# 6. Start services
sudo systemctl start goldscalper-robot goldscalper-panel

# 7. Verify
sudo journalctl -u goldscalper-robot -n 50
```

# GoldScalperPro Telegram Control Panel

A professional, enterprise-grade Telegram administration panel for the **GoldScalperPro v4** trading robot. Designed as a zero-invasive plugin — the existing robot continues to operate exactly as before.

---

## Architecture

```
telegram_panel/              ← Entire panel lives here
├── config/                  ← All settings (zero hardcoding)
│   ├── settings.py          ← Settings dataclasses (env-first)
│   ├── constants.py         ← Enums, icons, timing constants
│   └── panel.json.example   ← Config template
├── models/                  ← Pure data models (no DB logic)
│   ├── account.py
│   ├── user.py
│   ├── trade.py
│   ├── notification.py
│   ├── report.py
│   ├── session.py
│   ├── audit.py
│   ├── risk_config.py
│   └── strategy_config.py
├── storage/                 ← SQLite persistence (repository pattern)
│   ├── database.py          ← Schema, connection management
│   ├── encryption.py        ← Fernet encryption for credentials
│   └── repositories/        ← One repository per aggregate
├── services/                ← Business logic (no Telegram awareness)
│   ├── robot_service.py     ← Interface to trading engine (file-based)
│   ├── mt5_service.py       ← MT5 snapshot reader
│   ├── account_service.py
│   ├── trade_service.py
│   ├── risk_service.py
│   ├── strategy_service.py
│   ├── report_service.py
│   ├── system_service.py
│   └── notification_service.py
├── api/                     ← Telegram-specific code
│   ├── handlers/            ← One handler class per section
│   ├── keyboards/           ← All InlineKeyboardMarkup layouts
│   ├── formatters/          ← Unicode card message formatting
│   ├── middleware/          ← Auth, rate limiting
│   └── router.py            ← Central callback dispatcher
├── core/                    ← Bot lifecycle, events, heartbeat
│   ├── bot.py               ← Dependency injection composition root
│   ├── event_bus.py         ← Async pub/sub event bus
│   └── heartbeat.py         ← Robot health monitor
├── security/                ← Auth, audit, sessions
├── main.py                  ← Entry point
└── requirements.txt
```

---

## Integration with the Existing Robot

The panel communicates with the trading engine via **state files** — no source code changes required:

| File | Direction | Purpose |
|------|-----------|---------|
| `robot_state.json` | Robot → Panel | Robot writes status, active trades, uptime |
| `robot_commands.json` | Panel → Robot | Panel writes command queue; robot polls it |
| `robot_mt5_snapshot.json` | Robot → Panel | Robot writes live MT5 account/position data |
| `robot_trade_commands.json` | Panel → Robot | Trade management commands (close, modify SL/TP) |

**Robot code zero changes required.** Add file-writing to the robot optionally to enable live data. Without the files, the panel shows clean "disconnected" state.

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r telegram_panel/requirements.txt
```

### 2. Create your bot

1. Message [@BotFather](https://t.me/botfather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the API token

### 3. Get your Telegram ID

Message [@userinfobot](https://t.me/userinfobot) — it will reply with your numeric ID.

### 4. Configure

```bash
# Minimum required
export TELEGRAM_BOT_TOKEN="your_token_here"
export TELEGRAM_OWNER_ID="123456789"

# Recommended: enable encrypted credential storage
python -m telegram_panel.main --generate-key
export PANEL_ENCRYPTION_KEY="<key from above>"
```

Or copy `telegram_panel/config/panel.json.example` to `telegram_panel/config/panel.json` and edit it.

### 5. Run

```bash
# From project root
python -m telegram_panel.main

# Or with a config file
python -m telegram_panel.main --config telegram_panel/config/panel.json
```

---

## Features

### 📊 Dashboard
- Robot status, broker connection, MT5 status
- Live balance, equity, margin, floating profit
- Today's P&L, drawdown, active trades, pending orders
- VPS/system CPU & RAM at a glance
- Last heartbeat timestamp

### 👤 Account Management
- Unlimited accounts (Real, Demo, Prop Firm)
- Secure encrypted credential storage
- Add / Delete / Enable / Disable / Switch
- Connection test & reconnect
- Per-account stats overlay

### 🤖 Robot Control
- Start / Pause / Resume / Emergency Stop
- Restart Engine / MT5 / Telegram Bot
- Safe Shutdown
- All destructive actions require confirmation

### 💹 Trade Management
- View all open positions with live P&L
- View pending orders
- Close individual positions
- Partial close, Move SL/TP, Break Even, Trailing Stop
- Bulk close: All / Buy / Sell / Profitable / Losing

### 🧠 Strategy Control
- Toggle each SMC component individually:
  SMC, BOS, CHoCH, Order Blocks, Liquidity, FVG, Mitigation,
  Sessions, Trend Filter, Volume Filter, News Filter, Time Filter, Spread Filter
- Changes push to robot on next tick (no restart needed)

### 🛡️ Risk Management
- Change Risk %, Lot Size, Daily Loss, Max Trades, Max Spread
- Change Max Drawdown, R:R, SL/TP, Auto BE, Auto Trail
- Validation before saving, confirmation prompts for destructive values

### 📋 Reports
- Daily / Weekly / Monthly aggregated performance
- Total trades, Win rate, Avg R:R, Net profit, Drawdown
- Best/Worst trade, Profit Factor, Pips
- Export any period to CSV (sent as Telegram document)

### 🔔 Notifications
Individually configurable per user:
- Trade opened / closed
- SL hit / TP hit
- Daily target / Daily loss
- Connection lost / restored
- News pause, Errors, Warnings
- System restart, Heartbeat

### 💻 System
- CPU, RAM, Disk, Network, Latency
- Internet connectivity, Broker ping
- Robot log tail (last 50 lines)
- Uptime display (panel + robot)

### 🔒 Security
- Owner ID is always admin (cannot be removed via bot)
- Role-based access: Owner / Admin / Viewer / Blocked
- Audit log for every action (90-day retention)
- Rate limiting (30 req/60s per user; owner exempt)
- Session timeout (configurable, default 60 min)
- Encrypted credential storage (Fernet AES)
- Unauthorized access logging and alerts

---

## Configuration Reference

All settings can be provided via environment variables (highest priority) or `panel.json`.

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | — | **Required** BotFather token |
| `TELEGRAM_OWNER_ID` | — | **Required** Owner's Telegram ID |
| `TELEGRAM_ADMIN_IDS` | — | Comma-separated admin IDs |
| `PANEL_ENCRYPTION_KEY` | — | Fernet encryption key (generate with `--generate-key`) |
| `PANEL_DB_PATH` | `telegram_panel/storage/data/panel.db` | SQLite database location |
| `ROBOT_STATE_PATH` | `robot_state.json` | Robot state file |
| `ROBOT_CONFIG_PATH` | `robot_config.json` | Robot config file |
| `ROBOT_LOG_PATH` | `logs/robot.log` | Robot log file path |
| `ROBOT_INTERFACE_MODE` | `file` | `file` / `http` / `socket` |
| `HEARTBEAT_INTERVAL_SECONDS` | `30` | Heartbeat check frequency |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `SESSION_TIMEOUT_MINUTES` | `60` | User session expiry |
| `DEBUG` | `0` | Set to `1` for verbose output |

---

## Robot Integration (Optional)

To enable live data in the panel, have your robot write these JSON files:

### `robot_state.json`
```json
{
  "status": "running",
  "version": "v4.0.0",
  "uptime_seconds": 3600,
  "last_heartbeat": "2024-01-15T10:30:00",
  "connection_status": "connected",
  "mt5_status": "connected",
  "active_trades": 2,
  "pending_orders": 0,
  "last_error": null
}
```

### `robot_mt5_snapshot.json`
```json
{
  "account_info": {
    "balance": 10000.00,
    "equity": 10250.50,
    "margin": 500.00,
    "free_margin": 9750.50,
    "floating_profit": 250.50,
    "currency": "USD",
    "leverage": 100,
    "broker": "ICMarkets",
    "server": "ICMarkets-Demo02",
    "login": "12345678",
    "connection_status": "connected"
  },
  "open_positions": [],
  "pending_orders": [],
  "today_profit": 125.00,
  "floating_profit": 250.50,
  "drawdown": {
    "current": 0.0,
    "max": 150.00,
    "current_percent": 0.0,
    "max_percent": 1.5
  },
  "connection_status": "connected"
}
```

### `robot_commands.json` (written by panel; read by robot)
The panel appends command objects to this file. Your robot polls it on each tick:
```json
[
  {
    "command": "PAUSE",
    "payload": {},
    "issued_at": "2024-01-15T10:30:00"
  }
]
```
After processing, clear the file. Supported commands:
`START`, `PAUSE`, `RESUME`, `EMERGENCY_STOP`, `RESTART_ENGINE`, `RESTART_MT5`, `SAFE_SHUTDOWN`, `UPDATE_RISK`, `UPDATE_STRATEGY`

---

## Production Checklist

- [ ] `TELEGRAM_BOT_TOKEN` set
- [ ] `TELEGRAM_OWNER_ID` set to your Telegram ID
- [ ] `PANEL_ENCRYPTION_KEY` set (generate with `--generate-key`)
- [ ] `panel.json` NOT committed to version control
- [ ] `.env` NOT committed to version control
- [ ] Bot privacy mode configured (@BotFather → `/mybots` → Privacy)
- [ ] Log rotation configured
- [ ] Panel started as a system service (systemd/supervisor/PM2)
- [ ] Firewall allows outbound HTTPS (Telegram API)

---

## Running as a Service (systemd example)

```ini
# /etc/systemd/system/goldscalper-panel.service
[Unit]
Description=GoldScalperPro Telegram Control Panel
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/goldscalper
Environment=TELEGRAM_BOT_TOKEN=your_token
Environment=TELEGRAM_OWNER_ID=123456789
Environment=PANEL_ENCRYPTION_KEY=your_key
ExecStart=/usr/bin/python3 -m telegram_panel.main
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable goldscalper-panel
sudo systemctl start goldscalper-panel
sudo journalctl -u goldscalper-panel -f
```

---

## Design Principles

- **Zero coupling** — No imports from trading engine code
- **Clean Architecture** — Models → Repositories → Services → Handlers → Router
- **Repository Pattern** — All SQL in one place; swap DB without changing logic
- **Dependency Injection** — All dependencies resolved in `core/bot.py`
- **Event-driven notifications** — EventBus decouples trading events from notifications
- **Async-first** — Never blocks trading; all I/O non-blocking
- **Defense in depth** — Rate limiting + role checks + audit log + encryption
- **Config-first** — Zero hardcoded values anywhere in the codebase

---

*GoldScalperPro v4 · Telegram Panel v1.0*

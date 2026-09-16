# GoldScalperPro v4 Stable

**Production-grade gold scalping system for MetaTrader 5, connected directly via the `mt5rest` Docker bridge (no MetaAPI cloud dependency).**

> **Frozen Strategy.** All trading logic, signal engines, and risk thresholds are frozen at v4 Stable. This repository contains only engineering fixes — no strategy modifications.

---

## System Architecture

```
┌──────────────────────────────┐   Redis (+/tmp cache) ┌───────────────────────────┐
│  Live Trading Engine          │◄──────────────────────►│  Telegram Control Panel   │
│  live_trading/                │  robot_state.json      │  telegram_panel/          │
│  Python / asyncio             │  robot_commands.json   │  Python / python-tg-bot   │
│  (Render web service)         │  guardian_state.json   │  SQLite / aiosqlite       │
│                               │  (Redis is the durable  │  (Render web service)     │
│                               │   cross-service copy;   │                           │
│                               │   /tmp is an ephemeral  │                           │
│                               │   same-process cache)   │                           │
└──────────────┬────────────────┘                        └───────────────────────────┘
               │ HTTPS (mt5rest REST API)
               ▼
┌──────────────────────────────┐
│  mtapi-bridge (Docker)        │  Wine + MT5 terminal + a .NET REST wrapper
│  ger-mtapi service    │  exposes MT5 as an HTTP API (login/candles/orders)
└──────────────┬────────────────┘
               │
               ▼
┌──────────────────────────────┐
│  MetaTrader 5 Broker          │
│  (e.g. AMarkets-Demo)         │
└──────────────────────────────┘

┌──────────────────────────────┐
│  TypeScript Backtest Engine   │  (offline / development tool only)
│  robot/                       │  Uses backtestEngineV2 with real CSV data
└──────────────────────────────┘
```

**Redis is a required dependency, not optional.** It is the durable store for:
- Command IPC between the Telegram panel and the robot (`goldscalper:commands` key)
- The RiskGuardian's halt state (`day_open_balance`, `equity_peak`, halted flag) — this is what lets a halt (or a manual reset) survive a container restart/redeploy
- Robot state mirrored for the panel to read cross-service

The robot and panel run as separate Render web services with separate, ephemeral `/tmp` filesystems — they cannot share plain JSON files, hence Redis. **The Redis instance must be in the same Render region as both web services**, or its private hostname will not resolve (this exact misconfiguration was found and fixed — see `render.yaml` comment on the `ger-redis` service).

---

## Quick Start

### Prerequisites

- Python 3.11 (pinned — see `.python-version` and `PYTHON_VERSION` in `render.yaml`)
- A running `mtapi-bridge` instance (see `mtapi-bridge/`) reachable over HTTPS — this is what actually talks to MT5, no MetaAPI account needed
- MT5 broker account (demo recommended for first run)
- A Redis instance reachable from both the robot and the panel (Render's managed Redis, or any Redis 5+ compatible instance)

### Live Trading Engine

```bash
# 1. Install dependencies (exact versions — pinned for reproducibility)
pip install -r live_trading/requirements.txt

# 2. Configure environment
cp live_trading/.env.example .env
# Edit .env with your mtapi-bridge URL, MT5 broker credentials, and risk settings

# 3. Run
python live_trading/server.py
```

### Telegram Control Panel

```bash
# 1. Install dependencies
pip install -r telegram_panel/requirements.txt

# 2. Generate an encryption key (MANDATORY for production)
python -m telegram_panel.main --generate-key
# Copy the key to PANEL_ENCRYPTION_KEY env var

# 3. Configure environment
cp telegram_panel/.env.example .env
# Edit .env with your Telegram bot token, owner ID, and encryption key

# 4. Run (separate terminal from the robot)
python -m telegram_panel.main
```

---

## Required Environment Variables

Full reference with descriptions: [`live_trading/.env.example`](live_trading/.env.example) and [`telegram_panel/.env.example`](telegram_panel/.env.example). The tables below reflect what is actually configured on the live production services (values as of 2026-08-10) — see also `render.yaml`, which is kept in sync with production for a reproducible fresh deploy.

### Live Trading Engine (`goper-v4-robot`)

| Variable | Required | Current production value | Description |
|----------|----------|---------|-------------|
| `MTAPI_URL` | **YES** | `https://ger-mtapi.onrender.com` | URL of the `mtapi-bridge` Docker service |
| `MT5_HOST` | **YES** | `AMarkets-Demo` | Broker server name |
| `MT5_PORT` | **YES** | `443` | Broker TCP port |
| `MT5_USER` | **YES (secret)** | — | MT5 account login number |
| `MT5_PASSWORD` | **YES (secret)** | — | MT5 account password |
| `ROBOT_COMMAND_TOKEN` | **YES (secret)** | — | Shared secret for the panel→robot `/command` HTTP endpoint |
| `REDIS_URL` | **YES (secret)** | — | Redis connection string (same instance as the panel, same region) |
| `SYMBOL` | No | `XAUUSD` | Trading instrument (AMarkets reports gold as `XAUUSD`, not `XAUUSDb`) |
| `TIMEFRAME` | No | `5m` | Primary trading timeframe |
| `TRADE_TIMEFRAMES` | No | `M20,M15,M10,5m` | Multi-timeframe scan order, highest first |
| `CANDLE_WINDOW` | No | `300` | Bars fetched per candle request |
| `RISK_PERCENT` | No | `1.0` | Risk per trade (% of balance) |
| `MIN_CONFIRMATIONS` | No | `2` | Every ordinary entry requires two aligned engine confirmations |
| `TREND_MIN_CONFIRMATIONS` | No | `2` | Minimum confirmations when Trend votes for the entry direction |
| `PRICE_ACTION_STANDALONE` | No | `false` | Allow a directional Price Action signal to bypass the two-confirmation floor |
| `RANGE_TRADING_ENABLED` | No | `true` | Enable the dedicated edge/sweep/reversal RANGE playbook |
| `RANGE_MIN_CONFIRMATIONS` | No | `2` | RANGE requires two aligned strategy confirmations |
| `RANGE_MIN_RR` | No | `1.5` | Minimum RANGE risk/reward |
| `RANGE_EDGE_ATR_DISTANCE` | No | `0.25` | Maximum distance from a RANGE edge in ATR units |
| `RANGE_RISK_PERCENT` | No | `0.25` | Risk per RANGE trade (% of balance) |
| `RANGE_ENTRY_FILTERS_ENABLED` | No | `true` | Require RANGE edge, liquidity sweep, reversal, and two-confirmation validation |
| `MAX_RANGE_TRADES_PER_SESSION` | No | `2` | Successful RANGE entries allowed per UTC session |
| `MAX_OPEN_TRADES` | No | `4` | Maximum simultaneous open positions (one per strategy slot) |
| `CONF_HARD_MIN` | No | `35` | Balanced confidence-engine hard floor |
| `OPTION_TWO_MIN_CONFIDENCE` | No | `35` | MTF confidence floor for the HTF-confirmed entry gate |
| `MTF_ENABLED` | No | `true` | Enable the Option 1/2 higher-timeframe entry blocker |
| `QUALITY_ADX_MIN` | No | `12` | Balanced quality-filter ADX floor; blocks only very weak momentum |
| `DAILY_LOSS_LIMIT_PCT` | No | `4.0` | Guardian: daily loss halt threshold |
| `MAX_DRAWDOWN_PCT` | No | `12.0` | Guardian: drawdown halt threshold |
| `SLIPPAGE_POINTS` | No | `30` | Max fill slippage in broker points |
| `STATE_FILE` / `MT5_SNAPSHOT` / `COMMANDS_FILE` / `GUARDIAN_STATE_FILE` / `LOG_FILE` | No | `/tmp/...` | Local file paths — ephemeral, real cross-restart durability comes from Redis (see Architecture above) |

### Telegram Panel (`anel`)

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | **YES (secret)** | Bot token from @BotFather |
| `TELEGRAM_OWNER_ID` | **YES (secret)** | Your Telegram numeric user ID |
| `PANEL_ENCRYPTION_KEY` | **YES (secret)** | Fernet key for credential encryption — generate with `python -m telegram_panel.main --generate-key` |
| `ROBOT_COMMAND_TOKEN` | **YES (secret)** | Must match the robot service's value |
| `REDIS_URL` | **YES (secret)** | Must be the same Redis instance/region as the robot |
| `ROBOT_BASE_URL` | No | Robot service URL, used for the HTTP command fallback |
| `MT5_USER` / `MT5_PASSWORD` | **YES (secret)** | Same broker credentials as the robot, for display/verification |
| `MT5_HOST` | No | Broker server name |
| `TELEGRAM_ADMIN_IDS` | No | Comma-separated admin Telegram IDs |

### mt5rest Bridge (`ger-mtapi`, Docker)

| Variable | Required | Description |
|----------|----------|-------------|
| `MT5_HOST` / `MT5_PORT` | **YES** | Broker server name and port |
| `MT5_USER` / `MT5_PASSWORD` | **YES (secret)** | Broker login used by the MT5 terminal inside the container |
| `UserKey` | **YES (secret)** | Fixed 14-day trial key from `https://users.mtapi.io`; set it manually in Render and do not regenerate it |

---

## Deployment

See [DEPLOYMENT_GUIDE.md](audit_reports/DEPLOYMENT_GUIDE.md) for full deployment instructions including:
- Render.com two-service deployment
- Persistent storage configuration
- systemd service setup
- Docker Compose setup

---

## Operations

See [OPERATIONS_GUIDE.md](audit_reports/OPERATIONS_GUIDE.md) for:
- Telegram panel commands reference
- Guardian circuit breaker management
- Backup and recovery procedures
- Log analysis guide
- Upgrade and key rotation procedures

---

## Risk Warning

This software trades real or simulated money. **Always test on a demo account first.**

- The backtest engine V1 (`backtestEngine.ts`) uses **synthetic price data** — its results are NOT validated against real market conditions and must not be used to justify live deployment.
- The backtest engine V2 (`backtestEngineV2.ts`) requires a real XAUUSD historical CSV file. No CSV is included in this repository.
- Past performance (even on real historical data) does not guarantee future results.

---

## Audit Status

| Item | Status |
|------|--------|
| Trading behaviour frozen | ✅ Certified unchanged |
| 9 Phase-1 engineering fixes | ✅ Regression-verified |
| 11 Phase-2 engineering fixes | ✅ Applied (this release) |
| Dependency versions pinned | ✅ |
| Guardian env vars in deployment config | ✅ |
| Log rotation | ✅ |
| Candle deduplication | ✅ |
| Encryption key enforcement | ✅ |
| Engineering test suite | ✅ |
| Double-entry risk on disconnect | ⚠️ Requires live paper test |
| Backtest on real historical data | ⚠️ Requires real XAUUSD CSV |
| Persistent storage (Render) | ⚠️ Requires persistent disk mount |

Full audit reports: [audit_reports/](audit_reports/)

---

## Project Structure

```
GoldScalperPro/
├── live_trading/              ← Live trading engine (Python)
│   ├── main.py
│   ├── config.py
│   ├── requirements.txt       ← Pinned exact versions
│   ├── .env.example           ← Environment variable template
│   ├── signals/               ← 7 signal engines (FROZEN)
│   ├── risk/                  ← Guardian + capital manager (FROZEN)
│   ├── mt5/                   ← mt5rest bridge connector + executor
│   ├── trading/               ← Async M5 trading loop
│   └── utils/                 ← State file writer
├── telegram_panel/            ← Telegram control panel (Python)
│   ├── main.py
│   ├── requirements.txt       ← Pinned exact versions
│   ├── .env.example
│   └── ...
├── robot/                     ← TypeScript backtest engine (dev tool only)
│   └── src/
├── tests/                     ← Engineering test suite
├── audit_reports/             ← Independent audit documentation
├── render.yaml                ← Two-service Render deployment
├── README.md                  ← This file
├── CHANGELOG.md               ← Version history
└── LICENSE                    ← MIT License
```

---

*GoldScalperPro v4 Stable — Audited Release 2026-07-19, Portability-Verified Release 4.0.4 (2026-08-10)*

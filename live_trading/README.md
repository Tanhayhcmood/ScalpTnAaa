# GoldScalperPro v4 — Live Trading Robot

**Platform-agnostic** — runs on Linux, macOS, Windows, Render, Railway, Fly.io.  
MT5 access via **MetaAPI** cloud bridge (no Windows / COM required).

---

## Broker history recovery

The engine restores its local/Redis snapshot first, then synchronizes the
configured lookback window from MetaAPI account-deal history. Entry and exit
deals are grouped by `positionId`, so a restarted Render service does not show
an empty trade history when the broker still has real transactions.

`HISTORY_LOOKBACK_DAYS` defaults to `90` and `HISTORY_SYNC_INTERVAL` defaults to
`300` seconds. The latest result is exposed in `robot_state.json` under
`trade_history_sync`, including whether the broker returned zero deals or the
history request failed.

---

## Quick Start

### 1 — Get MetaAPI credentials (free)

1. Sign up at [app.metaapi.cloud](https://app.metaapi.cloud)
2. Go to **Accounts** → Add your MT5 demo account → copy the **Account ID**
3. Go to **API** → copy your **Token**

### 2 — Install dependencies

```bash
pip install -r live_trading/requirements.txt
```

### 3 — Set environment variables

```bash
export METAAPI_TOKEN="your-token-here"
export METAAPI_ACCOUNT_ID="your-account-id-here"

# Optional overrides
export SYMBOL=XAUUSD
export RISK_PERCENT=1.0
export MIN_CONFIRMATIONS=1

# Risk Guardian — circuit breakers (strongly recommended for live accounts)
export DAILY_LOSS_LIMIT_PCT=3.0   # halt if day PnL drops below -3% of balance
export MAX_DRAWDOWN_PCT=8.0        # halt if equity drops 8% from session peak
export SLIPPAGE_POINTS=30          # max fill deviation in broker points
```

### 4 — Run

```bash
python -m live_trading.main
```

---

## Deploy to Render (free tier)

1. Push this project to a GitHub repo
2. Go to [render.com](https://render.com) → **New → Background Worker**
3. Connect the repo — Render auto-detects `render.yaml`
4. Set `METAAPI_TOKEN` and `METAAPI_ACCOUNT_ID` in the **Environment** tab
5. Click **Deploy**

That's it — the robot runs 24/7 on Render's infrastructure.

---

## Architecture

```
live_trading/
├── main.py                       ← asyncio entry point
├── config.py                     ← all parameters (env-var driven)
├── requirements.txt              ← metaapi-cloud-sdk, aiohttp, aiofiles
├── signals/                      ← 7 signal engines (pure Python, no MT5)
│   ├── gold_engine.py            ← EMA / ATR helpers
│   ├── smc_engine.py             ← BOS, CHoCH, OB, FVG, Sweeps
│   ├── wyckoff_engine.py         ← Phase, Spring, Upthrust
│   ├── price_action_engine.py    ← Patterns, S/R, Breakouts
│   ├── trend_engine.py           ← EMA 50/100/200
│   ├── market_regime.py          ← 11 regimes + ADX
│   ├── confidence_engine.py      ← 0–100 weighted score
│   ├── quality_filter.py         ← Session quality / ADX / late-entry gate
│   ├── entry_filter.py           ← Trend-only live-entry gate
│   └── decision_engine.py        ← master orchestrator
├── risk/
│   ├── capital_manager.py        ← SL (structural), TP=2R, lot=1%
│   └── guardian.py               ← ⭐ circuit breaker: daily loss + drawdown stop
├── mt5/
│   ├── connector.py              ← MetaAPI connect / fetch candles (exp. backoff)
│   └── executor.py               ← place / modify / close (slippage-controlled)
├── trading/
│   └── live_loop.py              ← async M5-bar loop (Guardian-wired)
└── utils/
    └── state_writer.py           ← writes robot_state.json
```

---

## Signal Pipeline

```
candles (300 M5 bars)
   │
   ├─► SMC Engine        → BOS, CHoCH, OB, FVG, Sweeps
   ├─► Trend Engine      → EMA 50/100/200 direction
   ├─► Price Action      → Engulf, Pin Bar, Breakout
   ├─► Wyckoff Engine    → Phase, Spring, Upthrust
   │
    ├─► Entry Filter      → Trend only (other engines stay diagnostic)
   ├─► Market Regime     → 11 regimes + ADX + rules per regime
   ├─► Confidence Engine → 0–100 score (6 weighted bands)
   ├─► Quality Filter    → Session quality / ADX / late-entry / volume gate
   ├─► Capital Manager   → structural SL, 2R TP, 1% risk lot size
   │
   └─► Decision Engine   → ALLOWED / BLOCKED + full reasoning
```

---

## Key Parameters

| Parameter | Default | Source |
|---|---|---|
| Confidence hard minimum | 70% | decisionEngine.ts |
| Risk per trade | 1% of balance | capitalManager.ts |
| Take profit | 2R | capitalManager.ts |
| Min confirmations | 1-of-1 | entry_filter.py |
| SMC swing lookback | 5 bars | smcEngine.ts |
| ATR period | 14 (Wilder) | goldEngine.ts |

---

## Telegram Panel

The robot writes `robot_state.json` and `robot_mt5_snapshot.json` every bar.  
The Telegram panel (`telegram_panel/`) reads these files and exposes controls.

### Commands (via `robot_commands.json`)
| Command | Action |
|---|---|
| `{"pause": true}` | Stop opening new trades |
| `{"resume": true}` | Resume trading (blocked if Guardian is halted) |
| `{"stop": true}` | Graceful shutdown |
| `{"close_all": true}` | Close all open positions immediately |
| `{"reset_guardian": true}` | ⭐ Clear a Guardian halt and resume trading |

### Risk Guardian — robot_state.json fields
After every bar, `robot_state.json` includes a `guardian` block:
```json
{
  "guardian": {
    "halted": false,
    "reason": "",
    "daily_pnl": 42.50,
    "daily_pnl_pct": 0.425,
    "drawdown_pct": 1.23,
    "equity_peak": 10215.00,
    "session_open_balance": 10000.00,
    "daily_loss_limit_pct": 3.0,
    "max_drawdown_pct": 8.0,
    "triggered_at": null
  }
}
```

---

## Risk Warning

This software trades real or demo money. Always test on a **demo account** first.  
Past backtest results do not guarantee future performance.

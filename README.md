# GoldScalperPro v4 Stable

**Production-grade XAUUSD scalping system for MetaTrader 5.** The current strategy branch uses **M1 (1-minute) entries and execution**, with stop-loss volatility measured on the configured higher timeframe (M15 in the Render deployment).

> Strategy changes are isolated on the `copilot/xauusd-scalp-entry-hardening` branch for review and demo testing. Do not merge or deploy to a funded account before tests and forward testing on demo.

## Timeframe setup

- `TIMEFRAME=1m` and `TRADE_TIMEFRAMES=1m` select the one-minute signal/entry stream.
- `SL_ATR_TIMEFRAME=M15` intentionally remains higher than M1 to reduce sensitivity of the protective stop to one-minute noise.
- The active entry policy rejects executions on any timeframe other than M1.
- This changes the evaluation frequency and signal characteristics; it does not guarantee profitability. Validate with realistic spread, commission, slippage, and broker-specific XAUUSD symbol data.

## Running

```bash
pip install -r live_trading/requirements.txt
cp live_trading/.env.example .env
# Configure broker credentials and risk limits before running.
python live_trading/server.py
```

Always start with a demo account. Check the startup `EFFECTIVE_CONFIG` log to ensure both entry timeframes report `1m`, then inspect the decision and order logs before enabling any live account.

## Risk warning

This software can trade real or simulated money. Past performance and code-level filters do not guarantee future results. Start on demo and use conservative risk settings.

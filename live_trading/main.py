"""
 GoldScalperPro v4 – Live Trading Entry Point (MTAPI / Linux-compatible)

Direct MT5 connection via the official MTAPI REST API.

Usage:
    python -m live_trading.main

Required environment variables:
    MTAPI_URL      – normally https://mt5.mtapi.io
    MT5_HOST       – broker host/IP
    MT5_USER       – MT5 account login number
    MT5_PASSWORD   – MT5 account password

Optional:
    SYMBOL               – default: XAUUSDb  (AMarkets uses the 'b' suffix; adjust for your broker)
    RISK_PERCENT         – default: 1.0
    MIN_CONFIRMATIONS    – default: 2 (RANGE uses 2)
    DAILY_LOSS_LIMIT_PCT – default: 3.0
    MAX_DRAWDOWN_PCT     – default: 8.0
    SLIPPAGE_POINTS      – default: 30
"""
import asyncio
import os
import sys

if sys.version_info < (3, 11):
    print(
        f"ERROR: GoldScalperPro requires Python 3.11+. "
        f"Running Python {sys.version_info.major}.{sys.version_info.minor}."
    )
    sys.exit(1)

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from live_trading.logger import get_logger
from live_trading.config import MTAPI_URL, MT5_USER, MT5_PASSWORD, MT5_HOST
from live_trading.trading.live_loop import GoldScalperLive

log = get_logger()


async def _main() -> None:
    missing = []
    if not MT5_USER:
        missing.append("MT5_USER")
    if not MT5_PASSWORD:
        missing.append("MT5_PASSWORD")
    if not MT5_HOST:
        missing.append("MT5_HOST")

    if missing:
        for var in missing:
            log.error(f"Environment variable {var} is not set.")
        log.error(
            "Set the missing variables in the Render dashboard -> Environment. "
            "MTAPI_URL defaults to the official MTAPI service."
        )
        sys.exit(1)

    log.info(f"MT5 broker : {MT5_HOST}")
    # Mask account number: show only first 3 chars to avoid leaking credentials in logs.
    _masked_user = (MT5_USER[:3] + "***") if len(MT5_USER) > 3 else "***"
    log.info(f"MT5 user   : {_masked_user}")
    log.info(f"MT5 password configured: {bool(MT5_PASSWORD)} (value redacted)")
    log.info(f"MTAPI URL: {MTAPI_URL}")

    engine = GoldScalperLive()
    connected = await engine.start()

    if connected is False:
        log.error("Engine failed to connect to MT5. Exiting for restart.")
        sys.exit(1)


def main() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        log.info("Keyboard interrupt – shutting down")


if __name__ == "__main__":
    main()

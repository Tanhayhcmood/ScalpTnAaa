"""
DXY (US Dollar Index) Correlation Filter — GoldScalperPro v4.

Gold has an ~85% inverse correlation with the US Dollar Index.
This filter fetches real-time DXY trend data so the decision engine can:
  - Avoid BUY gold signals when the dollar is strengthening (BULLISH_DXY)
  - Favour BUY gold signals when the dollar is weakening  (BEARISH_DXY)
  - Proceed normally when the dollar is directionless     (NEUTRAL)

Data source: Yahoo Finance free intraday API — no API key required.
  Ticker: DX-Y.NYB  (ICE DXY futures, freely available)

Cache: result cached 5 minutes (≈ one M5 candle interval).
Fail-open: on any network / parse error the filter returns NEUTRAL so
  a data outage never blocks the robot.

DXY trend logic:
  BULLISH_DXY : last close > EMA20 > EMA50  (dollar rising  → headwind for gold)
  BEARISH_DXY : last close < EMA20 < EMA50  (dollar falling → tailwind for gold)
  NEUTRAL     : mixed / insufficient data
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
import os

import aiohttp

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
_YF_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB"
    "?interval=5m&range=2d"
)
_CACHE_TTL_S     = int(os.getenv("DXY_CACHE_TTL_S", "300"))   # 5 min default
_FETCH_TIMEOUT_S = 8


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ema(closes: list, period: int) -> float:
    """Simple EMA over a list of floats."""
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    k   = 2.0 / (period + 1)
    val = sum(closes[:period]) / period
    for p in closes[period:]:
        val = p * k + val * (1 - k)
    return val


async def _fetch_dxy_closes() -> list:
    """
    Fetch recent 5-min DXY closing prices from Yahoo Finance.
    Returns an empty list on any error (fail-open).
    """
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                _YF_URL,
                timeout=aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_S),
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; GoldScalperPro/4.0)",
                    "Accept": "application/json",
                },
            ) as resp:
                if resp.status != 200:
                    log.debug(f"[dxy_filter] Yahoo Finance HTTP {resp.status} — NEUTRAL")
                    return []
                data   = await resp.json(content_type=None)
                result = data.get("chart", {}).get("result") or []
                if not result:
                    return []
                closes = (
                    result[0]
                    .get("indicators", {})
                    .get("quote", [{}])[0]
                    .get("close", [])
                )
                # Strip None values (market-closed bars)
                return [c for c in closes if c is not None]
    except Exception as exc:
        log.debug(f"[dxy_filter] DXY fetch failed (fail-open): {exc}")
        return []


# ── Public API ────────────────────────────────────────────────────────────────

class DxyFilterResult:
    """Returned by get_dxy_signal()."""
    __slots__ = ("signal", "dxy_close", "ema20", "ema50")

    def __init__(
        self,
        signal: str,
        dxy_close: float = 0.0,
        ema20: float = 0.0,
        ema50: float = 0.0,
    ) -> None:
        self.signal    = signal       # "BULLISH_DXY" | "BEARISH_DXY" | "NEUTRAL"
        self.dxy_close = dxy_close
        self.ema20     = ema20
        self.ema50     = ema50

    def __repr__(self) -> str:
        return (
            f"DxyFilterResult(signal={self.signal}  "
            f"close={self.dxy_close:.3f}  "
            f"EMA20={self.ema20:.3f}  EMA50={self.ema50:.3f})"
        )


# Module-level cache
_cache_result:  DxyFilterResult = DxyFilterResult("NEUTRAL")
_cache_expires: Optional[datetime] = None


async def get_dxy_signal() -> DxyFilterResult:
    """
    Compute the current DXY trend and return the filter result.
    Result is cached for _CACHE_TTL_S seconds.
    On any error returns DxyFilterResult(signal='NEUTRAL').
    """
    global _cache_result, _cache_expires
    now = datetime.now(timezone.utc)

    if _cache_expires and now < _cache_expires:
        return _cache_result

    closes = await _fetch_dxy_closes()

    if len(closes) < 22:
        log.debug(
            f"[dxy_filter] Only {len(closes)} DXY bars — returning NEUTRAL"
        )
        return DxyFilterResult("NEUTRAL")

    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, min(50, len(closes)))
    last  = closes[-1]

    if last > ema20 > ema50:
        signal = "BULLISH_DXY"   # USD rising  → headwind for gold BUY
    elif last < ema20 < ema50:
        signal = "BEARISH_DXY"   # USD falling → tailwind  for gold BUY
    else:
        signal = "NEUTRAL"

    result = DxyFilterResult(
        signal=signal, dxy_close=last, ema20=ema20, ema50=ema50
    )

    _cache_result  = result
    _cache_expires = now + timedelta(seconds=_CACHE_TTL_S)

    log.info(f"[dxy_filter] {result}")
    return result

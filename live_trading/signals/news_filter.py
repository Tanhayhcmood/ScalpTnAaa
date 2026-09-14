"""
News / Economic-Calendar Filter — GoldScalperPro v4.

Blocks trading around high-impact USD economic events that cause extreme
gold volatility: FOMC, CPI, NFP, PPI, GDP, PCE, Retail Sales, ISM, etc.

Data source: ForexFactory free JSON feed — no API key required.
  https://nfs.faireconomy.media/ff_calendar_thisweek.json

Blocking windows (configurable via NEWS_BLOCK_BEFORE_MIN / NEWS_BLOCK_AFTER_MIN):
  - HIGH impact USD events: 15 min before → 30 min after event time

Fail-open: on any network / parse error trading is NOT blocked so a feed
outage never silently stops the robot.

Cache: calendar refreshed once per hour to avoid hammering the feed.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional
import os

import aiohttp

log = logging.getLogger(__name__)

# ── Config (env-overridable) ──────────────────────────────────────────────────
_FEED_URL        = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_BLOCK_BEFORE_S  = int(os.getenv("NEWS_BLOCK_BEFORE_MIN", "15")) * 60
_BLOCK_AFTER_S   = int(os.getenv("NEWS_BLOCK_AFTER_MIN",  "30")) * 60
_CACHE_TTL_S     = 3600   # refresh once per hour
_FETCH_TIMEOUT_S = 10     # give up quickly — never block the trading loop

# Keywords that mark genuinely high-impact USD events for gold
_HI_KEYWORDS = {
    "fomc", "federal", "interest rate", "nonfarm", "non-farm",
    "cpi", "consumer price", "ppi", "producer price",
    "gdp", "unemployment", "payroll", "inflation",
    "core", "retail sales", "ism", "pce", "durable goods",
    "trade balance", "jobs", "monetary policy",
}

# ── Module-level cache ────────────────────────────────────────────────────────
_cache_events:  List[dict] = []
_cache_expires: Optional[datetime] = None
_cache_lock = asyncio.Lock()


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _fetch_calendar() -> List[dict]:
    """Download this-week's ForexFactory calendar. Returns [] on any error."""
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                _FEED_URL,
                timeout=aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_S),
                headers={"User-Agent": "GoldScalperPro/4.0 news-filter"},
            ) as resp:
                if resp.status != 200:
                    log.warning(f"[news_filter] FF calendar HTTP {resp.status} — fail-open")
                    return []
                data = await resp.json(content_type=None)
                return data if isinstance(data, list) else []
    except Exception as exc:
        log.warning(f"[news_filter] Calendar fetch failed (fail-open): {exc}")
        return []


async def _get_events() -> List[dict]:
    """Return cached events, refreshing when the TTL expires."""
    global _cache_events, _cache_expires
    now = datetime.now(timezone.utc)
    async with _cache_lock:
        if _cache_expires is None or now >= _cache_expires:
            fresh = await _fetch_calendar()
            if fresh:  # only overwrite on success
                _cache_events  = fresh
                _cache_expires = now + timedelta(seconds=_CACHE_TTL_S)
                log.info(f"[news_filter] Calendar refreshed — {len(fresh)} events this week")
    return _cache_events


def _is_high_impact_usd(ev: dict) -> bool:
    """True for HIGH-impact USD events that substantially move gold."""
    if (ev.get("country") or "").upper() != "USD":
        return False
    if (ev.get("impact") or "").upper() != "HIGH":
        return False
    title = (ev.get("title") or "").lower()
    return any(kw in title for kw in _HI_KEYWORDS)


def _parse_event_utc(ev: dict) -> Optional[datetime]:
    """Parse the event's UTC datetime from the FF feed (date + time fields)."""
    date_str = (ev.get("date") or "").strip()
    time_str = (ev.get("time") or "").strip()
    if not date_str:
        return None
    fmts = (
        ("%Y-%m-%d %I:%M%p", f"{date_str} {time_str}"),  # "2024-01-12 08:30am"
        ("%Y-%m-%d %H:%M",   f"{date_str} {time_str}"),  # "2024-01-12 08:30"
        ("%Y-%m-%d",          date_str),                  # date-only (all-day)
    )
    for fmt, val in fmts:
        try:
            return datetime.strptime(val, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ── Public API ────────────────────────────────────────────────────────────────

class NewsFilterResult:
    """Returned by check_news_filter()."""
    __slots__ = ("blocked", "reason", "event_name", "event_time_utc")

    def __init__(
        self,
        blocked: bool,
        reason: str = "",
        event_name: str = "",
        event_time_utc: str = "",
    ) -> None:
        self.blocked        = blocked
        self.reason         = reason
        self.event_name     = event_name
        self.event_time_utc = event_time_utc

    def __repr__(self) -> str:
        if self.blocked:
            return f"NewsFilterResult(BLOCKED — {self.reason})"
        return "NewsFilterResult(clear)"


async def check_news_filter(
    now: Optional[datetime] = None,
) -> NewsFilterResult:
    """
    Return NewsFilterResult(blocked=True) if a high-impact USD event falls
    inside the configured blackout window around the current time.

    Always fails open: network / parse errors → blocked=False.
    """
    current = now or datetime.now(timezone.utc)
    events  = await _get_events()

    for ev in events:
        if not _is_high_impact_usd(ev):
            continue
        ev_time = _parse_event_utc(ev)
        if ev_time is None:
            continue

        delta = (ev_time - current).total_seconds()

        if -_BLOCK_AFTER_S <= delta <= _BLOCK_BEFORE_S:
            title = ev.get("title", "Unknown event")
            if delta > 0:
                mins = int(delta / 60)
                reason = f"High-impact news in {mins}m: {title}"
            else:
                mins = int(-delta / 60)
                reason = f"Post-news blackout ({mins}m since): {title}"
            return NewsFilterResult(
                blocked=True,
                reason=reason,
                event_name=title,
                event_time_utc=ev_time.strftime("%H:%M UTC"),
            )

    return NewsFilterResult(blocked=False)

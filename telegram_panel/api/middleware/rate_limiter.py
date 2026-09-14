"""
Rate Limiter — prevents spam and abuse.
Per-user sliding window rate limiting.
"""

import asyncio
import logging
from collections import deque
from datetime import datetime

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Sliding window rate limiter.
    Default: 30 requests per 60 seconds per user.
    """

    def __init__(
        self,
        max_requests: int = 30,
        window_seconds: float = 60.0,
        exempt_ids: frozenset[int] = frozenset(),
    ) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._exempt = exempt_ids
        # telegram_id -> deque of timestamps
        self._buckets: dict[int, deque] = {}
        # telegram_id -> warning count
        self._warnings: dict[int, int] = {}

    async def check(self, telegram_id: int) -> bool:
        """
        Returns True if request is allowed, False if rate-limited.
        Non-blocking.
        """
        if telegram_id in self._exempt:
            return True

        now = asyncio.get_running_loop().time()
        cutoff = now - self._window

        if telegram_id not in self._buckets:
            self._buckets[telegram_id] = deque()

        bucket = self._buckets[telegram_id]

        # Evict old entries
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if len(bucket) >= self._max:
            warns = self._warnings.get(telegram_id, 0) + 1
            self._warnings[telegram_id] = warns
            logger.warning(
                f"Rate limit exceeded: user {telegram_id} "
                f"({len(bucket)} req in {self._window}s) — warning #{warns}"
            )
            return False

        bucket.append(now)
        return True

    def reset(self, telegram_id: int) -> None:
        self._buckets.pop(telegram_id, None)
        self._warnings.pop(telegram_id, None)

    async def purge_stale(self) -> int:
        """Remove buckets for inactive users. Call periodically."""
        now = asyncio.get_running_loop().time()
        cutoff = now - self._window * 2
        stale = [
            tid for tid, bucket in self._buckets.items()
            if not bucket or bucket[-1] < cutoff
        ]
        for tid in stale:
            del self._buckets[tid]
            self._warnings.pop(tid, None)
        return len(stale)

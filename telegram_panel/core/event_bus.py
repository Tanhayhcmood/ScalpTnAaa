"""
Event Bus — decoupled publish/subscribe for internal events.
Allows the notification service to listen for trading events
without any circular dependency to trading code.
"""

import asyncio
import logging
from typing import Callable, Awaitable, Any, Optional

logger = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class EventBus:
    """
    Lightweight async pub/sub event bus.
    Publishers fire events; subscribers handle them asynchronously.
    No direct coupling between publishers and subscribers.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventHandler]] = {}
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._worker_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._worker_task = asyncio.create_task(self._worker(), name="event_bus_worker")
        logger.info("Event bus started")

    async def stop(self) -> None:
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("Event bus stopped")

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """Register a handler for an event type."""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(handler)
        logger.debug(f"Subscribed to event: {event_type}")

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        if event_type in self._subscribers:
            self._subscribers[event_type] = [
                h for h in self._subscribers[event_type] if h != handler
            ]

    async def publish(self, event_type: str, data: dict[str, Any] = None) -> None:
        """
        Publish an event. Non-blocking — queued for async delivery.
        """
        event = {"type": event_type, "data": data or {}}
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(f"Event queue full — dropped: {event_type}")

    async def _worker(self) -> None:
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            # task_done() is in a finally block so it is ALWAYS called after a
            # successful get(), even when the event is malformed or a handler
            # raises unexpectedly.  Without this, queue.join() would block
            # indefinitely and the unfinished-task counter would be wrong.
            try:
                event_type = event["type"]
                handlers = self._subscribers.get(event_type, [])
                if handlers:
                    results = await asyncio.gather(
                        *(h(event["data"]) for h in handlers),
                        return_exceptions=True,
                    )
                    # Log any subscriber exceptions — previously swallowed silently.
                    for i, result in enumerate(results):
                        if isinstance(result, Exception):
                            handler_name = getattr(handlers[i], "__name__", repr(handlers[i]))
                            logger.error(
                                f"Event handler '{handler_name}' raised for "
                                f"event '{event_type}': {result!r}"
                            )
            except asyncio.CancelledError:
                self._queue.task_done()
                break
            except Exception as e:
                logger.error(f"Event bus worker error: {e}")
            finally:
                self._queue.task_done()


# Well-known event type constants
class Events:
    TRADE_OPENED = "trade.opened"
    TRADE_CLOSED = "trade.closed"
    SL_HIT = "trade.sl_hit"
    TP_HIT = "trade.tp_hit"
    ROBOT_STARTED = "robot.started"
    ROBOT_STOPPED = "robot.stopped"
    ROBOT_PAUSED = "robot.paused"
    ROBOT_ERROR = "robot.error"
    CONNECTION_LOST = "connection.lost"
    CONNECTION_RESTORED = "connection.restored"
    DAILY_TARGET_HIT = "account.daily_target"
    DAILY_LOSS_HIT = "account.daily_loss"
    NEWS_PAUSE = "news.pause"
    HEARTBEAT = "system.heartbeat"
    SYSTEM_WARNING = "system.warning"
    SYSTEM_ERROR = "system.error"
    SYSTEM_RESTART = "system.restart"

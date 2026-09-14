"""
Heartbeat Monitor — watches robot health and fires events on state changes.
Runs on a configurable interval. Never blocks trading.

Fix: Periodic heartbeat notifications are ONLY sent when the robot is RUNNING.
     When STOPPED/PAUSED/ERROR, only status-change events fire (once),
     preventing spam every minute while the robot is offline.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from .event_bus import EventBus, Events
from ..services.robot_service import RobotService
from ..config.constants import RobotStatus, ConnectionStatus

logger = logging.getLogger(__name__)


class HeartbeatMonitor:
    """
    Periodically checks robot state and emits events when status changes.
    Sends periodic heartbeat notifications ONLY while robot is RUNNING.
    Status-change events (stopped, error, paused) fire once on transition.

    FIX: Also polls open positions (via mt5_service, when provided) and
    fires Events.TRADE_OPENED the moment a new ticket appears. Previously
    Events.TRADE_OPENED was defined but never published anywhere, so a
    freshly opened trade never triggered a Telegram notification — the
    panel only reflected it once the user manually refreshed the dashboard.

    FIX: The "already notified" ticket set only ever grows, never shrinks
    to match whatever get_open_positions() returns this cycle. A previous
    version diffed against "last cycle's tickets", which self-healed to an
    empty set whenever a single poll returned no positions (e.g. a brief
    Redis hiccup during a connection blip) — the next successful poll then
    treated the still-open trade as brand new and sent a duplicate
    TRADE_OPENED notification for the same ticket.
    """

    def __init__(
        self,
        robot_service: RobotService,
        event_bus: EventBus,
        interval_seconds: int = 30,
        heartbeat_notify_interval: int = 600,  # 10 minutes — only while RUNNING
        mt5_service=None,
    ) -> None:
        self._robot = robot_service
        self._bus = event_bus
        self._interval = interval_seconds
        self._hb_interval = heartbeat_notify_interval
        self._mt5 = mt5_service
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_status: Optional[RobotStatus] = None
        self._last_connection: Optional[ConnectionStatus] = None
        self._last_hb_notify: Optional[datetime] = None
        # Tickets we have already published TRADE_OPENED for. This set only
        # ever grows (via |=), it is never replaced/shrunk to "this cycle's
        # tickets" — that would make a single transient empty/short read (a
        # Redis or MT5 hiccup) look like every open position just closed,
        # causing the next good read to re-notify the same still-open ticket.
        # None until the first successful poll so we never fire TRADE_OPENED
        # for positions that were already open before the panel started
        # (that would misleadingly look like a brand-new trade).
        self._known_position_tickets: Optional[set] = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="heartbeat_monitor")
        logger.info(f"Heartbeat monitor started (check interval: {self._interval}s, notify interval: {self._hb_interval}s)")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Heartbeat monitor stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._check()
            except Exception as e:
                logger.error(f"Heartbeat check failed: {e}")
            await asyncio.sleep(self._interval)

    async def _check(self) -> None:
        state = await self._robot.get_state()
        current_status = await self._robot.get_status()
        current_conn = await self._robot.get_connection_status()
        uptime = state.get("uptime_seconds", 0)

        # ── Status change events (fire ONCE on each transition) ──────────────
        if self._last_status is not None and current_status != self._last_status:
            if current_status == RobotStatus.ERROR:
                await self._bus.publish(Events.ROBOT_ERROR, {
                    "status": current_status.value,
                    "error": state.get("last_error", "Unknown error"),
                })
            elif current_status == RobotStatus.RUNNING and self._last_status != RobotStatus.RUNNING:
                await self._bus.publish(Events.ROBOT_STARTED, {"status": current_status.value})
                # Reset heartbeat timer so the first running-heartbeat fires after a full interval
                self._last_hb_notify = datetime.now(timezone.utc)
            elif current_status == RobotStatus.STOPPED:
                await self._bus.publish(Events.ROBOT_STOPPED, {"status": current_status.value})
            elif current_status == RobotStatus.PAUSED:
                await self._bus.publish(Events.ROBOT_PAUSED, {"status": current_status.value})

        # ── Connection change events (fire ONCE on each transition) ──────────
        if self._last_connection is not None and current_conn != self._last_connection:
            if current_conn == ConnectionStatus.DISCONNECTED:
                await self._bus.publish(Events.CONNECTION_LOST, {
                    "previous": self._last_connection.value,
                    "current": current_conn.value,
                })
            elif current_conn == ConnectionStatus.CONNECTED:
                await self._bus.publish(Events.CONNECTION_RESTORED, {
                    "current": current_conn.value,
                })

        # ── Periodic heartbeat — ONLY while robot is actively RUNNING ────────
        # When STOPPED/PAUSED/ERROR: status-change events already notified the user.
        # No need to spam every N seconds with a "still stopped" message.
        if current_status == RobotStatus.RUNNING:
            now = datetime.now(timezone.utc)
            if (
                self._last_hb_notify is None
                or (now - self._last_hb_notify).total_seconds() >= self._hb_interval
            ):
                await self._bus.publish(Events.HEARTBEAT, {
                    "status": current_status.value,
                    "uptime_seconds": uptime,
                    "connection": current_conn.value,
                    "timestamp": now.isoformat(),
                })
                self._last_hb_notify = now

        self._last_status = current_status
        self._last_connection = current_conn

        # ── New-trade detection (fires once per newly seen ticket) ──────────
        # Best-effort: if mt5_service is unavailable or the read fails, skip
        # silently — this must never interfere with status/heartbeat events.
        if self._mt5 is not None:
            try:
                positions = await self._mt5.get_open_positions()
                # Exclude positions with invalid tickets (0 or None) — these
                # are phantom bridge rows that slipped through earlier filters.
                # Treating ticket=0 as a "new" position would send a spurious
                # TRADE OPENED notification with fabricated direction/volume.
                positions = [p for p in positions if p.ticket and p.ticket != 0]
                current_tickets = {p.ticket for p in positions}
                if self._known_position_tickets is None:
                    # First poll: just establish the baseline, never notify —
                    # these positions were already open before the panel started.
                    self._known_position_tickets = current_tickets
                else:
                    new_tickets = current_tickets - self._known_position_tickets
                    if new_tickets:
                        # Publish exactly once per newly-seen ticket. Iterating
                        # every row that matches (the previous behaviour) fires
                        # a duplicate TRADE_OPENED notification whenever the
                        # upstream snapshot contains more than one row for the
                        # same ticket (e.g. a corrupted/phantom duplicate row
                        # from the mt5rest bridge) — the user then sees two
                        # "TRADE OPENED" messages for one trade, one of them
                        # with a fabricated volume/price.
                        notified = set()
                        for pos in positions:
                            if pos.ticket in new_tickets and pos.ticket not in notified:
                                notified.add(pos.ticket)
                                await self._bus.publish(
                                    Events.TRADE_OPENED, {"position": pos}
                                )
                    # Grow-only: union in this cycle's tickets instead of
                    # replacing the set, so a transient empty/partial read
                    # can never erase tickets we've already notified for.
                    self._known_position_tickets |= current_tickets
            except Exception as e:
                logger.debug(f"Open-position poll failed (trade-open detection skipped): {e}")

        logger.debug(f"Heartbeat check: status={current_status.value} conn={current_conn.value}")

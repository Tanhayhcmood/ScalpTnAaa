"""
System Service — CPU, RAM, disk, network, latency monitoring.
Uses psutil for cross-platform system metrics.
"""

import asyncio
import logging
import socket
import time
from typing import Optional
from ..utils.time import utc_now

logger = logging.getLogger(__name__)


class SystemService:
    """Non-blocking system resource monitoring."""

    def __init__(self, robot_service=None) -> None:
        self._robot = robot_service
        self._start_time = time.time()

    async def get_system_stats(self) -> dict[str, object]:
        """Return all system stats in one call."""
        loop = asyncio.get_event_loop()

        def _collect():
            try:
                import psutil
                cpu = psutil.cpu_percent(interval=0.5)
                mem = psutil.virtual_memory()
                disk = psutil.disk_usage("/")
                net = psutil.net_io_counters()
                return {
                    "cpu_percent": round(cpu, 1),
                    "ram_percent": round(mem.percent, 1),
                    "ram_used_gb": round(mem.used / 1024**3, 2),
                    "ram_total_gb": round(mem.total / 1024**3, 2),
                    "disk_percent": round(disk.percent, 1),
                    "disk_used_gb": round(disk.used / 1024**3, 1),
                    "disk_total_gb": round(disk.total / 1024**3, 1),
                    "net_sent_mb": round(net.bytes_sent / 1024**2, 1),
                    "net_recv_mb": round(net.bytes_recv / 1024**2, 1),
                    "available": True,
                }
            except ImportError:
                return {
                    "cpu_percent": 0.0,
                    "ram_percent": 0.0,
                    "ram_used_gb": 0.0,
                    "ram_total_gb": 0.0,
                    "disk_percent": 0.0,
                    "disk_used_gb": 0.0,
                    "disk_total_gb": 0.0,
                    "net_sent_mb": 0.0,
                    "net_recv_mb": 0.0,
                    "available": False,
                }

        return await loop.run_in_executor(None, _collect)

    async def get_uptime(self) -> dict[str, object]:
        elapsed = int(time.time() - self._start_time)
        days = elapsed // 86400
        hours = (elapsed % 86400) // 3600
        minutes = (elapsed % 3600) // 60
        seconds = elapsed % 60
        return {
            "total_seconds": elapsed,
            "days": days,
            "hours": hours,
            "minutes": minutes,
            "seconds": seconds,
            "formatted": f"{days}d {hours:02d}h {minutes:02d}m {seconds:02d}s",
        }

    async def get_latency(self, host: str = "8.8.8.8", port: int = 53) -> Optional[float]:
        """
        TCP-connect latency in ms (non-blocking, works on Render / Docker).
        Uses a TCP handshake to port 53 (DNS) on the target host instead of
        ICMP ping, which is blocked in most container environments.
        """
        loop = asyncio.get_event_loop()

        def _tcp_ping():
            try:
                start = time.monotonic()
                with socket.create_connection((host, port), timeout=3):
                    pass
                return round((time.monotonic() - start) * 1000, 1)
            except Exception:
                return None

        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, _tcp_ping), timeout=4
            )
        except asyncio.TimeoutError:
            return None

    async def get_broker_ping(self, host: Optional[str] = None) -> Optional[float]:
        """Latency to broker server via TCP connect (port 443 fallback)."""
        if host is None:
            return None
        return await self.get_latency(host, port=443)

    async def get_internet_status(self) -> bool:
        """Internet connectivity check via TCP connect to 8.8.8.8:53."""
        latency = await self.get_latency("8.8.8.8", port=53)
        return latency is not None

    async def get_robot_version(self) -> str:
        if self._robot:
            return await self._robot.get_version()
        return "v4.0.0"

    async def read_logs(
        self, log_path: str, lines: int = 50
    ) -> list[str]:
        """Read last N lines from the robot log file."""
        loop = asyncio.get_event_loop()
        def _read():
            import os
            if not os.path.exists(log_path):
                return [f"Log file not found: {log_path}"]
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    all_lines = f.readlines()
                return [l.rstrip() for l in all_lines[-lines:]]
            except OSError as e:
                return [f"Error reading log: {e}"]
        return await loop.run_in_executor(None, _read)

    async def get_full_report(self) -> dict[str, object]:
        """Consolidated system report for the dashboard."""
        stats_task = asyncio.create_task(self.get_system_stats())
        uptime_task = asyncio.create_task(self.get_uptime())
        internet_task = asyncio.create_task(self.get_internet_status())
        latency_task = asyncio.create_task(self.get_latency())

        stats = await stats_task
        uptime = await uptime_task
        internet = await internet_task
        latency = await latency_task

        version = await self.get_robot_version()

        return {
            **stats,
            "uptime": uptime,
            "internet": internet,
            "latency_ms": latency,
            "version": version,
            "timestamp": utc_now().isoformat(),
        }

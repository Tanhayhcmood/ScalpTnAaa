"""Regression tests for Telegram trade-open notification deduplication."""

from types import SimpleNamespace

import pytest

from telegram_panel.config.constants import NotificationType
from telegram_panel.services.notification_service import NotificationService


class _Repo:
    async def get_setting(self, _notification_type):
        return None


@pytest.mark.asyncio
async def test_trade_notification_deduplicates_recipients_and_events(monkeypatch):
    monkeypatch.setattr(
        "telegram_panel.redis_ipc.redis_claim_notification",
        lambda _key: True,
    )
    service = NotificationService(
        notification_repo=_Repo(),
        owner_id=101,
        admin_ids=[101, 202, 202],
    )

    metadata = {"dedupe_key": "trade_open:295236922", "ticket": 295236922}
    await service.notify_all_admins(
        NotificationType.TRADE_OPEN,
        "trade",
        metadata=metadata,
    )
    await service.notify_all_admins(
        NotificationType.TRADE_OPEN,
        "trade",
        metadata=metadata,
    )

    assert service._queue.qsize() == 1
    payload = service._queue.get_nowait()
    assert payload["recipients"] == [101, 202]
    assert payload["metadata"] == metadata


@pytest.mark.asyncio
async def test_existing_redis_claim_suppresses_event(monkeypatch):
    monkeypatch.setattr(
        "telegram_panel.redis_ipc.redis_claim_notification",
        lambda _key: False,
    )
    service = NotificationService(
        notification_repo=_Repo(),
        owner_id=101,
    )

    await service.notify_all_admins(
        NotificationType.TRADE_OPEN,
        "duplicate",
        metadata={"dedupe_key": "trade_open:295236922"},
    )

    assert service._queue.qsize() == 0
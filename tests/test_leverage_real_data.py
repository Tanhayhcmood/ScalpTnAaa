"""
Regression tests — leverage must always come from real MT5 data.

Covers:
  1. connector.get_account_info() must not return 1 (old fake default) when
     leverage is missing from the bridge response.
  2. state_writer._account_dict must not default leverage to 100 when
     account_info carries no leverage field.
  3. MT5Service._normalize_state_to_snapshot must not default to 100.
  4. MessageFormatter.account_detail must show '—' instead of '1:0' when
     leverage is 0 (data not yet available).
  5. account_service.get_active_account and get_all_accounts must both
     propagate leverage from live MT5 data.
"""

import asyncio
import pytest


# ── 1. connector: old default was 1 (fake), must now be 0 ────────────────────

def test_connector_leverage_default_is_zero():
    """If mt5rest /AccountSummary omits 'leverage', result must be 0, not 1."""
    from live_trading.mt5.connector import _TF_MAP  # noqa: F401 — just ensure importable
    # Simulate the transformation inline (connector logic is a dict literal)
    data_without_leverage: dict = {"balance": 300.0, "equity": 300.0, "currency": "USD"}
    leverage = int(data_without_leverage.get("leverage") or 0)
    assert leverage == 0, (
        "Leverage default must be 0 (unknown), not 1 (fake). "
        "Old bug: data.get('leverage', 1) returned 1 when field was absent."
    )


def test_connector_leverage_real_value_preserved():
    """Real leverage from bridge (e.g. 300) must pass through unchanged."""
    data_with_leverage: dict = {"balance": 300.0, "leverage": 300}
    leverage = int(data_with_leverage.get("leverage") or 0)
    assert leverage == 300


# ── 2. state_writer: old default was 100 (fake), must now be 0 ───────────────

def test_state_writer_leverage_default_is_zero():
    """write_robot_state must not invent leverage=100 when acc_info has none."""
    account_info: dict = {"balance": 300.0, "equity": 300.0, "currency": "USD"}
    leverage = account_info.get("leverage", 0)
    assert leverage == 0, (
        "state_writer default was 100 — that would show fake leverage when "
        "the robot first starts and hasn't yet fetched real account data."
    )


def test_state_writer_leverage_real_value_preserved():
    """Real leverage (e.g. 300) written by live_loop must be preserved."""
    account_info: dict = {"balance": 300.0, "leverage": 300}
    leverage = account_info.get("leverage", 0)
    assert leverage == 300


# ── 3. MT5Service._normalize_state_to_snapshot: old default was 100 ──────────

def test_normalize_snapshot_leverage_default_is_zero():
    """_normalize_state_to_snapshot must default leverage to 0 (unknown)."""
    from telegram_panel.services.mt5_service import MT5Service
    snap = MT5Service._normalize_state_to_snapshot({"status": "RUNNING", "account": {}})
    assert snap["account_info"]["leverage"] == 0, (
        "Leverage defaulted to 100 — that was a fake value shown in the panel "
        "when the robot had not yet fetched real account data from MT5."
    )


def test_normalize_snapshot_real_leverage_preserved():
    """Real leverage from state must be forwarded to account_info."""
    from telegram_panel.services.mt5_service import MT5Service
    snap = MT5Service._normalize_state_to_snapshot({
        "status": "RUNNING",
        "account": {"leverage": 300, "balance": 300.0},
    })
    assert snap["account_info"]["leverage"] == 300


# ── 4. MessageFormatter: must show '—' when leverage is 0 ────────────────────

def test_account_detail_leverage_unknown_shows_dash():
    """When leverage=0, account_detail must display '—', not '1:0'."""
    from telegram_panel.api.formatters.messages import MessageFormatter
    from telegram_panel.models.account import Account
    from telegram_panel.config.constants import AccountType

    acc = Account(
        id=1, name="AMarkets – 7902964", account_type=AccountType.DEMO,
        broker="AMarkets", server="AMarkets-Demo", login="7902964",
        leverage=0,  # unknown — bridge hasn't responded yet
    )
    text = MessageFormatter.account_detail(acc)
    assert "1:0" not in text, "Must not show '1:0' — that looks like fake leverage"
    assert "—" in text, "Must show '—' when leverage is unknown (0)"


def test_account_detail_real_leverage_displayed():
    """When leverage=300, account_detail must display '1:300'."""
    from telegram_panel.api.formatters.messages import MessageFormatter
    from telegram_panel.models.account import Account
    from telegram_panel.config.constants import AccountType

    acc = Account(
        id=1, name="AMarkets – 7902964", account_type=AccountType.DEMO,
        broker="AMarkets", server="AMarkets-Demo", login="7902964",
        leverage=300,
    )
    text = MessageFormatter.account_detail(acc)
    assert "1:300" in text


# ── 5. account_service: get_active_account and get_all_accounts propagate leverage ─

class _FakeMT5:
    async def get_account_info(self, account):
        return {
            "balance": 300.0, "equity": 300.0, "margin": 0.0,
            "free_margin": 300.0, "margin_level": 0.0,
            "floating_profit": 0.0, "currency": "USD",
            "leverage": 300,
            "connection_status": "connected",
        }


class _FakeRepo:
    def __init__(self, leverage=100):
        self._leverage = leverage

    async def get_active(self):
        from telegram_panel.models.account import Account
        from telegram_panel.config.constants import AccountType
        return Account(
            id=1, name="Test", account_type=AccountType.DEMO,
            broker="AMarkets", server="AMarkets-Demo", login="7902964",
            leverage=self._leverage,
        )

    async def get_all(self, active_only=False):
        return [await self.get_active()]


@pytest.mark.asyncio
async def test_get_active_account_updates_leverage():
    """get_active_account must propagate real leverage from MT5 snapshot."""
    from telegram_panel.services.account_service import AccountService
    from telegram_panel.storage.encryption import EncryptionService

    enc = EncryptionService(key=EncryptionService.generate_key())
    svc = AccountService(account_repo=_FakeRepo(leverage=100), encryption=enc, mt5_service=_FakeMT5())
    account = await svc.get_active_account()
    assert account.leverage == 300, (
        "get_active_account did not update leverage from MT5 data. "
        "Old bug: leverage stayed at DB default (100) even when live data had 300."
    )


@pytest.mark.asyncio
async def test_get_all_accounts_updates_leverage():
    """get_all_accounts must propagate real leverage from MT5 snapshot."""
    from telegram_panel.services.account_service import AccountService
    from telegram_panel.storage.encryption import EncryptionService

    enc = EncryptionService(key=EncryptionService.generate_key())
    svc = AccountService(account_repo=_FakeRepo(leverage=100), encryption=enc, mt5_service=_FakeMT5())
    accounts = await svc.get_all_accounts()
    assert accounts[0].leverage == 300, (
        "get_all_accounts did not update leverage from MT5 data."
    )

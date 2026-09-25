from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from live_trading.mt5.executor import TradeResult
from live_trading.risk.capital_manager import CapitalInput, calc_trade_parameters
from live_trading.trading import live_loop
from live_trading.trading.live_loop import GoldScalperLive
from live_trading.trading.strategy_slots import ACTIVE_STRATEGY_SLOT


def _decision(initial_rr: float):
    trade_params = calc_trade_parameters(
        CapitalInput(
            direction="BUY",
            entry_price=100.0,
            atr=1.0,
            account_balance=10_100.0,
            take_profit_rr=initial_rr,
            sl_atr_multiplier=2.0,
            sl_min_atr_multiplier=1.5,
        )
    )
    assert trade_params.risk_reward_ratio == initial_rr
    return SimpleNamespace(
        allowed=True,
        direction="BUY",
        regime="STRONG_TREND_BULL",
        regime_rules=SimpleNamespace(min_rr=1.5, label="Strong Bull Trend"),
        confidence=80.0,
        trade_params=trade_params,
    )


def _patch_order_dependencies(monkeypatch, order_calls):
    async def fake_get_open_positions(*_args, **kwargs):
        if kwargs.get("return_diagnostics"):
            return [], []
        return []

    async def fake_get_symbol_params(_symbol):
        return {
            "symbolInfo": {"digits": 2, "points": 0.01},
            "symbolGroup": {"sl": 200.0, "tp": 0.0},
        }

    async def fake_get_current_quote(_symbol):
        return {"bid": 100.00, "ask": 100.10}

    async def fake_place_market_order(**kwargs):
        order_calls.append(kwargs)
        return TradeResult(True, "position-1", "ORDER_PLACED")

    monkeypatch.setattr(
        live_loop,
        "evaluate_active_entry",
        lambda *_args, **_kwargs: SimpleNamespace(allowed=True, reason=""),
    )
    monkeypatch.setattr(live_loop, "get_open_positions", fake_get_open_positions)
    monkeypatch.setattr(live_loop, "get_symbol_params", fake_get_symbol_params)
    monkeypatch.setattr(live_loop, "get_current_quote", fake_get_current_quote)
    monkeypatch.setattr(live_loop, "place_market_order", fake_place_market_order)


@pytest.mark.asyncio
async def test_valid_initial_rr_invalid_after_rebase_blocks_order(monkeypatch):
    order_calls = []
    _patch_order_dependencies(monkeypatch, order_calls)
    robot = GoldScalperLive()
    decision = _decision(1.5)

    result, positions, stage, reason = await robot._safe_entry_order(
        decision,
        (ACTIVE_STRATEGY_SLOT,),
        "5m",
        datetime(2026, 9, 25, tzinfo=timezone.utc),
        1.0,
    )

    assert decision.trade_params.risk_reward_ratio == 1.49
    assert result is None
    assert positions == []
    assert stage == "FINAL_RR_BELOW_REGIME_MINIMUM"
    assert "Final R:R 1.49 < 1.50" in reason
    assert order_calls == []


@pytest.mark.asyncio
async def test_valid_final_rr_allows_order(monkeypatch):
    order_calls = []
    _patch_order_dependencies(monkeypatch, order_calls)
    robot = GoldScalperLive()
    decision = _decision(2.0)

    result, positions, stage, reason = await robot._safe_entry_order(
        decision,
        (ACTIVE_STRATEGY_SLOT,),
        "5m",
        datetime(2026, 9, 25, tzinfo=timezone.utc),
        1.0,
    )

    assert result is not None
    assert result.success
    assert positions == []
    assert stage == ""
    assert reason == ""
    assert decision.trade_params.risk_reward_ratio == 1.99
    assert len(order_calls) == 1
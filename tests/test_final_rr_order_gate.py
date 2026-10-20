import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from live_trading import redis_ipc
from live_trading.mt5.executor import TradeResult
from live_trading.risk.capital_manager import CapitalInput, calc_trade_parameters
from live_trading.trading import live_loop
from live_trading.trading.entry_signal import (
    entry_signal_comment,
    entry_signal_id,
)
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
        quality_filter=SimpleNamespace(adx=50.0),
        trend=SimpleNamespace(trend="BULLISH"),
        trade_params=trade_params,
    )


def _false_breakout_decision():
    decision = _decision(2.0)
    decision.pa = SimpleNamespace(
        fake_bull_breakout=True,
        fake_bear_breakout=False,
        valid_bull_breakout=False,
        valid_bear_breakout=False,
        bullish_inside_breakout=False,
        bearish_inside_breakout=False,
        bullish_pullback=False,
        bearish_pullback=False,
        near_resistance=False,
        near_support=False,
        breakout_overextended=False,
    )
    decision.quality_filter = SimpleNamespace(is_fake_breakout=True)
    return decision


def _smc_only_false_breakout_decision():
    decision = _decision(2.0)
    decision.pa = SimpleNamespace(
        fake_bull_breakout=False,
        fake_bear_breakout=False,
        valid_bull_breakout=False,
        valid_bear_breakout=False,
        bullish_inside_breakout=False,
        bearish_inside_breakout=False,
        bullish_pullback=False,
        bearish_pullback=False,
        near_resistance=False,
        near_support=False,
        breakout_overextended=False,
    )
    decision.quality_filter = SimpleNamespace(
        adx=50.0,
        is_fake_breakout=True,
    )
    return decision


def _patch_order_dependencies(monkeypatch, order_calls):
    async def fake_get_open_positions(*_args, **kwargs):
        if kwargs.get("return_diagnostics"):
            return [], []
        return []

    async def fake_get_symbol_params(_symbol):
        return {
            "_mtapi_volume_units": "lots",
            "_mtapi_volume_scale": 1.0,
            "symbolInfo": {
                "digits": 2,
                "points": 0.01,
                "tickSize": 0.01,
                "tickValue": 1.0,
                "tickValueCurrency": "USD",
                "contractSize": 100.0,
                "volumeMin": 0.01,
                "volumeStep": 0.01,
                "volumeMax": 50.0,
            },
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

    assert decision.trade_params.risk_reward_ratio == 1.42
    assert result is None
    assert positions == []
    assert stage == "FINAL_RR_BELOW_REGIME_MINIMUM"
    assert "Final R:R 1.42 < 1.50" in reason
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
    assert decision.trade_params.risk_reward_ratio == 1.90
    assert len(order_calls) == 1


@pytest.mark.asyncio
async def test_final_rr_uses_actual_executable_entry_including_spread(monkeypatch):
    order_calls = []
    _patch_order_dependencies(monkeypatch, order_calls)
    robot = GoldScalperLive()
    decision = _decision(1.52)

    result, positions, stage, reason = await robot._safe_entry_order(
        decision,
        (ACTIVE_STRATEGY_SLOT,),
        "5m",
        datetime(2026, 9, 25, tzinfo=timezone.utc),
        1.0,
    )

    assert result is None
    assert positions == []
    assert stage == "FINAL_RR_BELOW_REGIME_MINIMUM"
    assert decision.trade_params.risk_reward_ratio == 1.44
    assert "Final R:R 1.44 < 1.50" in reason
    assert order_calls == []


@pytest.mark.asyncio
async def test_range_uses_its_configured_minimum_rr_not_generic_range_rule(
    monkeypatch,
):
    order_calls = []
    _patch_order_dependencies(monkeypatch, order_calls)
    robot = GoldScalperLive()
    decision = _decision(1.6)
    decision.regime = "RANGE"
    decision.regime_rules = SimpleNamespace(min_rr=2.0, label="Range / Choppy")
    decision.minimum_rr = 1.5

    result, positions, stage, reason = await robot._safe_entry_order(
        decision,
        (ACTIVE_STRATEGY_SLOT,),
        "5m",
        datetime(2026, 9, 25, tzinfo=timezone.utc),
        1.0,
    )

    assert result is not None and result.success
    assert positions == []
    assert stage == ""
    assert reason == ""
    assert decision.trade_params.risk_reward_ratio == 1.52
    assert len(order_calls) == 1


@pytest.mark.asyncio
async def test_final_gate_sizes_one_open_trade_under_aggregate_cap(monkeypatch):
    order_calls = []
    _patch_order_dependencies(monkeypatch, order_calls)
    monkeypatch.setattr(redis_ipc, "REDIS_URL", "")
    existing_position = {
        "ticket": 101,
        "symbol": "XAUUSD",
        "type": "BUY",
        "volume": 0.01,
        "openPrice": 100.0,
        "stopLoss": 90.0,
        "comment": "GSPv4|S=XVTB",
    }

    async def one_open_position(*_args, **kwargs):
        result = ([existing_position], [])
        return result if kwargs.get("return_diagnostics") else [existing_position]

    monkeypatch.setattr(live_loop, "get_open_positions", one_open_position)
    robot = GoldScalperLive()

    result, positions, stage, reason = await robot._safe_entry_order(
        _decision(2.0),
        (ACTIVE_STRATEGY_SLOT,),
        "5m",
        datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc),
        1.0,
        account_balance=10_000.0,
        account_currency="USD",
    )

    assert result is not None and result.success
    assert stage == ""
    assert reason == ""
    assert len(positions) == 1
    sent_risk = order_calls[0]["lot_size"] * (
        abs(100.10 - 97.99) / 0.01
    )
    assert sent_risk <= 100.0
    assert sent_risk + 10.0 <= 200.0
    assert order_calls[0]["volume_step"] == 0.01


@pytest.mark.asyncio
async def test_final_gate_blocks_when_multiple_positions_exceed_aggregate_cap(
    monkeypatch,
):
    order_calls = []
    _patch_order_dependencies(monkeypatch, order_calls)
    monkeypatch.setattr(redis_ipc, "REDIS_URL", "")
    existing_positions = [
        {
            "ticket": ticket,
            "symbol": "XAUUSD",
            "type": "BUY",
            "volume": 0.6,
            "openPrice": 100.0,
            "stopLoss": 98.0,
            "comment": "GSPv4|S=XVTB",
        }
        for ticket in (201, 202)
    ]

    async def over_cap_positions(*_args, **kwargs):
        result = (existing_positions, [])
        return result if kwargs.get("return_diagnostics") else existing_positions

    monkeypatch.setattr(live_loop, "get_open_positions", over_cap_positions)
    robot = GoldScalperLive()

    result, positions, stage, reason = await robot._safe_entry_order(
        _decision(2.0),
        (ACTIVE_STRATEGY_SLOT,),
        "5m",
        datetime(2026, 10, 16, 9, 35, tzinfo=timezone.utc),
        1.0,
        account_balance=10_000.0,
        account_currency="USD",
    )

    assert result is None
    assert len(positions) == 2
    assert stage == "AGGREGATE_RISK_LIMIT"
    assert "240.00 USD" in reason
    assert order_calls == []


@pytest.mark.asyncio
async def test_final_gate_blocks_new_order_if_open_position_has_no_valid_sl(
    monkeypatch,
):
    order_calls = []
    _patch_order_dependencies(monkeypatch, order_calls)
    monkeypatch.setattr(redis_ipc, "REDIS_URL", "")
    unprotected_position = {
        "ticket": 301,
        "symbol": "XAUUSD",
        "type": "BUY",
        "volume": 0.01,
        "openPrice": 100.0,
        "stopLoss": None,
        "comment": "GSPv4|S=XVTB",
    }

    async def unprotected(*_args, **kwargs):
        result = ([unprotected_position], [])
        return result if kwargs.get("return_diagnostics") else [unprotected_position]

    monkeypatch.setattr(live_loop, "get_open_positions", unprotected)
    robot = GoldScalperLive()

    result, positions, stage, reason = await robot._safe_entry_order(
        _decision(2.0),
        (ACTIVE_STRATEGY_SLOT,),
        "5m",
        datetime(2026, 10, 16, 9, 40, tzinfo=timezone.utc),
        1.0,
        account_balance=10_000.0,
        account_currency="USD",
    )

    assert result is None
    assert len(positions) == 1
    assert stage == "OPEN_POSITION_RISK_UNVERIFIABLE"
    assert "SL must be finite and positive" in reason
    assert order_calls == []


@pytest.mark.asyncio
async def test_explicit_false_breakout_is_blocked_at_final_order_boundary(monkeypatch):
    order_calls = []
    _patch_order_dependencies(monkeypatch, order_calls)
    robot = GoldScalperLive()
    decision = _false_breakout_decision()

    result, positions, stage, reason = await robot._safe_entry_order(
        decision,
        (ACTIVE_STRATEGY_SLOT,),
        "5m",
        datetime(2026, 9, 25, tzinfo=timezone.utc),
        1.0,
    )

    assert result is None
    assert positions == []
    assert stage == "BREAKOUT_SAFETY_BLOCKED"
    assert "false breakout" in reason
    assert order_calls == []


@pytest.mark.asyncio
async def test_smc_fake_breakout_flag_does_not_block_final_order(monkeypatch):
    order_calls = []
    _patch_order_dependencies(monkeypatch, order_calls)
    robot = GoldScalperLive()

    result, positions, stage, reason = await robot._safe_entry_order(
        _smc_only_false_breakout_decision(),
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
    assert len(order_calls) == 1


@pytest.mark.asyncio
async def test_unconfirmed_strong_trend_is_blocked_before_position_check(
    monkeypatch,
):
    order_calls = []
    _patch_order_dependencies(monkeypatch, order_calls)
    async def unexpected_position_check(*_args, **_kwargs):
        raise AssertionError("position check must not run after Strong Trend block")

    monkeypatch.setattr(live_loop, "get_open_positions", unexpected_position_check)
    robot = GoldScalperLive()
    decision = _decision(2.0)
    decision.trend = SimpleNamespace(trend="NEUTRAL")
    decision.pa = SimpleNamespace(
        pa_signal="BUY",
        near_resistance=False,
        near_support=False,
        valid_bull_breakout=False,
        bullish_inside_breakout=False,
        bullish_pullback=False,
        fake_bull_breakout=False,
        fake_bear_breakout=False,
        breakout_overextended=False,
    )

    result, positions, stage, reason = await robot._safe_entry_order(
        decision,
        (ACTIVE_STRATEGY_SLOT,),
        "5m",
        datetime(2026, 9, 25, tzinfo=timezone.utc),
        1.0,
    )

    assert result is None
    assert positions == []
    assert stage == "STRONG_TREND_STRUCTURE_BLOCKED"
    assert "no structural Price Action confirmation" in reason
    assert order_calls == []


def test_signal_id_normalizes_timeframe_alias_and_timezone():
    utc_time = datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc)
    tehran_time = datetime.fromisoformat("2026-10-16T13:00:00+03:30")

    assert entry_signal_id("xauusd", "M5", utc_time) == entry_signal_id(
        "XAUUSD", "5m", tehran_time
    )


def test_broker_comment_has_compact_signal_tag_within_mt5_limit():
    signal_id = entry_signal_id(
        "XAUUSD",
        "5m",
        datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc),
    )

    comment = entry_signal_comment("GSPv4|S=XVTB", signal_id)

    assert comment.startswith("GSPv4|S=XVTB|B=")
    assert len(comment) <= 32


@pytest.mark.asyncio
async def test_same_closed_candle_cannot_submit_twice_even_with_timeframe_alias(
    monkeypatch,
):
    order_calls = []
    _patch_order_dependencies(monkeypatch, order_calls)
    monkeypatch.setattr(redis_ipc, "REDIS_URL", "")
    robot = GoldScalperLive()
    bar_time = datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc)

    first = await robot._safe_entry_order(
        _decision(2.0),
        (ACTIVE_STRATEGY_SLOT,),
        "5m",
        bar_time,
        1.0,
    )
    second = await robot._safe_entry_order(
        _decision(2.0),
        (ACTIVE_STRATEGY_SLOT,),
        "M5",
        bar_time,
        1.0,
    )

    assert first[0] is not None and first[0].success
    assert second[0] is None
    assert second[2] == "DUPLICATE_CANDLE_SIGNAL"
    assert len(order_calls) == 1


@pytest.mark.asyncio
async def test_matching_open_broker_position_blocks_the_same_signal(monkeypatch):
    order_calls = []
    _patch_order_dependencies(monkeypatch, order_calls)
    monkeypatch.setattr(redis_ipc, "REDIS_URL", "")
    bar_time = datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc)
    signal_id = entry_signal_id("XAUUSD", "5m", bar_time)
    existing_position = {
        "ticket": 101,
        "symbol": "XAUUSD",
        "type": "BUY",
        "volume": 0.01,
        "comment": entry_signal_comment("GSPv4|S=XVTB", signal_id),
    }

    async def existing_position_check(*_args, **kwargs):
        result = ([existing_position], [])
        return result if kwargs.get("return_diagnostics") else [existing_position]

    monkeypatch.setattr(live_loop, "get_open_positions", existing_position_check)
    robot = GoldScalperLive()

    result, positions, stage, _ = await robot._safe_entry_order(
        _decision(2.0),
        (ACTIVE_STRATEGY_SLOT,),
        "5m",
        bar_time,
        1.0,
    )

    assert result is None
    assert positions[0]["comment"] == existing_position["comment"]
    assert stage == "DUPLICATE_CANDLE_SIGNAL"
    assert order_calls == []


@pytest.mark.asyncio
async def test_distinct_closed_candles_remain_independently_eligible(monkeypatch):
    order_calls = []
    _patch_order_dependencies(monkeypatch, order_calls)
    monkeypatch.setattr(redis_ipc, "REDIS_URL", "")
    robot = GoldScalperLive()
    first_bar = datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc)
    second_bar = datetime(2026, 10, 16, 9, 35, tzinfo=timezone.utc)

    first = await robot._safe_entry_order(
        _decision(2.0),
        (ACTIVE_STRATEGY_SLOT,),
        "5m",
        first_bar,
        1.0,
    )
    second = await robot._safe_entry_order(
        _decision(2.0),
        (ACTIVE_STRATEGY_SLOT,),
        "5m",
        second_bar,
        1.0,
    )

    assert first[0] is not None and first[0].success
    assert second[0] is not None and second[0].success
    assert len(order_calls) == 2
    assert order_calls[0]["comment"] != order_calls[1]["comment"]
    assert all(len(call["comment"]) <= 32 for call in order_calls)


@pytest.mark.asyncio
async def test_failed_order_stays_failed_and_same_signal_is_not_retried(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.values = {}

        def set(self, key, value, *, nx=False, ex=None):
            if nx and key in self.values:
                return None
            self.values[key] = value
            return True

    order_calls = []
    fake_redis = FakeRedis()
    _patch_order_dependencies(monkeypatch, order_calls)
    monkeypatch.setattr(redis_ipc, "REDIS_URL", "redis://test")
    monkeypatch.setattr(redis_ipc, "_get_client", lambda: fake_redis)

    async def failed_order(**kwargs):
        order_calls.append(kwargs)
        return TradeResult(False, None, "broker rejected order")

    monkeypatch.setattr(live_loop, "place_market_order", failed_order)
    robot = GoldScalperLive()
    bar_time = datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc)

    first = await robot._safe_entry_order(
        _decision(2.0),
        (ACTIVE_STRATEGY_SLOT,),
        "5m",
        bar_time,
        1.0,
    )
    second = await robot._safe_entry_order(
        _decision(2.0),
        (ACTIVE_STRATEGY_SLOT,),
        "5m",
        bar_time,
        1.0,
    )

    assert first[0] is not None and not first[0].success
    assert second[0] is None
    assert second[2] == "DUPLICATE_CANDLE_SIGNAL"
    assert len(order_calls) == 1
    assert robot.trade_history == []
    assert json.loads(next(iter(fake_redis.values.values())))["status"] == "FAILED"


@pytest.mark.asyncio
async def test_shared_redis_claim_blocks_parallel_engine_instances(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.values = {}

        def set(self, key, value, *, nx=False, ex=None):
            if nx and key in self.values:
                return None
            self.values[key] = value
            return True

    order_calls = []
    fake_redis = FakeRedis()
    _patch_order_dependencies(monkeypatch, order_calls)
    monkeypatch.setattr(redis_ipc, "REDIS_URL", "redis://test")
    monkeypatch.setattr(redis_ipc, "_get_client", lambda: fake_redis)

    async def delayed_position_check(*_args, **kwargs):
        await asyncio.sleep(0.01)
        return ([], []) if kwargs.get("return_diagnostics") else []

    async def delayed_order(**kwargs):
        order_calls.append(kwargs)
        await asyncio.sleep(0.01)
        return TradeResult(True, "position-1", "ORDER_PLACED")

    monkeypatch.setattr(live_loop, "get_open_positions", delayed_position_check)
    monkeypatch.setattr(live_loop, "place_market_order", delayed_order)
    robots = [GoldScalperLive(), GoldScalperLive()]
    bar_time = datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc)

    results = await asyncio.gather(*[
        robot._safe_entry_order(
            _decision(2.0),
            (ACTIVE_STRATEGY_SLOT,),
            "5m",
            bar_time,
            1.0,
        )
        for robot in robots
    ])

    assert len(order_calls) == 1
    assert sum(result[0] is not None and result[0].success for result in results) == 1
    assert sum(result[2] == "DUPLICATE_CANDLE_SIGNAL" for result in results) == 1


@pytest.mark.asyncio
async def test_configured_but_unavailable_redis_fails_closed(monkeypatch):
    order_calls = []
    _patch_order_dependencies(monkeypatch, order_calls)
    monkeypatch.setattr(redis_ipc, "REDIS_URL", "redis://unavailable")
    monkeypatch.setattr(redis_ipc, "_get_client", lambda: None)
    robot = GoldScalperLive()

    result, _, stage, _ = await robot._safe_entry_order(
        _decision(2.0),
        (ACTIVE_STRATEGY_SLOT,),
        "5m",
        datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc),
        1.0,
    )

    assert result is None
    assert stage == "SIGNAL_DEDUP_UNAVAILABLE"
    assert order_calls == []

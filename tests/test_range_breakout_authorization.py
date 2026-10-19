"""RANGE must not replace the active Trend-breakout strategy."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import live_trading.signals.decision_engine as decision_engine
from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.market_regime import REGIME_RULES


def _candles() -> list[OHLCV]:
    start = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
    history = [
        OHLCV(
            time=(start + timedelta(minutes=5 * index)).isoformat(),
            open=2500.0,
            high=2501.0,
            low=2499.0,
            close=2500.0,
            volume=100.0,
        )
        for index in range(219)
    ]
    # Closed expansion above the prior high, without a same-direction
    # liquidity sweep of the prior lows.
    history.append(
        OHLCV(
            time=(start + timedelta(minutes=5 * 219)).isoformat(),
            open=2500.2,
            high=2502.5,
            low=2500.1,
            close=2502.2,
            volume=150.0,
        )
    )
    return history


def _pa(
    signal: str,
    *,
    structural_breakout: bool = False,
    bullish_engulf: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        pa_signal=signal,
        pa_score=0.8 if signal != "NEUTRAL" else 0.0,
        valid_bull_breakout=structural_breakout,
        valid_bear_breakout=False,
        bullish_inside_breakout=False,
        bearish_inside_breakout=False,
        bullish_pullback=False,
        bearish_pullback=False,
        bullish_engulf=bullish_engulf,
        bearish_engulf=False,
        bullish_pin_bar=False,
        bearish_pin_bar=False,
        strong_bullish=False,
        strong_bearish=False,
        near_demand_zone=False,
        near_supply_zone=False,
        near_support=False,
        near_resistance=False,
        fake_bull_breakout=False,
        fake_bear_breakout=False,
        breakout_overextended=False,
    )


def _run_decision(monkeypatch, *, trend_direction, pa, trend_score=60.0):
    trend = SimpleNamespace(
        trend=trend_direction,
        strength="MODERATE" if trend_direction != "NEUTRAL" else "WEAK",
        score=trend_score if trend_direction != "NEUTRAL" else 0.0,
    )
    regime = SimpleNamespace(
        regime="RANGE",
        rules=REGIME_RULES["RANGE"],
        atr=1.0,
        atr_mean=1.0,
        atr_ratio=1.0,
        adx=18.0,
        description="ADX below trend threshold",
    )
    quality = SimpleNamespace(
        allowed=True,
        blocked_reasons=[],
        adx=18.0,
        is_fake_breakout=False,
    )

    monkeypatch.setattr(
        decision_engine,
        "analyze_smc_structure",
        lambda *_args, **_kwargs: SimpleNamespace(smc_signal="NEUTRAL"),
    )
    monkeypatch.setattr(
        decision_engine,
        "analyze_wyckoff",
        lambda *_args, **_kwargs: SimpleNamespace(wyckoff_signal="NEUTRAL"),
    )
    monkeypatch.setattr(
        decision_engine,
        "analyze_price_action",
        lambda *_args, **_kwargs: pa,
    )
    monkeypatch.setattr(
        decision_engine,
        "analyze_trend",
        lambda *_args, **_kwargs: trend,
    )
    monkeypatch.setattr(
        decision_engine,
        "detect_market_regime",
        lambda *_args, **_kwargs: regime,
    )
    monkeypatch.setattr(
        decision_engine,
        "calc_confidence",
        lambda *_args, **_kwargs: SimpleNamespace(
            confidence=80.0,
            components=SimpleNamespace(total=80.0),
            grade="HIGH",
            reasoning=[],
        ),
    )
    monkeypatch.setattr(
        decision_engine,
        "apply_quality_filter",
        lambda *_args, **_kwargs: quality,
    )
    monkeypatch.setattr(
        decision_engine,
        "detect_order_block_fake_breakout",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        decision_engine,
        "analyze_divergence",
        lambda *_args, **_kwargs: SimpleNamespace(signal="NEUTRAL"),
    )
    monkeypatch.setattr(
        decision_engine,
        "calc_trade_parameters",
        lambda params: SimpleNamespace(
            entry_price=params.entry_price,
            stop_loss=params.entry_price - 1.5,
            take_profit=params.entry_price + 2.25,
            risk_reward_ratio=1.5,
            stop_loss_valid=True,
            stop_loss_reason="",
            min_lot_risk_exceeded=False,
            lot_size=0.01,
            risk_amount=50.0,
            risk_budget=100.0,
            risk_percent=1.0,
        ),
    )

    return decision_engine.run_decision_engine(
        _candles(),
        account_balance=10_000.0,
        timeframe="5m",
        price_action_standalone=True,
        trend_standalone=True,
        range_trading_enabled=True,
        range_min_rr=1.5,
        symbol="XAUUSD",
    )


def test_aligned_trend_breakout_passes_without_range_sweep_or_reversal(monkeypatch):
    decision = _run_decision(
        monkeypatch,
        trend_direction="BULLISH",
        pa=_pa("BUY", structural_breakout=True),
    )

    assert decision.allowed, decision.blocked_reasons
    assert decision.direction == "BUY"
    assert decision.entry_filter.trend
    assert decision.entry_filter.price_action
    assert decision.minimum_rr == 1.5
    assert decision.range_context is not None
    assert not decision.range_context.liquidity_sweep
    assert "liquidity sweep and range-reversal checks are diagnostic only" in (
        decision.range_context.reason
    )


@pytest.mark.parametrize(
    ("trend_direction", "pa", "expected_reason"),
    [
        (
            "NEUTRAL",
            _pa("BUY", structural_breakout=True),
            "aligned Trend confirmation is required",
        ),
        (
            "BULLISH",
            _pa("NEUTRAL"),
            "same-direction Price Action confirmation is required",
        ),
        (
            "BULLISH",
            _pa("BUY", bullish_engulf=True),
            "closed breakout, inside-bar breakout, or pullback/reclaim",
        ),
    ],
)
def test_range_rejects_pa_alone_or_nonstructural_confirmation(
    monkeypatch,
    trend_direction,
    pa,
    expected_reason,
):
    decision = _run_decision(
        monkeypatch,
        trend_direction=trend_direction,
        pa=pa,
    )

    assert not decision.allowed
    assert expected_reason in decision.blocked_reasons[0]

from types import SimpleNamespace

from live_trading.signals import decision_engine
from live_trading.signals.confidence_engine import (
    ConfidenceComponents,
    ConfidenceResult,
)
from live_trading.signals.entry_filter import EntryFilterResult
from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.market_regime import REGIME_RULES, RegimeResult
from live_trading.signals.quality_filter import QualityFilterResult


def _candle() -> OHLCV:
    return OHLCV(
        time="2026-09-25T10:00:00+00:00",
        open=2500.0,
        high=2502.0,
        low=2498.0,
        close=2501.0,
        volume=100.0,
    )


def _pa_with_explicit_false_breakout():
    return SimpleNamespace(
        pa_signal="BUY",
        pa_score=0.9,
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


def test_high_confidence_explicit_false_breakout_is_blocked(monkeypatch):
    pa = _pa_with_explicit_false_breakout()
    trend = SimpleNamespace(
        trend="BULLISH",
        strength="STRONG",
        score=90.0,
    )
    regime = RegimeResult(
        regime="STRONG_TREND_BULL",
        rules=REGIME_RULES["STRONG_TREND_BULL"],
        atr=1.0,
        atr_mean=1.0,
        atr_ratio=1.0,
        adx=35.0,
        description="Strong Bull Trend",
    )
    quality = QualityFilterResult(
        allowed=True,
        blocked_reasons=[],
        session_quality="PRIME",
        adx=35.0,
        is_severe_range=False,
        is_late_entry=False,
        is_low_probability=False,
        is_fake_breakout=False,
        is_weak_volume=False,
        is_low_momentum=False,
    )

    monkeypatch.setattr(
        decision_engine,
        "analyze_smc_structure",
        lambda *_args, **_kwargs: SimpleNamespace(),
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
    monkeypatch.setattr(decision_engine, "analyze_trend", lambda *_args: trend)
    monkeypatch.setattr(
        decision_engine,
        "detect_market_regime",
        lambda *_args, **_kwargs: regime,
    )
    monkeypatch.setattr(
        decision_engine,
        "apply_entry_filter",
        lambda **_kwargs: EntryFilterResult(
            allowed=True,
            direction="BUY",
            confirmation_count=2,
            smc=False,
            trend=True,
            price_action=True,
            wyckoff=False,
        ),
    )
    monkeypatch.setattr(
        decision_engine,
        "analyze_divergence",
        lambda *_args: SimpleNamespace(signal="NEUTRAL"),
    )
    monkeypatch.setattr(
        decision_engine,
        "calc_confidence",
        lambda *_args, **_kwargs: ConfidenceResult(
            confidence=99.0,
            components=ConfidenceComponents(0, 20, 20, 15, 5, 5, 99),
            grade="PRIME",
            reasoning=["high confidence"],
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

    result = decision_engine.run_decision_engine(
        [_candle()],
        account_balance=10_000.0,
        timeframe="M5",
    )

    assert result.confidence == 99.0
    assert result.allowed is False
    assert result.trade_params is None
    assert result.quality_filter.is_fake_breakout is True
    assert any("false breakout" in reason for reason in result.blocked_reasons)
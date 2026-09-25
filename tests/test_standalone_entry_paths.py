from types import SimpleNamespace
from unittest.mock import patch

from live_trading.config import CONF_HARD_MIN, PA_STANDALONE_MIN_SCORE
from live_trading.signals.confidence_engine import _assign_grade
from live_trading.signals.decision_engine import _candidate_direction
from live_trading.signals.entry_filter import apply_entry_filter
from live_trading.signals.price_action_engine import _compute_pa_signal
from live_trading.trading.active_entry_policy import evaluate_active_entry


def test_confidence_30_to_39_allows_clear_trend_or_pa_without_smc_wyckoff():
    confidence = 35.0
    assert CONF_HARD_MIN == 30.0
    assert _assign_grade(confidence, min_conf=40.0) == "MARGINAL"

    trend_entry = apply_entry_filter(
        smc_signal="NEUTRAL",
        ema_trend="BULLISH",
        pa_signal="NEUTRAL",
        wyckoff_signal="NEUTRAL",
        min_confirmations=2,
        trend_score=60.0,
        trend_standalone=True,
        trend_standalone_min_score=55.0,
    )
    assert trend_entry.allowed
    assert trend_entry.direction == "BUY"
    assert not trend_entry.smc
    assert not trend_entry.wyckoff

    pa_entry = apply_entry_filter(
        smc_signal="SELL",
        ema_trend="NEUTRAL",
        pa_signal="BUY",
        wyckoff_signal="SELL",
        min_confirmations=2,
        price_action_standalone=True,
        pa_score=PA_STANDALONE_MIN_SCORE,
        pa_standalone_min_score=PA_STANDALONE_MIN_SCORE,
    )
    assert pa_entry.allowed
    assert pa_entry.direction == "BUY"
    assert not pa_entry.smc
    assert not pa_entry.wyckoff
    candidate = _candidate_direction(
        SimpleNamespace(smc_signal="SELL"),
        SimpleNamespace(wyckoff_signal="SELL"),
        SimpleNamespace(pa_signal="BUY", pa_score=PA_STANDALONE_MIN_SCORE),
        SimpleNamespace(trend="NEUTRAL"),
        price_action_standalone=True,
        pa_standalone_min_score=PA_STANDALONE_MIN_SCORE,
    )
    assert candidate == "BUY"

    active = evaluate_active_entry(
        SimpleNamespace(
            allowed=True,
            direction="BUY",
            regime="STRONG_TREND_BULL",
            trend=SimpleNamespace(trend="NEUTRAL"),
            entry_filter=pa_entry,
        ),
        symbol="XAUUSD",
        timeframe="5m",
    )
    assert active.allowed

    # One clear breakout remains directional even when its rounded score is
    # just below the configured standalone floor.
    with patch(
        "live_trading.signals.price_action_engine.PA_STANDALONE_MIN_SCORE",
        PA_STANDALONE_MIN_SCORE + 0.01,
    ):
        signal, score = _compute_pa_signal(
            bull_engulf=False,
            bear_engulf=False,
            bull_pin=False,
            bear_pin=False,
            strong_bull=False,
            strong_bear=False,
            vbull=True,
            vbear=False,
            fbull=False,
            fbear=False,
            bull_pb=False,
            bear_pb=False,
            bull_inside=False,
            bear_inside=False,
            near_demand=False,
            near_supply=False,
            near_support=False,
            near_resist=False,
        )
    assert score < PA_STANDALONE_MIN_SCORE + 0.01
    assert signal == "BUY"


def test_trend_standalone_requires_strong_aligned_score_when_pa_is_neutral():
    allowed = apply_entry_filter(
        smc_signal="NEUTRAL",
        ema_trend="BULLISH",
        pa_signal="NEUTRAL",
        wyckoff_signal="NEUTRAL",
        min_confirmations=2,
        trend_score=55.0,
        trend_standalone=True,
        trend_standalone_min_score=55.0,
    )
    weak = apply_entry_filter(
        smc_signal="NEUTRAL",
        ema_trend="BULLISH",
        pa_signal="NEUTRAL",
        wyckoff_signal="NEUTRAL",
        min_confirmations=2,
        trend_score=54.99,
        trend_standalone=True,
        trend_standalone_min_score=55.0,
    )

    assert allowed.allowed
    assert allowed.direction == "BUY"
    assert allowed.confirmation_count == 1
    assert not weak.allowed
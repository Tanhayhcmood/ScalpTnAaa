"""Trend-only live-entry authorization tests."""

from types import SimpleNamespace

from live_trading.signals.decision_engine import _candidate_direction
from live_trading.signals.entry_filter import apply_entry_filter
from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.quality_filter import apply_quality_filter


def test_trend_alone_can_open_an_entry():
    result = apply_entry_filter(
        smc_signal="NEUTRAL",
        ema_trend="BULLISH",
        pa_signal="NEUTRAL",
        wyckoff_signal="NEUTRAL",
        min_confirmations=1,
    )

    assert result.allowed is True
    assert result.direction == "BUY"
    assert result.confirmation_count == 1
    assert result.smc is False
    assert result.trend is True
    assert result.price_action is False


def test_diagnostic_engines_do_not_authorize_entries_without_trend():
    result = apply_entry_filter(
        smc_signal="BUY",
        ema_trend="NEUTRAL",
        pa_signal="NEUTRAL",
        wyckoff_signal="BUY",
        min_confirmations=1,
        enabled_strategies=("trend", "price_action"),
    )

    assert result.allowed is False
    assert result.direction == "NEUTRAL"
    assert result.confirmation_count == 0
    assert result.smc is False
    assert result.wyckoff is False


def test_smc_and_wyckoff_disagreement_does_not_veto_trend_pa_vote():
    result = apply_entry_filter(
        smc_signal="BUY",
        ema_trend="BULLISH",
        pa_signal="BUY",
        wyckoff_signal="SELL",
        min_confirmations=2,
    )

    assert result.allowed is True
    assert result.direction == "BUY"


def test_price_action_only_vote_cannot_pass_trend_only_policy():
    result = apply_entry_filter(
        smc_signal="NEUTRAL",
        ema_trend="NEUTRAL",
        pa_signal="BUY",
        wyckoff_signal="NEUTRAL",
        min_confirmations=1,
        price_action_standalone=True,
    )

    assert result.allowed is False
    assert result.direction == "NEUTRAL"
    assert result.confirmation_count == 0


def test_candidate_direction_uses_the_consensus_not_smc_alone():
    result = _candidate_direction(
        SimpleNamespace(smc_signal="NEUTRAL"),
        SimpleNamespace(wyckoff_signal="NEUTRAL"),
        SimpleNamespace(pa_signal="BUY"),
        SimpleNamespace(trend="BULLISH"),
    )

    assert result == "BUY"


def test_candidate_direction_ignores_diagnostic_votes():
    result = _candidate_direction(
        SimpleNamespace(smc_signal="BUY"),
        SimpleNamespace(wyckoff_signal="BUY"),
        SimpleNamespace(pa_signal="SELL"),
        SimpleNamespace(trend="BEARISH"),
    )

    assert result == "SELL"


def test_quality_filter_does_not_reintroduce_an_smc_mandate():
    candles = [
        OHLCV(
            time=f"2026-08-11T12:{index:02d}:00+00:00",
            open=2500.0,
            high=2500.5,
            low=2499.5,
            close=2500.2,
            volume=100.0,
        )
        for index in range(40)
    ]

    blocked_without_pa_path = apply_quality_filter(
        candles,
        smc_signal="NEUTRAL",
        confidence=80.0,
        last_bos_bar=None,
        adx=25.0,
    )
    allowed_pa_path = apply_quality_filter(
        candles,
        smc_signal="NEUTRAL",
        confidence=80.0,
        last_bos_bar=None,
        adx=25.0,
        allow_without_smc=True,
    )

    assert "No SMC direction signal" in blocked_without_pa_path.blocked_reasons
    assert "No SMC direction signal" not in allowed_pa_path.blocked_reasons

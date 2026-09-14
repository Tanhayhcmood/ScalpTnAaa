"""Two-strategy consensus entry policy tests."""

from types import SimpleNamespace

from live_trading.signals.decision_engine import _candidate_direction
from live_trading.signals.entry_filter import apply_entry_filter
from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.quality_filter import apply_quality_filter


def test_any_two_non_smc_strategies_can_open_an_entry():
    result = apply_entry_filter(
        smc_signal="NEUTRAL",
        ema_trend="BULLISH",
        pa_signal="BUY",
        wyckoff_signal="NEUTRAL",
        min_confirmations=2,
    )

    assert result.allowed is True
    assert result.direction == "BUY"
    assert result.confirmation_count == 2
    assert result.smc is False
    assert result.trend is True
    assert result.price_action is True


def test_opposing_two_to_two_vote_is_blocked():
    result = apply_entry_filter(
        smc_signal="BUY",
        ema_trend="BEARISH",
        pa_signal="BUY",
        wyckoff_signal="SELL",
        min_confirmations=2,
    )

    assert result.allowed is False
    assert result.direction == "NEUTRAL"


def test_candidate_direction_uses_the_consensus_not_smc_alone():
    result = _candidate_direction(
        SimpleNamespace(smc_signal="NEUTRAL"),
        SimpleNamespace(wyckoff_signal="NEUTRAL"),
        SimpleNamespace(pa_signal="BUY"),
        SimpleNamespace(trend="BULLISH"),
    )

    assert result == "BUY"


def test_candidate_direction_is_neutral_on_a_tie():
    result = _candidate_direction(
        SimpleNamespace(smc_signal="BUY"),
        SimpleNamespace(wyckoff_signal="SELL"),
        SimpleNamespace(pa_signal="BUY"),
        SimpleNamespace(trend="BEARISH"),
    )

    assert result == "NEUTRAL"


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

from types import SimpleNamespace

import pytest

from live_trading.trading.active_entry_policy import (
    strong_trend_validation_reason,
)


def _decision(
    *,
    regime: str,
    direction: str,
    adx: float,
    trend: str,
    pa_signal: str = "NEUTRAL",
    valid_breakout: bool = False,
    inside_breakout: bool = False,
    pullback: bool = False,
):
    is_buy = direction == "BUY"
    return SimpleNamespace(
        regime=regime,
        direction=direction,
        quality_filter=SimpleNamespace(adx=adx),
        trend=SimpleNamespace(trend=trend),
        pa=SimpleNamespace(
            pa_signal=pa_signal,
            valid_bull_breakout=valid_breakout if is_buy else False,
            valid_bear_breakout=valid_breakout if not is_buy else False,
            bullish_inside_breakout=inside_breakout if is_buy else False,
            bearish_inside_breakout=inside_breakout if not is_buy else False,
            bullish_pullback=pullback if is_buy else False,
            bearish_pullback=pullback if not is_buy else False,
        ),
    )


@pytest.mark.parametrize(
    ("regime", "direction", "trend"),
    [
        ("STRONG_TREND_BULL", "BUY", "BULLISH"),
        ("STRONG_TREND_BEAR", "SELL", "BEARISH"),
    ],
)
def test_adx_driven_strong_trend_with_aligned_trend_is_allowed(
    regime,
    direction,
    trend,
):
    assert strong_trend_validation_reason(
        _decision(
            regime=regime,
            direction=direction,
            adx=50.0,
            trend=trend,
        )
    ) is None


@pytest.mark.parametrize(
    ("regime", "direction", "pa_signal", "event"),
    [
        ("STRONG_TREND_BULL", "BUY", "BUY", "valid_breakout"),
        ("STRONG_TREND_BULL", "BUY", "BUY", "inside_breakout"),
        ("STRONG_TREND_BULL", "BUY", "BUY", "pullback"),
        ("STRONG_TREND_BEAR", "SELL", "SELL", "valid_breakout"),
        ("STRONG_TREND_BEAR", "SELL", "SELL", "inside_breakout"),
        ("STRONG_TREND_BEAR", "SELL", "SELL", "pullback"),
    ],
)
def test_neutral_trend_with_same_direction_structural_pa_is_allowed(
    regime,
    direction,
    pa_signal,
    event,
):
    events = {event: True}
    assert strong_trend_validation_reason(
        _decision(
            regime=regime,
            direction=direction,
            adx=50.0,
            trend="NEUTRAL",
            pa_signal=pa_signal,
            **events,
        )
    ) is None


@pytest.mark.parametrize(
    ("regime", "direction", "trend"),
    [
        ("STRONG_TREND_BULL", "BUY", "NEUTRAL"),
        ("STRONG_TREND_BULL", "BUY", "BEARISH"),
        ("STRONG_TREND_BEAR", "SELL", "NEUTRAL"),
        ("STRONG_TREND_BEAR", "SELL", "BULLISH"),
    ],
)
def test_unconfirmed_adx_driven_strong_trend_is_blocked(
    regime,
    direction,
    trend,
):
    reason = strong_trend_validation_reason(
        _decision(
            regime=regime,
            direction=direction,
            adx=50.0,
            trend=trend,
        )
    )
    assert reason is not None
    assert "no structural Price Action confirmation" in reason


@pytest.mark.parametrize(
    ("regime", "direction", "trend", "pa_signal"),
    [
        ("STRONG_TREND_BULL", "BUY", "BEARISH", "BUY"),
        ("STRONG_TREND_BEAR", "SELL", "BULLISH", "SELL"),
    ],
)
def test_opposite_trend_with_same_direction_structural_pa_is_allowed(
    regime,
    direction,
    trend,
    pa_signal,
):
    assert strong_trend_validation_reason(
        _decision(
            regime=regime,
            direction=direction,
            adx=50.0,
            trend=trend,
            pa_signal=pa_signal,
            valid_breakout=True,
        )
    ) is None


@pytest.mark.parametrize(
    ("regime", "direction", "trend"),
    [
        ("STRONG_TREND_BULL", "BUY", "NEUTRAL"),
        ("STRONG_TREND_BEAR", "SELL", "NEUTRAL"),
    ],
)
def test_adx_below_45_keeps_existing_path(
    regime,
    direction,
    trend,
):
    assert strong_trend_validation_reason(
        _decision(
            regime=regime,
            direction=direction,
            adx=40.0,
            trend=trend,
        )
    ) is None


@pytest.mark.parametrize(
    ("regime", "expected_direction", "wrong_direction"),
    [
        ("STRONG_TREND_BULL", "BUY", "SELL"),
        ("STRONG_TREND_BEAR", "SELL", "BUY"),
    ],
)
def test_strong_regime_direction_mismatch_is_blocked(
    regime,
    expected_direction,
    wrong_direction,
):
    reason = strong_trend_validation_reason(
        _decision(
            regime=regime,
            direction=wrong_direction,
            adx=50.0,
            trend="NEUTRAL",
        )
    )
    assert reason is not None
    assert "does not match regime direction" in reason
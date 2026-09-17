"""Smoke tests for the negative-only higher-timeframe opposition filter."""

from live_trading.signals.mtf_filter import (
    MtfBias,
    evaluate_mtf_opposition,
    mtf_allows_trade,
)


def _bias(trend: str, trend_score: float) -> MtfBias:
    direction = {
        "BULLISH": "BUY",
        "BEARISH": "SELL",
        "NEUTRAL": "NEUTRAL",
    }[trend]
    return MtfBias(
        direction=direction,  # type: ignore[arg-type]
        trend=trend,  # type: ignore[arg-type]
        smc_signal=direction,
        regime="STRONG_TREND_BULL" if trend == "BULLISH" else "STRONG_TREND_BEAR",
        strength="STRONG" if abs(trend_score) >= 55 else "WEAK",
        trend_score=trend_score,
        reasoning=["test"],
    )


def test_strong_bullish_htf_blocks_sell():
    check = evaluate_mtf_opposition(
        _bias("BULLISH", 82.0),
        "SELL",
        opposition_threshold=55.0,
    )
    assert check.htf_trend == "BUY"
    assert check.opposition_strength == 82.0
    assert check.would_block is True


def test_strong_bearish_htf_blocks_buy():
    check = evaluate_mtf_opposition(
        _bias("BEARISH", -78.0),
        "BUY",
        opposition_threshold=55.0,
    )
    assert check.htf_trend == "SELL"
    assert check.opposition_strength == 78.0
    assert check.would_block is True


def test_neutral_htf_does_not_block_by_default():
    check = evaluate_mtf_opposition(
        _bias("NEUTRAL", 0.0),
        "SELL",
        opposition_threshold=55.0,
    )
    assert check.htf_trend == "NEUTRAL"
    assert check.opposition_strength == 0.0
    assert check.would_block is False


def test_weak_opposition_preserves_existing_entry_path():
    check = evaluate_mtf_opposition(
        _bias("BULLISH", 42.0),
        "SELL",
        opposition_threshold=55.0,
    )
    assert check.would_block is False
    assert mtf_allows_trade(
        _bias("BULLISH", 42.0),
        "SELL",
        opposition_threshold=55.0,
    ) == (True, "")


def test_threshold_is_configurable():
    check = evaluate_mtf_opposition(
        _bias("BULLISH", 42.0),
        "SELL",
        opposition_threshold=40.0,
    )
    assert check.would_block is True

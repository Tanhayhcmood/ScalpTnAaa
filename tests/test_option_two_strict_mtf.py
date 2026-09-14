"""Option 2: strict higher-timeframe confirmation tests."""

from live_trading.signals.mtf_filter import MtfBias, mtf_allows_trade


def _bias(
    direction: str = "BUY",
    regime: str = "STRONG_TREND_BULL",
) -> MtfBias:
    return MtfBias(
        direction=direction,  # type: ignore[arg-type]
        trend="BULLISH" if direction == "BUY" else "BEARISH",
        smc_signal=direction,
        regime=regime,
        strength="STRONG",
        reasoning=["test"],
    )


def test_option_two_allows_aligned_entry_at_exact_49_percent_floor():
    assert mtf_allows_trade(
        _bias(), "BUY", confidence=49.0, confirmed_timeframes=2
    ) == (True, "")


def test_option_two_blocks_confidence_below_49_percent():
    allowed, reason = mtf_allows_trade(
        _bias(), "BUY", confidence=48.9, confirmed_timeframes=2
    )
    assert not allowed
    assert "confidence" in reason.lower()


def test_option_two_blocks_neutral_htf():
    allowed, reason = mtf_allows_trade(
        _bias(direction="NEUTRAL", regime="RANGE"),
        "BUY",
        confidence=90.0,
        confirmed_timeframes=2,
    )
    assert not allowed
    assert "neutral" in reason.lower()


def test_option_two_blocks_range_htf_even_when_directional():
    allowed, reason = mtf_allows_trade(
        _bias(direction="BUY", regime="RANGE"),
        "BUY",
        confidence=90.0,
        confirmed_timeframes=2,
    )
    assert not allowed
    assert "range" in reason.lower()


def test_option_three_blocks_directional_bias_with_neutral_h1_trend():
    bias = _bias(direction="BUY", regime="LOW_VOLATILITY")
    bias.trend = "NEUTRAL"

    allowed, reason = mtf_allows_trade(
        bias,
        "BUY",
        confidence=90.0,
        confirmed_timeframes=2,
    )

    assert not allowed
    assert "neutral" in reason.lower()


def test_option_three_blocks_neutral_h1_regime_even_when_directional():
    allowed, reason = mtf_allows_trade(
        _bias(direction="SELL", regime="NEUTRAL"),
        "SELL",
        confidence=90.0,
        confirmed_timeframes=2,
    )

    assert not allowed
    assert "neutral" in reason.lower()


def test_option_two_blocks_missing_htf_data():
    allowed, reason = mtf_allows_trade(
        None, "BUY", confidence=90.0, confirmed_timeframes=0
    )
    assert not allowed
    assert "unavailable" in reason.lower()


def test_option_two_blocks_opposing_htf_regardless_of_strength():
    allowed, reason = mtf_allows_trade(
        _bias(direction="SELL", regime="WEAK_TREND_BEAR"),
        "BUY",
        confidence=90.0,
        confirmed_timeframes=2,
    )
    assert not allowed
    assert "wants buy" in reason.lower()


def test_option_two_requires_two_timeframe_confirmations():
    allowed, reason = mtf_allows_trade(
        _bias(), "BUY", confidence=90.0, confirmed_timeframes=1
    )
    assert not allowed
    assert "timeframe" in reason.lower()
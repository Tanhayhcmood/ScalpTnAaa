from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.market_regime import (
    STRONG_ADX_TREND_THRESHOLD,
    detect_market_regime,
)
from live_trading.signals.trend_engine import TrendResult
from live_trading.signals.wyckoff_engine import WyckoffResult


def _trend_neutral() -> TrendResult:
    return TrendResult(
        ema50=100.0,
        ema100=100.0,
        ema200=100.0,
        trend="NEUTRAL",
        strength="WEAK",
        score=-4.0,
        slope50=-0.1,
        slope100=-0.1,
    )


def _wyckoff_neutral() -> WyckoffResult:
    return WyckoffResult(
        phase="NEUTRAL",
        spring=False,
        upthrust=False,
        volume_confirmed=False,
        wyckoff_signal="NEUTRAL",
        wyckoff_score=0.0,
    )


def _downtrend_candles(count: int = 80) -> list[OHLCV]:
    return [
        OHLCV(
            time=f"2026-09-22T03:{index:02d}:00Z",
            open=500.0 - index,
            high=500.2 - index,
            low=499.8 - index,
            close=500.0 - index,
            volume=100.0,
        )
        for index in range(count)
    ]


def _flat_candles(count: int = 80) -> list[OHLCV]:
    return [
        OHLCV(
            time=f"2026-09-22T03:{index:02d}:00Z",
            open=500.0,
            high=500.1,
            low=499.9,
            close=500.0,
            volume=100.0,
        )
        for index in range(count)
    ]


def test_strong_adx_direction_overrides_neutral_ema_structure():
    result = detect_market_regime(
        _downtrend_candles(),
        _trend_neutral(),
        _wyckoff_neutral(),
    )

    assert result.adx >= STRONG_ADX_TREND_THRESHOLD
    assert result.regime == "STRONG_TREND_BEAR"
    assert result.rules.label == "Strong Bear Trend"


def test_low_adx_sideways_market_remains_range():
    result = detect_market_regime(
        _flat_candles(),
        _trend_neutral(),
        _wyckoff_neutral(),
    )

    assert result.adx < STRONG_ADX_TREND_THRESHOLD
    assert result.regime == "RANGE"
    assert result.rules.label == "Range / Choppy"
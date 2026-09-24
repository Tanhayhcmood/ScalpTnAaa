from types import SimpleNamespace

from live_trading.signals.confidence_engine import _calc_liquidity_score
from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.range_strategy import _latest_sweep, evaluate_range_entry


def _candles() -> list[OHLCV]:
    history = [
        OHLCV(
            time=f"2026-09-25T12:{index:02d}:00+00:00",
            open=105.0,
            high=110.0,
            low=100.0,
            close=105.0,
            volume=100.0,
        )
        for index in range(20)
    ]
    return history + [
        OHLCV(
            time="2026-09-25T12:20:00+00:00",
            open=101.5,
            high=102.1,
            low=99.5,
            close=101.8,
            volume=150.0,
        )
    ]


def test_liquidity_is_derived_from_candles_not_smc():
    candles = _candles()
    pa = SimpleNamespace(
        bullish_engulf=False,
        bearish_engulf=False,
        bullish_pin_bar=True,
        bearish_pin_bar=False,
        strong_bullish=False,
        strong_bearish=False,
    )

    assert _latest_sweep(candles, "BUY", len(candles) - 1)
    range_result = evaluate_range_entry(
        candles,
        "BUY",
        smc=object(),
        pa=pa,
        confirmation_count=1,
        min_confirmations=1,
    )
    assert range_result.valid
    assert range_result.liquidity_sweep

    score, reasons = _calc_liquidity_score(candles, "BUY")
    assert score == 5.0
    assert "Independent bullish liquidity sweep" in reasons
    assert "Repeated candle liquidity level" in reasons
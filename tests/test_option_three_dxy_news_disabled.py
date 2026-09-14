"""Option 3: DXY and News Filter must not affect entry decisions."""

from live_trading.signals.confidence_engine import calc_confidence
from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.market_regime import detect_market_regime
from live_trading.signals.price_action_engine import analyze_price_action
from live_trading.signals.smc_engine import analyze_smc_structure
from live_trading.signals.trend_engine import analyze_trend
from live_trading.signals.wyckoff_engine import analyze_wyckoff
from live_trading.signals.quality_filter import apply_quality_filter


def _candles(count: int = 80) -> list[OHLCV]:
    candles = []
    price = 2500.0
    for index in range(count):
        close = price + (0.35 if index % 2 == 0 else -0.08)
        hour = 12 + index // 60
        minute = index % 60
        candles.append(
            OHLCV(
                time=f"2026-08-11T{hour:02d}:{minute:02d}:00+00:00",
                open=price,
                high=max(price, close) + 0.20,
                low=min(price, close) - 0.20,
                close=close,
                volume=100.0,
            )
        )
        price = close
    return candles


def test_dxy_signal_does_not_change_confidence_or_components():
    candles = _candles()
    smc = analyze_smc_structure(candles)
    wyckoff = analyze_wyckoff(candles)
    pa = analyze_price_action(candles)
    trend = analyze_trend(candles)
    regime = detect_market_regime(candles, trend, wyckoff, False)

    neutral = calc_confidence(
        smc, wyckoff, pa, trend, regime, "PRIME", "BUY",
        dxy_signal="NEUTRAL",
    )
    opposing = calc_confidence(
        smc, wyckoff, pa, trend, regime, "PRIME", "BUY",
        dxy_signal="BULLISH_DXY",
    )

    assert opposing.confidence == neutral.confidence
    assert opposing.components.dxy_score == 0.0
    assert "DXY rising — headwind against gold BUY" not in opposing.reasoning


def test_news_block_does_not_change_quality_filter_entry_result():
    candles = _candles()
    without_news = apply_quality_filter(
        candles, "BUY", 80.0, None, adx=30.0, news_blocked=False
    )
    with_news = apply_quality_filter(
        candles, "BUY", 80.0, None, adx=30.0,
        news_blocked=True, news_reason="test blackout",
    )

    assert with_news.allowed == without_news.allowed
    assert with_news.is_news_blocked is False
    assert "test blackout" not in with_news.blocked_reasons
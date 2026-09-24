"""Decision telemetry must explain blocked signals without changing the gates."""

from types import SimpleNamespace

from live_trading.signals.decision_engine import (
    _candidate_direction,
    _make_neutral,
    describe_strategy,
)
from live_trading.signals.entry_filter import EntryFilterResult, apply_entry_filter
from live_trading.signals.market_regime import REGIME_RULES, RegimeResult
from live_trading.signals.gold_engine import OHLCV


def _candle():
    return OHLCV(
        time="2026-09-15T10:30:00+00:00",
        open=4275.0,
        high=4278.0,
        low=4272.0,
        close=4274.0,
        volume=100.0,
    )


def _signals():
    return (
        SimpleNamespace(smc_signal="SELL", current_price=4274.0, order_blocks=[], fair_value_gaps=[]),
        SimpleNamespace(wyckoff_signal="NEUTRAL"),
        SimpleNamespace(
            pa_signal="NEUTRAL",
            pa_score=0.0,
            bullish_engulf=False,
            bearish_engulf=False,
            valid_bull_breakout=False,
            valid_bear_breakout=False,
            fake_bull_breakout=False,
            fake_bear_breakout=False,
            inside_bar_detected=False,
            inside_bar_depth=0,
            bullish_inside_breakout=False,
            bearish_inside_breakout=False,
            inside_bar_breakout_level=None,
            inside_bar_breakout_strength=0.0,
        ),
        SimpleNamespace(trend="BEARISH"),
    )


def test_regime_block_preserves_direction_regime_and_votes():
    smc, wyckoff, pa, trend = _signals()
    entry_filter = EntryFilterResult(
        allowed=True,
        direction="SELL",
        confirmation_count=1,
        smc=False,
        trend=True,
        price_action=False,
        wyckoff=False,
    )
    accumulation = RegimeResult(
        regime="ACCUMULATION",
        rules=REGIME_RULES["ACCUMULATION"],
        atr=4.0,
        atr_mean=4.0,
        atr_ratio=1.0,
        adx=17.5,
        description="Wyckoff Accumulation",
    )

    result = _make_neutral(
        smc,
        wyckoff,
        pa,
        trend,
        ['Regime "Wyckoff Accumulation" does not allow SHORT'],
        regime_result=accumulation,
        entry_filter=entry_filter,
        direction="SELL",
    )

    assert result.allowed is False
    assert result.direction == "SELL"
    assert result.regime == "ACCUMULATION"
    assert result.regime_label == "Wyckoff Accumulation"
    assert result.quality_filter.adx == 17.5
    assert result.entry_filter is entry_filter


def test_blocked_decision_telemetry_reports_aligned_confirmations():
    smc, wyckoff, pa, trend = _signals()
    entry_filter = EntryFilterResult(
        allowed=True,
        direction="SELL",
        confirmation_count=1,
        smc=False,
        trend=True,
        price_action=False,
        wyckoff=False,
    )

    result = _make_neutral(
        smc,
        wyckoff,
        pa,
        trend,
        ["range edge required"],
        entry_filter=entry_filter,
        direction="SELL",
    )
    telemetry = __import__(
        "live_trading.signals.decision_engine",
        fromlist=["describe_strategy"],
    ).describe_strategy(result)

    assert telemetry["direction"] == "SELL"
    assert telemetry["confirmation_count"] == 1
    assert telemetry["confirmations"] == [
        "Trend (EMA alignment)",
    ]
    assert telemetry["confirmation_total"] == 2
    assert telemetry["consensus"]["total"] == 2


def test_consensus_excludes_smc_and_wyckoff_from_trend_pa_entry_vote():
    smc, wyckoff, pa, trend = _signals()
    smc.smc_signal = "SELL"
    wyckoff.wyckoff_signal = "SELL"
    pa.pa_signal = "BUY"
    trend.trend = "BULLISH"

    candidate = _candidate_direction(smc, wyckoff, pa, trend)
    actual = apply_entry_filter(
        smc_signal=smc.smc_signal,
        ema_trend=trend.trend,
        pa_signal=pa.pa_signal,
        wyckoff_signal=wyckoff.wyckoff_signal,
        min_confirmations=2,
    )
    diagnostics_neutral = apply_entry_filter(
        smc_signal="NEUTRAL",
        ema_trend=trend.trend,
        pa_signal=pa.pa_signal,
        wyckoff_signal="NEUTRAL",
        min_confirmations=2,
    )

    assert candidate == "BUY"
    assert actual == diagnostics_neutral
    assert actual.allowed is True
    assert actual.direction == "BUY"
    assert actual.confirmation_count == 2
    assert actual.trend is True
    assert actual.price_action is True
    assert actual.smc is False
    assert actual.wyckoff is False

    decision = _make_neutral(
        smc,
        wyckoff,
        pa,
        trend,
        ["telemetry-only test"],
        entry_filter=actual,
        direction=candidate,
    )
    consensus = describe_strategy(decision)["consensus"]

    assert consensus["candidate"] == "BUY"
    assert consensus["engines"] == {"trend": "BUY", "price_action": "BUY"}
    assert consensus["confirmed"] == 2
    assert consensus["total"] == 2
    assert consensus["allowed"] is True
    assert consensus["informational_engines"] == {
        "smc": "SELL",
        "wyckoff": "SELL",
    }
    assert consensus["informational_only"] == {
        "engines": ["smc", "wyckoff"],
        "excluded_from": ["candidate", "confirmed", "allowed", "total"],
    }
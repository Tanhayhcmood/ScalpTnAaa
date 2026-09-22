from live_trading.signals.decision_engine import _resolve_entry_policy
from live_trading.signals.market_regime import REGIME_RULES, RegimeResult
from live_trading.signals.trend_engine import TrendResult


def test_entry_policy_regime_uses_local_entry_timeframe_regime():
    """An HTF RANGE context must not relabel a strong local entry regime."""
    regime = RegimeResult(
        regime="STRONG_TREND_BEAR",
        rules=REGIME_RULES["STRONG_TREND_BEAR"],
        atr=1.0,
        atr_mean=1.0,
        atr_ratio=1.0,
        adx=56.86,
        description="strong directional ADX",
    )
    trend = TrendResult(
        ema50=0.0,
        ema100=0.0,
        ema200=0.0,
        trend="NEUTRAL",
        strength="WEAK",
    )

    strength, policy_regime = _resolve_entry_policy(
        regime,
        trend,
        regime_strength="WEAK",
        regime_context="RANGE",
    )

    assert strength == "WEAK"
    assert policy_regime == "STRONG_TREND_BEAR"
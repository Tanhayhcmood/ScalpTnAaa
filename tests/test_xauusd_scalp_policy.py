"""Additional policy invariants for the XAUUSD M5 strategy branch."""


def test_supported_regime_policy_is_explicit():
    from live_trading.trading.active_entry_policy import (
        _ALLOWED_SCALP_REGIMES,
    )

    assert "RANGE" not in _ALLOWED_SCALP_REGIMES
    assert "HIGH_VOLATILITY" not in _ALLOWED_SCALP_REGIMES
    assert "LOW_VOLATILITY" not in _ALLOWED_SCALP_REGIMES

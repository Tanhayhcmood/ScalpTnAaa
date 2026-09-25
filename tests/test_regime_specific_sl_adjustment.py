import pytest

from live_trading.risk.capital_manager import CapitalInput, calc_trade_parameters
from live_trading.signals.decision_engine import _protective_sl_atr_multiplier
from live_trading.signals.market_regime import REGIME_RULES, RegimeResult


def _regime(name: str) -> RegimeResult:
    return RegimeResult(
        regime=name,
        rules=REGIME_RULES[name],
        atr=1.0,
        atr_mean=1.0,
        atr_ratio=1.0,
        adx=30.0,
        description="test",
    )


@pytest.mark.parametrize(
    ("name", "existing_adjustment"),
    [
        ("STRONG_TREND_BULL", 1.0),
        ("WEAK_TREND_BULL", 0.9),
        ("PULLBACK_BULL", 0.95),
        ("RANGE", 0.8),
        ("HIGH_VOLATILITY", 1.3),
    ],
)
def test_regime_adjustment_reaches_bounded_atr_fallback(name, existing_adjustment):
    base_multiplier = 3.0
    regime = _regime(name)
    expected_multiplier = base_multiplier * existing_adjustment

    assert regime.rules.sl_atr_mult_adjust == existing_adjustment
    effective_multiplier = _protective_sl_atr_multiplier(
        regime,
        base_multiplier,
        low_volatility_add=0.5,
    )
    assert effective_multiplier == pytest.approx(expected_multiplier)

    result = calc_trade_parameters(
        CapitalInput(
            direction="BUY",
            entry_price=100.0,
            atr=1.0,
            account_balance=10_000.0,
            sl_atr_multiplier=effective_multiplier,
            sl_min_atr_multiplier=1.5,
        )
    )

    # The existing capital-manager floor/cap remains authoritative. In
    # particular, HIGH_VOLATILITY's 3.9x adjusted input is capped at 3.5x.
    bounded_multiplier = min(max(expected_multiplier, 1.5), 3.5)
    assert result.stop_loss_mode == "ATR_FALLBACK"
    assert result.stop_loss == pytest.approx(100.0 - bounded_multiplier)
    assert result.sl_distance_usd <= 3.5
    assert result.risk_amount <= result.risk_budget


def test_regime_adjustment_preserves_structural_stop_and_risk_envelope():
    weak_trend = _regime("WEAK_TREND_BULL")
    effective_multiplier = _protective_sl_atr_multiplier(
        weak_trend,
        base_multiplier=3.0,
        low_volatility_add=0.5,
    )

    valid_structure = calc_trade_parameters(
        CapitalInput(
            direction="BUY",
            entry_price=100.0,
            atr=1.0,
            account_balance=10_000.0,
            support_level=97.5,
            sl_atr_multiplier=effective_multiplier,
            sl_min_atr_multiplier=1.5,
            structure_buffer_atr=0.2,
        )
    )
    assert valid_structure.stop_loss_valid is True
    assert valid_structure.stop_loss_mode == "STRUCTURAL"
    assert valid_structure.stop_loss == 97.3
    assert valid_structure.sl_distance_usd <= effective_multiplier

    too_far_structure = calc_trade_parameters(
        CapitalInput(
            direction="BUY",
            entry_price=100.0,
            atr=1.0,
            account_balance=10_000.0,
            support_level=97.4,
            sl_atr_multiplier=effective_multiplier,
            sl_min_atr_multiplier=1.5,
            structure_buffer_atr=0.2,
        )
    )
    assert too_far_structure.stop_loss_valid is False
    assert too_far_structure.stop_loss_mode == "STRUCTURE_TOO_FAR"
    assert too_far_structure.sl_distance_usd <= effective_multiplier


def test_low_volatility_keeps_its_existing_additive_adjustment_only():
    low_volatility = _regime("LOW_VOLATILITY")

    effective_multiplier = _protective_sl_atr_multiplier(
        low_volatility,
        base_multiplier=3.0,
        low_volatility_add=0.5,
    )

    assert effective_multiplier == 3.5
    assert effective_multiplier != (
        3.0 * low_volatility.rules.sl_atr_mult_adjust + 0.5
    )
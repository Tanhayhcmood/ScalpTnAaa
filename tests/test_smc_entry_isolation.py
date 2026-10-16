from types import SimpleNamespace

import pytest

from live_trading.signals.confidence_engine import _calc_smc_score


@pytest.mark.parametrize("candidate", ["BUY", "SELL"])
def test_smc_structure_never_contributes_to_trade_confidence(candidate):
    smc = SimpleNamespace(
        smc_signal="BUY" if candidate == "BUY" else "SELL",
        smc_score=100.0,
        order_blocks=[{"top": 2600.0, "bottom": 2500.0}],
        fair_value_gaps=[{"top": 2600.0, "bottom": 2500.0}],
    )

    score, reasons = _calc_smc_score(smc, candidate)

    assert score == 0.0
    assert reasons == []

from live_trading.trading.entry_gates import evaluate_spread_atr_gate


def test_spread_atr_gate_allows_equal_fraction_and_blocks_above():
    assert evaluate_spread_atr_gate(0.30, 2.0, max_spread_atr_fraction=0.15) == (
        True,
        "",
    )

    allowed, reason = evaluate_spread_atr_gate(
        0.3001, 2.0, max_spread_atr_fraction=0.15
    )

    assert not allowed
    assert "M5 ATR(14)" in reason
    assert "0.3000" in reason


def test_spread_atr_gate_blocks_unavailable_atr():
    allowed, reason = evaluate_spread_atr_gate(
        0.10, 0.0, max_spread_atr_fraction=0.15
    )

    assert not allowed
    assert reason == "M5 ATR(14) unavailable or invalid"

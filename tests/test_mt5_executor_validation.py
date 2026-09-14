from live_trading.mt5.executor import _normalise_lot


def test_normalise_lot_rejects_non_finite_or_non_positive_values():
    for value in (0, -0.01, float("nan"), float("inf")):
        try:
            _normalise_lot(value)
        except ValueError:
            continue
        raise AssertionError(f"invalid lot value was accepted: {value!r}")


def test_normalise_lot_keeps_broker_step():
    assert _normalise_lot(0.014) == 0.01
    assert _normalise_lot(0.016) == 0.02
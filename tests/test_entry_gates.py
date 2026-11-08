from datetime import datetime, timezone

from live_trading.trading.entry_gates import evaluate_entry_gates


def _evaluate(now_utc, spread_price=0.25):
    return evaluate_entry_gates(
        now_utc,
        spread_price,
        max_spread_price=0.50,
        trade_window_utc_start=7,
        trade_window_utc_end=20,
        friday_last_entry_utc_hour=19,
    )


def test_trading_window_edges_are_start_inclusive_and_end_exclusive():
    assert _evaluate(datetime(2026, 11, 4, 7, 0, tzinfo=timezone.utc))[0]
    assert not _evaluate(datetime(2026, 11, 4, 6, 59, tzinfo=timezone.utc))[0]
    assert _evaluate(datetime(2026, 11, 4, 19, 59, tzinfo=timezone.utc))[0]
    allowed, reasons = _evaluate(datetime(2026, 11, 4, 20, 0, tzinfo=timezone.utc))
    assert not allowed
    assert any("trading window" in reason for reason in reasons)


def test_friday_cutoff_blocks_at_configured_hour():
    assert _evaluate(datetime(2026, 11, 6, 18, 59, tzinfo=timezone.utc))[0]
    allowed, reasons = _evaluate(datetime(2026, 11, 6, 19, 0, tzinfo=timezone.utc))
    assert not allowed
    assert any("Friday entry cutoff" in reason for reason in reasons)


def test_spread_limit_allows_equal_value_and_blocks_above_it():
    assert _evaluate(
        datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc), 0.50
    )[0]
    allowed, reasons = _evaluate(
        datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc), 0.5001
    )
    assert not allowed
    assert any("Spread" in reason for reason in reasons)


def test_missing_spread_is_reported_as_blocking_reason():
    allowed, reasons = _evaluate(
        datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc), None
    )
    assert not allowed
    assert "Spread unavailable" in reasons

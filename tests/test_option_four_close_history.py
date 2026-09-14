"""Option 4 regression tests: exact MT5 close details and reason detection."""

from live_trading.trading.live_loop import classify_close_reason


def _entry():
    return {
        "position_id": "123",
        "direction": "BUY",
        "entry": 2350.0,
        "sl": 2345.0,
        "tp": 2360.0,
    }


def test_close_reason_uses_broker_comment_for_tp():
    assert classify_close_reason(
        {"closeComment": "take profit", "closePrice": 2360.0},
        _entry(),
    ) == "TP"


def test_close_reason_uses_price_for_original_sl():
    assert classify_close_reason(
        {"closePrice": 2345.0},
        _entry(),
    ) == "SL"


def test_close_reason_detects_trailing_sl_from_last_trailing_price():
    entry = _entry()
    entry["sl"] = 2345.0
    assert classify_close_reason(
        {"closePrice": 2354.5},
        entry,
        trailing_stop=2354.5,
    ) == "Trailing"


def test_close_reason_does_not_guess_when_no_stop_or_target_matches():
    assert classify_close_reason(
        {"closePrice": 2351.25},
        _entry(),
    ) == "Manual"
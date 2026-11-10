from types import SimpleNamespace

import pytest

from live_trading.trading.active_entry_policy import (
    ACTIVE_ENTRY_STRATEGY,
    evaluate_active_entry,
)
from live_trading.utils import canonical_symbol


@pytest.mark.parametrize(
    "raw",
    [
        "XAUUSD",
        "XAUUSD_i",
        "XAUUSD_I",
        "xauusd.pro",
        "XAUUSDm",
        "XAUUSDb",
        "XAUUSD.a",
        "XAUUSD.",
        "GOLD",
        "GOLD#",
        "GOLD.",
    ],
)
def test_gold_symbols_canonicalize_to_xauusd(raw):
    assert canonical_symbol(raw) == "XAUUSD"


@pytest.mark.parametrize("raw", ["XAUEUR", "XAUGBP", "XAGUSD", "EURUSD", "", None])
def test_non_gold_symbols_remain_distinct_and_none_is_safe(raw):
    result = canonical_symbol(raw)
    assert result != "XAUUSD"
    if raw is None:
        assert result == ""


def test_symbol_alias_override_is_safe_and_malformed_entries_are_ignored(monkeypatch):
    monkeypatch.setenv(
        "SYMBOL_ALIASES",
        "GOLD-CUSTOM=XAUUSD,XAUUSD_I=EURUSD,NOEQUALS,=XAUUSD,"
        "GOLD-BAD=,XAUUSD_I==XAUUSD,EURUSD=XAUUSD,XAUEUR=XAUUSD",
    )

    assert canonical_symbol("GOLD-CUSTOM") == "XAUUSD"
    # Explicit overrides take precedence over built-ins, and fail closed.
    assert canonical_symbol("XAUUSD_I") == "EURUSD"
    # Non-gold instruments cannot be aliased into the active gold symbol.
    assert canonical_symbol("EURUSD") == "EURUSD"
    assert canonical_symbol("XAUEUR") == "XAUEUR"


def _approved_decision():
    return SimpleNamespace(
        allowed=True,
        direction="BUY",
        regime="STRONG_TREND_BULL",
        trend=SimpleNamespace(trend="BULLISH"),
        pa=SimpleNamespace(
            pa_signal="BUY",
            valid_bull_breakout=True,
            valid_bear_breakout=False,
            bullish_inside_breakout=False,
            bearish_inside_breakout=False,
            bullish_pullback=False,
            bearish_pullback=False,
        ),
        entry_filter=SimpleNamespace(
            trend=True,
            price_action=True,
            smc=False,
            wyckoff=False,
        ),
    )


def test_active_entry_policy_allows_gold_suffix_and_blocks_other_instruments():
    allowed = evaluate_active_entry(
        _approved_decision(),
        symbol="XAUUSD_i",
        timeframe="5m",
    )
    blocked = evaluate_active_entry(
        _approved_decision(),
        symbol="EURUSD",
        timeframe="5m",
    )

    assert allowed.allowed is True
    assert allowed.strategy == ACTIVE_ENTRY_STRATEGY
    assert blocked.allowed is False
    assert "raw='EURUSD'" in blocked.reason
    assert "canonical='EURUSD'" in blocked.reason
    assert "allowed='XAUUSD'" in blocked.reason

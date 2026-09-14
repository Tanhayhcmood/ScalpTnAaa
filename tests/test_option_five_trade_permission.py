"""Option 5 regression tests.

The signal engine's ``allowed`` value is an analysis result.  The panel must
receive a separate final entry permission so a signal cannot be mistaken for
an order authorization.
"""

import json
from unittest.mock import patch


def test_signal_allowed_is_separate_from_final_trade_permission(tmp_path):
    from live_trading.utils import state_writer

    state_path = tmp_path / "robot_state.json"
    with patch.object(state_writer, "STATE_FILE", str(state_path)):
        state_writer.write_robot_state(
            status="SCANNING",
            decision=None,
            open_position=None,
            account_info={},
            trade_history=[],
            loop_count=1,
            trade_permission={
                "allowed": False,
                "stage": "SIGNAL_BLOCKED",
                "reasons": ["Signal did not pass the entry strategy"],
            },
            extra=None,
        )

    with state_path.open() as handle:
        state = json.load(handle)

    assert state["last_decision"] is None
    assert state["trade_permission"]["allowed"] is False
    assert state["trade_permission"]["stage"] == "SIGNAL_BLOCKED"
    assert state["trade_permission"]["reasons"]


def test_legacy_allowed_field_is_not_the_final_permission(tmp_path):
    from live_trading.utils import state_writer

    state_path = tmp_path / "robot_state.json"

    class Signal:
        allowed = True
        direction = "BUY"
        confidence = 90.0
        grade = "A"
        regime = "TREND"
        regime_label = "Directional"
        reasoning = []
        blocked_reasons = []

        class Components:
            smc_score = trend_score = pa_score = wyckoff_score = 0.0
            liquidity_score = volatility_score = total = 0.0

        components = Components()
        trade_params = None

    with patch.object(state_writer, "STATE_FILE", str(state_path)):
        state_writer.write_robot_state(
            status="SCANNING",
            decision=Signal(),
            open_position=None,
            account_info={},
            trade_history=[],
            loop_count=1,
            trade_permission={
                "allowed": False,
                "stage": "GUARDIAN_HALTED",
                "reasons": ["Daily loss limit reached"],
            },
            extra=None,
        )

    with state_path.open() as handle:
        state = json.load(handle)

    assert state["last_decision"]["allowed"] is True
    assert state["last_decision"]["signal_allowed"] is True
    assert state["trade_permission"]["allowed"] is False
    assert state["trade_permission"]["stage"] == "GUARDIAN_HALTED"
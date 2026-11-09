from datetime import datetime, timezone

from live_trading.trading.decision_funnel import DecisionFunnel


def test_counter_tracks_first_blocker_gate_only_and_rolling_window():
    now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    funnel = DecisionFunnel(clock=lambda: now)

    assert funnel.begin("2026-11-09T11:55:00Z", "M5")
    funnel.set_candidate(True)
    funnel.block("CONF_MIN")
    funnel.block("NEWS")
    funnel.finish()

    assert funnel.begin("2026-11-09T12:00:00Z", "5m")
    funnel.set_candidate(True)
    funnel.would_block("SPREAD")
    funnel.finish(passed=True)

    snapshot = funnel.snapshot()
    assert snapshot["bars"] == 2
    assert snapshot["candidates"] == 2
    assert snapshot["blocked_by"] == {"CONF_MIN": 1}
    assert snapshot["blocked_only_by"] == {"SPREAD": 1}
    assert snapshot["rolling_24h"]["bars"] == 2
    assert snapshot["rolling_24h"]["blocked_only_by"] == {"SPREAD": 1}
    assert snapshot["rolling_24h"]["passed"] == 1

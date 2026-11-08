import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from live_trading import redis_ipc, server
from live_trading.trading import live_loop


class FakeRedisLists:
    def __init__(self):
        self.values = {}

    def lpush(self, key, value):
        self.values.setdefault(key, []).insert(0, value)

    def ltrim(self, key, start, end):
        self.values[key] = self.values.get(key, [])[start : end + 1]

    def lrange(self, key, start, end):
        values = self.values.get(key, [])
        return values[start:] if end == -1 else values[start : end + 1]

    def lset(self, key, index, value):
        self.values[key][index] = value


def test_redis_trade_features_keep_latest_500_and_update_close(monkeypatch):
    client = FakeRedisLists()
    monkeypatch.setattr(redis_ipc, "_get_client", lambda: client)

    for ticket in range(501):
        assert redis_ipc.redis_append_trade_feature({"ticket": str(ticket)})

    records = redis_ipc.redis_read_trade_features()
    assert len(records) == 500
    assert records[0]["ticket"] == "500"
    assert records[-1]["ticket"] == "1"
    assert redis_ipc._TRADE_FEATURES_KEY in client.values

    assert redis_ipc.redis_update_trade_feature("250", "TP", 12.75)
    updated = next(record for record in redis_ipc.redis_read_trade_features()
                   if record["ticket"] == "250")
    assert updated["exit_type"] == "TP"
    assert updated["net_profit"] == 12.75
    assert not redis_ipc.redis_update_trade_feature("not-found", "manual", 0.0)


def test_trade_feature_snapshot_captures_directional_decision_fields():
    decision = SimpleNamespace(
        direction="BUY",
        regime="TREND",
        confidence=81.5,
        trend=SimpleNamespace(score=62.0, strength="STRONG", adx=27.0),
        pa=SimpleNamespace(
            bullish_engulf=True,
            bearish_engulf=False,
            bullish_inside_breakout=True,
            bearish_inside_breakout=False,
            valid_bull_breakout=False,
            valid_bear_breakout=True,
            bullish_pullback=False,
            bearish_pullback=True,
            pa_score=73.5,
        ),
        quality_filter=SimpleNamespace(session_quality="PRIME", adx=27.0),
        entry_filter=SimpleNamespace(trend=True, price_action=False),
    )
    opened_at = datetime(
        2026, 1, 2, 3, 0, tzinfo=timezone(timedelta(hours=3))
    )

    record = live_loop._build_trade_feature_record(
        ticket=123,
        decision=decision,
        h1_bias="BULLISH",
        spread_at_entry=0.25,
        stop_loss=2300.0,
        take_profit=2320.0,
        opened_at=opened_at,
    )

    assert record["ticket"] == "123"
    assert record["time_utc"] == "2026-01-02T00:00:00+00:00"
    assert record["direction"] == "BUY"
    assert record["regime"] == "TREND"
    assert record["trend"] == {"score": 62.0, "strength": "STRONG"}
    assert record["pa_flags"] == {
        "engulf": True,
        "inside_bar_breakout": True,
        "valid_breakout": False,
        "pullback": False,
    }
    assert record["pa_score"] == 73.5
    assert record["confidence"] == 81.5
    assert record["adx"] == 27.0
    assert record["session"] == "PRIME"
    assert record["h1_bias"] == "BULLISH"
    assert record["entry_filter"] == {"trend": True, "price_action": False}
    assert record["spread_at_entry"] == 0.25
    assert record["sl"] == 2300.0
    assert record["tp"] == 2320.0


def test_closed_trade_updates_feature_record_with_net_profit(monkeypatch):
    updates = []
    monkeypatch.setattr(
        redis_ipc,
        "redis_update_trade_feature",
        lambda ticket, exit_type, net_profit: (
            updates.append((ticket, exit_type, net_profit)) or True
        ),
    )
    robot = live_loop.GoldScalperLive()
    entry = {
        "position_id": "ticket-1",
        "direction": "BUY",
        "entry": 2300.0,
        "sl": 2290.0,
        "tp": 2320.0,
        "bar_time": "2026-01-02T00:00:00+00:00",
    }
    robot.trade_history = [entry]

    changed = robot._apply_close_history(
        entry,
        {
            "closePrice": 2320.0,
            "closeTime": "2026-01-02T01:00:00Z",
            "closeComment": "take profit",
            "profit": 10.0,
            "commission": -0.3,
            "swap": 0.1,
            "fee": -0.05,
        },
    )

    assert changed
    assert entry["close_reason"] == "TP"
    assert entry["profit"] == pytest.approx(9.75)
    assert updates == [("ticket-1", "TP", pytest.approx(9.75))]


@pytest.mark.parametrize(
    ("close_reason", "expected"),
    [
        ("SL", "SL"),
        ("TP", "TP"),
        ("BE", "BE"),
        ("Trailing", "trailing"),
        ("Manual", "manual"),
    ],
)
def test_feature_exit_type_uses_requested_labels(close_reason, expected):
    assert live_loop._trade_feature_exit_type(close_reason) == expected


def _request(token=""):
    return SimpleNamespace(headers={"X-Robot-Command-Token": token})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured_token", "supplied_token", "expected_status"),
    [
        (None, "anything", 503),
        ("expected-secret", "", 401),
        ("expected-secret", "wrong-secret", 403),
    ],
)
async def test_trade_features_endpoint_rejects_unauthorized_requests(
    monkeypatch, configured_token, supplied_token, expected_status
):
    if configured_token is None:
        monkeypatch.delenv("ROBOT_COMMAND_TOKEN", raising=False)
    else:
        monkeypatch.setenv("ROBOT_COMMAND_TOKEN", configured_token)
    reads = []
    monkeypatch.setattr(
        redis_ipc,
        "redis_read_trade_features",
        lambda: reads.append(True) or [],
    )

    response = await server._trade_features(_request(supplied_token))

    assert response.status == expected_status
    assert response.content_type == "application/json"
    assert not reads


@pytest.mark.asyncio
async def test_trade_features_endpoint_returns_json_after_authorization(monkeypatch):
    monkeypatch.setenv("ROBOT_COMMAND_TOKEN", "expected-secret")
    records = [{"ticket": "ticket-1", "exit_type": "TP", "net_profit": 9.75}]
    monkeypatch.setattr(
        redis_ipc,
        "redis_read_trade_features",
        lambda: records,
    )

    response = await server._trade_features(_request("expected-secret"))

    assert response.status == 200
    assert json.loads(response.text) == {"trade_features": records}


def test_trade_features_endpoint_is_registered():
    app = server._create_app()

    assert any(
        route.method == "GET"
        and route.resource.canonical == "/trade-features"
        for route in app.router.routes()
    )

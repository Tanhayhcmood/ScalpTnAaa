import asyncio
import json
from types import SimpleNamespace

import pytest

from live_trading import redis_ipc, server
from live_trading.mt5 import connector


@pytest.fixture
def clean_connection_health(monkeypatch):
    original = dict(connector._connection_health)
    connector._connection_events.clear()
    connector._connection_health.update({
        "last_success_utc": None,
        "consecutive_failures": 0,
        "total_reconnects": 0,
        "last_error_class": None,
        "last_error_text": None,
    })
    monkeypatch.setattr(connector, "_connection_history_loaded", True)
    monkeypatch.setattr(connector, "_persist_connection_history", lambda: None)
    yield
    connector._connection_events.clear()
    connector._connection_health.update(original)


def _request(token=""):
    return SimpleNamespace(headers={"X-Robot-Command-Token": token})


def test_connection_health_ring_buffer_keeps_only_last_50(clean_connection_health):
    for index in range(60):
        connector._record_connection_event(f"watchdog_event_{index}")

    health = connector.get_connection_health()

    assert len(health["events"]) == 50
    assert health["events"][0]["event"] == "watchdog_event_10"
    assert health["events"][-1]["event"] == "watchdog_event_59"


def test_connection_counters_and_error_text_are_redacted(clean_connection_health, monkeypatch):
    monkeypatch.setattr(connector, "MT5_PASSWORD", "password-value")
    monkeypatch.setattr(connector, "MT5_USER", "12345678")
    monkeypatch.setattr(connector, "_conn_id", "session-identifier")

    connector._record_connection_event("connect_success", source="ConnectEx")
    connector._record_connection_event(
        "connect_failure",
        error=RuntimeError(
            "password=password-value id=session-identifier account=12345678"
        ),
        failure=True,
    )
    connector._record_connection_event("connect_success", source="ConnectEx")
    health = connector.get_connection_health()
    serialized = json.dumps(health)

    assert health["last_success_utc"]
    assert health["consecutive_failures"] == 0
    assert health["total_reconnects"] == 1
    assert health["last_error_class"] == "RuntimeError"
    assert len(health["last_error_text"]) <= 200
    assert "password-value" not in serialized
    assert "session-identifier" not in serialized
    assert "12345678" not in serialized


@pytest.mark.asyncio
async def test_connectex_failure_records_event(clean_connection_health, monkeypatch):
    class FakeResponse:
        status = 503

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return "broker unavailable"

    class FakeSession:
        def get(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(connector, "MTAPI_URL", "https://mtapi.example")
    monkeypatch.setattr(connector, "MT5_HOST", "Broker")
    monkeypatch.setattr(connector, "MT5_USER", "12345678")
    monkeypatch.setattr(connector, "MT5_PASSWORD", "test-password")
    monkeypatch.setattr(connector, "_connected", False)
    monkeypatch.setattr(connector, "_conn_id", "")
    monkeypatch.setattr(connector, "_get_session", lambda: FakeSession())

    assert await connector._connect_unlocked() is False

    health = connector.get_connection_health()
    assert health["consecutive_failures"] == 1
    assert health["events"][-1]["event"] == "connect_failure"
    assert health["events"][-1]["error_text"].startswith(
        "ConnectEx returned HTTP 503"
    )
    assert health["events"][-1]["classification"] == "BRIDGE_DOWN"


@pytest.mark.asyncio
async def test_connectex_success_records_success_and_never_stores_session_id(
    clean_connection_health, monkeypatch
):
    session_id = "test-session-id-123456"

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return session_id

    class FakeSession:
        def get(self, *_args, **_kwargs):
            return FakeResponse()

    async def broker_ready(*_args, **_kwargs):
        return True

    monkeypatch.setattr(connector, "MTAPI_URL", "https://mtapi.example")
    monkeypatch.setattr(connector, "MT5_HOST", "Broker")
    monkeypatch.setattr(connector, "MT5_USER", "12345678")
    monkeypatch.setattr(connector, "MT5_PASSWORD", "test-password")
    monkeypatch.setattr(connector, "_connected", False)
    monkeypatch.setattr(connector, "_conn_id", "")
    monkeypatch.setattr(connector, "_get_session", lambda: FakeSession())
    monkeypatch.setattr(connector, "_wait_for_broker_ready", broker_ready)

    assert await connector._connect_unlocked() is True

    health = connector.get_connection_health()
    assert health["last_success_utc"]
    assert health["consecutive_failures"] == 0
    assert health["events"][-1]["event"] == "connect_success"
    assert session_id not in json.dumps(health)
    assert "12345678" not in json.dumps(health)


@pytest.mark.asyncio
async def test_keepalive_records_connection_status_not_alive(
    clean_connection_health, monkeypatch
):
    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def json(self, **_kwargs):
            return {"isConnected": False}

    class FakeSession:
        def get(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(connector, "_base_url", "https://mtapi.example")
    monkeypatch.setattr(connector, "_conn_id", "session-id-that-must-not-leak")
    monkeypatch.setattr(connector, "_get_session", lambda: FakeSession())

    assert await connector.keepalive_mtapi() is False

    health = connector.get_connection_health()
    assert health["consecutive_failures"] == 1
    assert health["events"][-1]["event"] == "connection_status_not_alive"
    assert "session-id-that-must-not-leak" not in json.dumps(health)


def test_invalidate_connection_records_event(clean_connection_health, monkeypatch):
    monkeypatch.setattr(connector, "_connected", True)
    monkeypatch.setattr(connector, "_conn_id", "session-id-that-must-not-leak")

    connector._invalidate_connection()

    health = connector.get_connection_health()
    assert connector._connected is False
    assert connector._conn_id == ""
    assert health["events"][-1]["event"] == "connection_invalidated"
    assert health["consecutive_failures"] == 1
    assert "session-id-that-must-not-leak" not in json.dumps(health)


@pytest.mark.asyncio
async def test_watchdog_reconnect_actions_are_recorded(clean_connection_health, monkeypatch):
    sleep_calls = 0

    async def stop_after_one_cycle(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise asyncio.CancelledError

    async def reconnect():
        return True

    monkeypatch.setattr(connector.asyncio, "sleep", stop_after_one_cycle)
    monkeypatch.setattr(connector, "ensure_connected", reconnect)
    monkeypatch.setattr(connector, "_connected", False)
    monkeypatch.setattr(connector, "_conn_id", "")

    with pytest.raises(asyncio.CancelledError):
        await connector.start_connection_watchdog(interval_seconds=1)

    events = [item["event"] for item in connector.get_connection_health()["events"]]
    assert "watchdog_started" in events
    assert "watchdog_reconnect_attempt" in events
    assert "watchdog_reconnect_success" in events
    assert "watchdog_stopped" in events


def test_connection_events_redis_helpers_use_persistent_key(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.values = {}

        def set(self, key, value):
            self.values[key] = value

        def get(self, key):
            return self.values.get(key)

    fake = FakeRedis()
    monkeypatch.setattr(redis_ipc, "_get_client", lambda: fake)
    data = {"counters": {"total_reconnects": 3}, "events": [{"event": "connect_success"}]}

    assert redis_ipc.redis_write_connection_events(data) is True
    assert "goldscalper:connection_events" in fake.values
    assert redis_ipc.redis_read_connection_events() == data


def test_connection_health_restores_counters_and_events_from_redis(
    clean_connection_health, monkeypatch
):
    saved = {
        "counters": {
            "last_success_utc": "2026-11-08T12:00:00+00:00",
            "consecutive_failures": 2,
            "total_reconnects": 7,
            "last_error_class": "TimeoutError",
            "last_error_text": "broker request timed out",
        },
        "events": [
            {
                "timestamp_utc": "2026-11-08T12:01:00+00:00",
                "event": "connection_status_not_alive",
                "source": "watchdog",
            }
        ],
    }
    monkeypatch.setattr(connector, "_connection_history_loaded", False)
    monkeypatch.setattr(redis_ipc, "redis_available", lambda: True)
    monkeypatch.setattr(redis_ipc, "redis_read_connection_events", lambda: saved)

    health = connector.get_connection_health()

    assert health["last_success_utc"] == "2026-11-08T12:00:00+00:00"
    assert health["consecutive_failures"] == 2
    assert health["total_reconnects"] == 7
    assert health["last_error_class"] == "TimeoutError"
    assert health["events"] == saved["events"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured_token", "supplied_token", "expected_status"),
    [
        (None, "anything", 503),
        ("expected-secret", "", 401),
        ("expected-secret", "wrong-secret", 403),
    ],
)
async def test_connection_health_endpoint_rejects_unauthorized_requests(
    monkeypatch, configured_token, supplied_token, expected_status
):
    if configured_token is None:
        monkeypatch.delenv("ROBOT_COMMAND_TOKEN", raising=False)
    else:
        monkeypatch.setenv("ROBOT_COMMAND_TOKEN", configured_token)

    response = await server._connection_health(_request(supplied_token))

    assert response.status == expected_status
    assert response.content_type == "application/json"


@pytest.mark.asyncio
async def test_connection_health_endpoint_returns_counters_and_events_after_auth(
    clean_connection_health, monkeypatch
):
    monkeypatch.setenv("ROBOT_COMMAND_TOKEN", "expected-secret")
    connector._record_connection_event("watchdog_started", source="watchdog")

    response = await server._connection_health(_request("expected-secret"))

    assert response.status == 200
    payload = json.loads(response.text)
    assert payload["events"][-1]["event"] == "watchdog_started"
    assert "total_reconnects" in payload


def test_connection_health_route_is_registered():
    app = server._create_app()

    assert any(
        route.method == "GET"
        and route.resource.canonical == "/connection-health"
        for route in app.router.routes()
    )

import pytest

from live_trading.mt5 import connector


@pytest.mark.parametrize(
    ("error", "message", "status", "expected"),
    [
        (None, "CloudTrial failed: Trial expired", 200, "TRIAL_EXPIRED"),
        (None, "SERVICE_NOT_AVAILABLE", 503, "BROKER_UNAVAILABLE"),
        (TimeoutError(), "", None, "BROKER_UNAVAILABLE"),
        (None, "TCP connect timed out", None, "BROKER_UNAVAILABLE"),
        (None, "bridge error", 502, "BRIDGE_DOWN"),
        (None, "bridge error", 503, "BRIDGE_DOWN"),
        (None, "bridge error", 504, "BRIDGE_DOWN"),
        (ConnectionRefusedError("connection refused"), "", None, "BRIDGE_DOWN"),
        (None, "Invalid account or password", 400, "AUTH_ERROR"),
        (None, "authentication failed", 401, "AUTH_ERROR"),
        (None, "unrecognized broker response", 400, "UNKNOWN"),
    ],
)
def test_connection_error_classification(error, message, status, expected):
    assert connector._classify_connection_error(
        error=error,
        message=message,
        status=status,
    ) == expected


def test_failure_event_buffer_includes_classification(monkeypatch):
    monkeypatch.setattr(connector, "_connection_history_loaded", True)
    monkeypatch.setattr(connector, "_persist_connection_history", lambda: None)
    connector._connection_events.clear()

    connector._record_connection_event(
        "data_call_failure",
        source="ConnectEx",
        error=RuntimeError("MTAPI rejected the connection"),
        message="CloudTrial failed: Trial expired",
        failure=True,
    )

    assert connector.get_connection_health()["events"][-1]["classification"] == (
        "TRIAL_EXPIRED"
    )


def test_reconnect_backoff_schedule_caps_at_120_seconds(monkeypatch):
    monkeypatch.setattr(connector, "MT5_CONNECT_RETRY_JITTER", 0.0)

    delays = [
        connector._retry_backoff_delay("BROKER_UNAVAILABLE", attempt)
        for attempt in range(1, 8)
    ]

    assert delays == [3.0, 6.0, 12.0, 30.0, 60.0, 120.0, 120.0]


def test_transient_backoff_jitter_never_shortens_the_nominal_delay(monkeypatch):
    monkeypatch.setattr(connector, "MT5_CONNECT_RETRY_JITTER", 0.2)
    draws = []

    def choose_upper_bound(low, high):
        draws.append((low, high))
        return high

    monkeypatch.setattr(connector.random, "uniform", choose_upper_bound)

    assert connector._retry_backoff_delay("BRIDGE_DOWN", 1) == 3.6
    assert draws == [(3.0, 3.6)]


def test_service_not_available_starts_at_thirty_seconds(monkeypatch):
    monkeypatch.setattr(connector, "MT5_CONNECT_RETRY_JITTER", 0.0)

    assert connector._retry_backoff_delay(
        "BROKER_UNAVAILABLE", 1, message="SERVICE_NOT_AVAILABLE"
    ) == 30.0


@pytest.mark.parametrize("classification", ["AUTH_ERROR", "TRIAL_EXPIRED"])
def test_auth_and_trial_errors_use_bounded_backoff(monkeypatch, classification):
    monkeypatch.setattr(connector, "MT5_CONNECT_RETRY_JITTER", 0.0)

    assert connector._retry_backoff_delay(classification, 1) == 3.0


@pytest.mark.asyncio
async def test_connect_with_retry_is_unbounded_and_uses_backoff(monkeypatch):
    attempts = 0
    delays = []
    monkeypatch.setattr(connector, "_connect_retry_attempt", 0)
    monkeypatch.setattr(connector, "_connect_retry_service_unavailable", False)
    monkeypatch.setattr(connector, "MT5_CONNECT_RETRY_JITTER", 0.0)

    async def connect_then_succeed():
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            connector._record_connection_event(
                "connect_failure",
                source="test",
                error=ConnectionError("SERVICE_NOT_AVAILABLE"),
                failure=True,
            )
            return False
        return True

    async def capture_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(connector, "connect", connect_then_succeed)
    monkeypatch.setattr(connector.asyncio, "sleep", capture_sleep)

    assert await connector.connect_with_retry(max_attempts=1, retry_delay=0) is True
    assert attempts == 4
    assert delays == [30.0, 60.0, 120.0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("previous_alive", "expected_id", "expected_connected"),
    [
        (True, "previous-live-session-id", True),
        (False, "", False),
    ],
)
async def test_unready_new_session_keeps_previous_only_if_alive(
    monkeypatch, previous_alive, expected_id, expected_connected
):
    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return "new-session-identifier"

    class FakeSession:
        def get(self, *_args, **_kwargs):
            return FakeResponse()

    async def not_ready(*_args, **_kwargs):
        return False

    async def previous_status(*_args, **_kwargs):
        return previous_alive

    monkeypatch.setattr(connector, "MTAPI_URL", "https://mtapi.example")
    monkeypatch.setattr(connector, "MT5_HOST", "Broker")
    monkeypatch.setattr(connector, "MT5_USER", "12345678")
    monkeypatch.setattr(connector, "MT5_PASSWORD", "test-password")
    monkeypatch.setattr(connector, "_base_url", "https://mtapi.example")
    monkeypatch.setattr(connector, "_conn_id", "previous-live-session-id")
    monkeypatch.setattr(connector, "_connected", False)
    monkeypatch.setattr(connector, "_get_session", lambda: FakeSession())
    monkeypatch.setattr(connector, "_wait_for_broker_ready", not_ready)
    monkeypatch.setattr(
        connector, "_previous_connection_is_alive", previous_status
    )
    monkeypatch.setattr(connector, "_connection_history_loaded", True)
    monkeypatch.setattr(connector, "_persist_connection_history", lambda: None)

    assert await connector._connect_unlocked() is expected_connected
    assert connector._conn_id == expected_id
    assert connector._connected is expected_connected

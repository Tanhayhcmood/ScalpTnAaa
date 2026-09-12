"""Regression tests for the official MTAPI Connect query parameters."""


def test_connect_params_use_official_connect_fields():
    from live_trading.mt5.connector import _connect_params

    params = _connect_params("123", "secret", "broker.example", 443)

    assert params["user"] == "123"
    assert params["password"] == "secret"
    assert params["host"] == "broker.example"
    assert params["port"] == 443
    assert params["errorReplyStatusCode"] == 400


def test_connection_status_accepts_boolean_and_string_true_values():
    from live_trading.mt5.connector import _connection_status_is_alive

    assert _connection_status_is_alive({"isConnected": True}) is True
    assert _connection_status_is_alive({"isConnected": "true"}) is True
    assert _connection_status_is_alive({"connected": "CONNECTED"}) is True


def test_connection_status_rejects_false_or_malformed_payloads():
    from live_trading.mt5.connector import _connection_status_is_alive

    assert _connection_status_is_alive({"isConnected": False}) is False
    assert _connection_status_is_alive({"isConnected": "false"}) is False
    assert _connection_status_is_alive({"message": "starting"}) is False
    assert _connection_status_is_alive([]) is False
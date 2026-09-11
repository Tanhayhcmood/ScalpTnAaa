"""Regression tests for mt5rest ConnectEx query parameters."""


def test_connect_ex_boolean_flags_are_query_string_values():
    from live_trading.mt5.connector import _connect_params

    params = _connect_params("123", "secret", "AMarkets-Demo")

    assert params["downloadOrderHistory"] == "true"
    assert params["reconnectOnSymbolUpdate"] == "true"
    assert all(not isinstance(params[name], bool) for name in (
        "downloadOrderHistory",
        "reconnectOnSymbolUpdate",
    ))


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
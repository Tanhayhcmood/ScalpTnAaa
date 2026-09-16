"""Regression tests for the MTAPI connector contract."""

import asyncio
from unittest.mock import patch


def test_account_field_reads_dict_and_sdk_style_objects():
    from live_trading.mt5.connector import _account_field

    class AccountInfo:
        broker = "AMarkets"

    assert _account_field({"broker": "MetaQuotes"}, "broker") == "MetaQuotes"
    assert _account_field(AccountInfo(), "broker") == "AMarkets"
    assert _account_field({}, "server", "MT5") == "MT5"


def test_connect_params_use_server_name_connect_ex_contract():
    from live_trading.mt5.connector import _connect_params

    params = _connect_params("7924815", "secret", "AMarkets-Demo", 443)

    assert params == {
        "user": "7924815",
        "password": "secret",
        "server": "AMarkets-Demo",
        "connectTimeoutClusterMemberSeconds": 30,
        "connectToNearestByPing": "true",
        "connectTimeoutSeconds": 60,
        "errorReplyStatusCode": 400,
    }


def test_connect_fails_closed_without_metaapi_credentials():
    from live_trading.mt5 import connector

    with (
        patch.object(connector, "METAAPI_TOKEN", ""),
        patch.object(connector, "METAAPI_ACCOUNT_ID", ""),
    ):
        assert asyncio.run(connector.connect("", "", 1)) is False
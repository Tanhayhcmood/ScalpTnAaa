import os
from types import SimpleNamespace

from live_trading import server


def _request(token: str = ""):
    return SimpleNamespace(headers={"X-Robot-Command-Token": token})


def test_command_auth_requires_configuration(monkeypatch):
    monkeypatch.delenv("ROBOT_COMMAND_TOKEN", raising=False)

    authorized, status, message = server._command_authorized(_request("anything"))

    assert (authorized, status) == (False, 503)
    assert message == "command interface is not configured"


def test_command_auth_rejects_missing_or_invalid_token(monkeypatch):
    monkeypatch.setenv("ROBOT_COMMAND_TOKEN", "expected-secret")

    missing = server._command_authorized(_request())
    invalid = server._command_authorized(_request("wrong-secret"))

    assert missing[:2] == (False, 401)
    assert invalid[:2] == (False, 403)


def test_command_auth_accepts_shared_token(monkeypatch):
    monkeypatch.setenv("ROBOT_COMMAND_TOKEN", "expected-secret")

    assert server._command_authorized(_request("expected-secret")) == (True, 200, "")


def test_status_route_uses_the_same_authorization_guard():
    assert server._status.__annotations__["req"] is not None
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from telegram_panel.api.handlers.dashboard import DashboardHandler
from telegram_panel.api.keyboards.inline import Keyboards


def test_dashboard_has_reset_cooldown_button_between_run_and_stop_controls():
    rows = Keyboards.dashboard().inline_keyboard

    assert rows[1][0].text == "♻️ Reset Cooldown"
    assert rows[1][0].callback_data == "robot:reset_guardian_confirm"


@pytest.mark.asyncio
async def test_reset_cooldown_requires_confirmation_then_uses_guardian_reset():
    user = SimpleNamespace(telegram_id=123)
    auth = SimpleNamespace(
        check_permission=AsyncMock(return_value=(True, user)),
        record_action=AsyncMock(),
    )
    robot = SimpleNamespace(
        start=AsyncMock(return_value=True),
        pause=AsyncMock(return_value=True),
        resume=AsyncMock(return_value=True),
        reset_guardian=AsyncMock(return_value=True),
        emergency_stop=AsyncMock(return_value=True),
        restart_engine=AsyncMock(return_value=True),
        restart_mt5=AsyncMock(return_value=True),
        restart_telegram=AsyncMock(return_value=True),
        safe_shutdown=AsyncMock(return_value=True),
    )
    handler = DashboardHandler(robot, None, None, None, auth, None)
    handler.edit_or_reply = AsyncMock()
    handler.answer_callback = AsyncMock()
    handler.show_dashboard = AsyncMock()
    update = SimpleNamespace(callback_query=object())
    context = object()

    await handler.handle_robot_control(update, context, "reset_guardian_confirm")

    assert not robot.reset_guardian.await_count
    prompt = handler.edit_or_reply.await_args.args[2]
    confirmation_keyboard = handler.edit_or_reply.await_args.args[3]
    assert "does not reset the daily-loss baseline" in prompt
    assert "post-SL entry cooldown" in prompt
    assert confirmation_keyboard.inline_keyboard[0][0].callback_data == (
        "robot:reset_guardian_confirmed"
    )

    await handler.handle_robot_control(update, context, "reset_guardian_confirmed")

    robot.reset_guardian.assert_awaited_once_with()
    auth.record_action.assert_awaited_once()
    audit_call = auth.record_action.await_args
    assert audit_call.args[1] == "ROBOT_RESET_GUARDIAN_CONFIRMED"
    assert audit_call.kwargs["success"] is True
    handler.show_dashboard.assert_awaited_once_with(update, context)


@pytest.mark.asyncio
async def test_reset_cooldown_requires_robot_control_permission():
    auth = SimpleNamespace(
        check_permission=AsyncMock(return_value=(False, None)),
        record_action=AsyncMock(),
    )
    robot = SimpleNamespace(reset_guardian=AsyncMock(return_value=True))
    handler = DashboardHandler(robot, None, None, None, auth, None)
    update = SimpleNamespace(callback_query=object())

    await handler.handle_robot_control(update, None, "reset_guardian_confirmed")

    robot.reset_guardian.assert_not_awaited()
    auth.record_action.assert_not_awaited()

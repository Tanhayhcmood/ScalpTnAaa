from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from telegram_panel.api.handlers.dashboard import (
    DashboardHandler,
    _format_guardian_reset_receipt,
)
from telegram_panel.api.keyboards.inline import Keyboards


def test_dashboard_has_reset_cooldown_button_between_run_and_stop_controls():
    rows = Keyboards.dashboard().inline_keyboard

    assert rows[1][0].text == "♻️ Reset Cooldown"
    assert rows[1][0].callback_data == "robot:reset_guardian_confirm"


def test_reset_confirmation_explains_new_baselines_and_unchanged_limits():
    user = SimpleNamespace(telegram_id=123)
    auth = SimpleNamespace(
        check_permission=AsyncMock(return_value=(True, user)),
        record_action=AsyncMock(),
    )
    robot = SimpleNamespace(reset_guardian=AsyncMock(return_value=True))
    handler = DashboardHandler(robot, None, None, None, auth, None)
    handler.edit_or_reply = AsyncMock()
    handler.answer_callback = AsyncMock()
    handler.show_dashboard = AsyncMock()
    update = SimpleNamespace(callback_query=object())

    async def run():
        await handler.handle_robot_control(update, object(), "reset_guardian_confirm")
        assert not robot.reset_guardian.await_count
        prompt = handler.edit_or_reply.await_args.args[2]
        keyboard = handler.edit_or_reply.await_args.args[3]
        assert "count from the live account snapshot" in prompt
        assert "configured limits stay unchanged" in prompt
        assert "post-SL entry cooldown still applies" in prompt
        assert keyboard.inline_keyboard[0][0].callback_data == "robot:reset_guardian_confirmed"

    import asyncio
    asyncio.run(run())


@pytest.mark.asyncio
async def test_reset_cooldown_waits_for_robot_receipt_and_shows_baseline_values():
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

    async def fresh_state():
        request_id = robot.reset_guardian.await_args.kwargs["request_id"]
        return {
            "guardian_reset_receipt": {
                "request_id": request_id,
                "status": "applied",
                "currency": "USD",
                "daily_loss_limit_pct": 3.0,
                "max_drawdown_pct": 8.0,
                "baseline": {
                    "reset_at": "2026-10-17T20:00:58+00:00",
                    "trading_day": "2026-10-17",
                    "realized_day_pnl": -480.59,
                    "balance": 9_149.41,
                    "equity": 9_160.00,
                    "equity_peak": 9_160.00,
                },
            }
        }

    robot.get_fresh_state = AsyncMock(side_effect=fresh_state)
    handler = DashboardHandler(robot, None, None, None, auth, None)
    handler.edit_or_reply = AsyncMock()
    handler.answer_callback = AsyncMock()
    handler.show_dashboard = AsyncMock()
    update = SimpleNamespace(
        callback_query=object(), effective_chat=SimpleNamespace(id=456)
    )
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

    await handler.handle_robot_control(update, context, "reset_guardian_confirmed")

    robot.reset_guardian.assert_awaited_once()
    request_id = robot.reset_guardian.await_args.kwargs["request_id"]
    assert request_id
    robot.get_fresh_state.assert_awaited()
    message = context.bot.send_message.await_args.kwargs["text"]
    assert "limits now count from the reset point" in message
    assert "-480.59 USD" in message
    assert "9,149.41 USD" in message
    assert "9,160.00 / 9,160.00 USD" in message
    assert "daily loss 3.00%" in message
    assert "max drawdown 8.00%" in message
    assert auth.record_action.await_args.kwargs["success"] is True
    handler.show_dashboard.assert_awaited_once_with(update, context)


def test_reset_receipt_reports_noop_when_guardian_was_not_halted():
    message = _format_guardian_reset_receipt({
        "status": "ignored",
        "reason": "Guardian was not halted; no state was changed.",
    })
    assert "No reset applied" in message
    assert "left unchanged" in message

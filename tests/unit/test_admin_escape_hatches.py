"""The ways out: /cancel, /admin, the command menu, and stale buttons.

The trap these close: tap «🔎 جستجوی سفارش», then tap «📊 آمار» instead of
typing. The FSM stayed on ``waiting_order_id`` and every later plain message —
in any chat with the bot — came back «شناسه باید عدد باشد.»
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from bot.commands import ADMIN_COMMANDS, CUSTOMER_COMMANDS, setup_bot_commands
from bot.handlers.common import cmd_admin, cmd_cancel, is_admin, unknown_callback
from bot.states import AdminStates
from bot.texts import texts

ADMIN_ID = 555
CUSTOMER_ID = 777


def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=ADMIN_ID, user_id=ADMIN_ID),
    )


def _settings(**over: object) -> SimpleNamespace:
    # web_panel_link: the admin home asks for it (empty while the Mini App panel is off).
    return SimpleNamespace(admin_telegram_ids=[ADMIN_ID], web_panel_link="", **over)


def _message(user_id: int = ADMIN_ID) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        answer=AsyncMock(),
    )


# --------------------------------------------------------------------------- #
# /cancel
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cancel_clears_a_stuck_admin_state(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state()
    await state.set_state(AdminStates.waiting_order_id)

    message = _message()
    await cmd_cancel(
        message,  # type: ignore[arg-type]
        state,
        settings=_settings(),  # type: ignore[arg-type]
        db_user=SimpleNamespace(id=1),  # type: ignore[arg-type]
    )

    assert await state.get_state() is None
    assert message.answer.await_args.args[0] == texts.cancelled_done


@pytest.mark.asyncio
async def test_cancel_is_harmless_with_nothing_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    message = _message()
    await cmd_cancel(
        message,  # type: ignore[arg-type]
        state,
        settings=_settings(),  # type: ignore[arg-type]
        db_user=None,
    )
    assert message.answer.await_args.args[0] == texts.cancelled_nothing


# --------------------------------------------------------------------------- #
# /admin
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_admin_command_opens_the_panel_and_drops_any_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bot.admin_home as mod

    monkeypatch.setattr(
        mod,
        "get_home_status",
        AsyncMock(
            return_value=SimpleNamespace(
                pending=3,
                stale=0,
                today=SimpleNamespace(orders=0, revenue_toman=0),
                groups_without_panel=[],
                report_attached=True,
                report_unready=0,
                panel_down=[],
            )
        ),
    )
    state = _state()
    await state.set_state(AdminStates.waiting_ban_telegram_id)

    message = _message()
    await cmd_admin(
        message,  # type: ignore[arg-type]
        state,
        session=AsyncMock(),
        settings=_settings(),  # type: ignore[arg-type]
        db_user=SimpleNamespace(id=1, is_admin=True),  # type: ignore[arg-type]
    )

    assert await state.get_state() is None
    kb = message.answer.await_args.kwargs["reply_markup"]
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any("(۳)" in label for label in labels), labels


@pytest.mark.asyncio
async def test_admin_command_refuses_a_customer() -> None:
    state = _state()
    message = _message(user_id=CUSTOMER_ID)
    await cmd_admin(
        message,  # type: ignore[arg-type]
        state,
        session=AsyncMock(),
        settings=_settings(),  # type: ignore[arg-type]
        db_user=SimpleNamespace(id=2, is_admin=False),  # type: ignore[arg-type]
    )
    assert message.answer.await_args.args[0] == texts.admin_only


def test_is_admin_requires_configured_membership_despite_stale_db_flag() -> None:
    assert is_admin(ADMIN_ID, _settings(), None)  # type: ignore[arg-type]
    assert not is_admin(CUSTOMER_ID, _settings(), SimpleNamespace(is_admin=True))  # type: ignore[arg-type]
    assert not is_admin(CUSTOMER_ID, _settings(), SimpleNamespace(is_admin=False))  # type: ignore[arg-type]
    assert not is_admin(None, _settings(), None)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Stale buttons and the command menu
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unknown_callback_is_answered_not_left_spinning() -> None:
    callback = SimpleNamespace(data="adm:removed:7", answer=AsyncMock())
    await unknown_callback(callback)  # type: ignore[arg-type]
    callback.answer.assert_awaited_once_with(texts.stale_button, show_alert=True)


@pytest.mark.asyncio
async def test_command_menu_is_published_per_scope() -> None:
    bot = SimpleNamespace(set_my_commands=AsyncMock())
    await setup_bot_commands(bot, [ADMIN_ID])  # type: ignore[arg-type]

    calls = bot.set_my_commands.await_args_list
    assert calls[0].args[0] == CUSTOMER_COMMANDS
    assert calls[1].args[0] == ADMIN_COMMANDS
    assert calls[1].kwargs["scope"].chat_id == ADMIN_ID
    assert {c.command for c in ADMIN_COMMANDS} == {"start", "admin", "cancel"}


@pytest.mark.asyncio
async def test_one_unreachable_admin_does_not_stop_startup() -> None:
    """An admin who never opened the bot has no chat — that must not be fatal."""
    bot = SimpleNamespace(
        set_my_commands=AsyncMock(side_effect=[None, RuntimeError("chat not found"), None])
    )
    await setup_bot_commands(bot, [ADMIN_ID, 999])  # type: ignore[arg-type]
    assert bot.set_my_commands.await_count == 3


# --------------------------------------------------------------------------- #
# Unhandled input, found in live testing
# --------------------------------------------------------------------------- #


def test_unknown_text_has_a_fallback_handler() -> None:
    """A customer migrating from a market bot still has its reply keyboard.

    Live log: «خرید سرویس 🛍» arrived and the bot answered nothing at all —
    `is not handled` — which is the worst possible first impression.
    """
    from bot.handlers import common

    handlers = [h.callback.__name__ for h in common.fallback_router.message.handlers]
    assert "unknown_message" in handlers


def test_the_fallback_router_is_registered_last(router_names: list[str]) -> None:
    """Registered any earlier it would swallow every real handler's messages."""
    assert router_names[0] == "common", "/cancel must beat every state-scoped handler"
    assert router_names[-1] == "fallback"


def test_the_fallback_replaces_a_stale_reply_keyboard_with_ours() -> None:
    """Sending our keyboard displaces the old bot's, without leaving none.

    This used to assert ``ReplyKeyboardRemove``. That was correct while the bot
    had no keyboard of its own; now it does, and removing it on an unrecognized
    message would strip the customer's only permanent navigation.
    """
    import inspect

    from bot.handlers.common import unknown_message

    source = inspect.getsource(unknown_message)
    assert "main_reply_keyboard(" in source
    # The docstring names the old behaviour to explain it; only a *call* is a bug.
    assert "ReplyKeyboardRemove(" not in source


def test_a_refresh_that_changes_nothing_does_not_duplicate_the_card() -> None:
    """Telegram rejects an identical edit; resending filled the chat with copies."""
    import inspect

    from bot.handlers.account import _present_service_detail_message

    source = inspect.getsource(_present_service_detail_message)
    assert "not modified" in source
    assert "TelegramBadRequest" in source

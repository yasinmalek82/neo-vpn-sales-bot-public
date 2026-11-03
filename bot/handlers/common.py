"""Global escape hatches: /cancel, /admin, and the unknown-callback fallback.

Two routers live here because they need opposite registration order.
``router`` goes **first** so ``/cancel`` wins over every state-scoped message
handler — that is what makes an admin stuck in ``waiting_order_id`` (where every
plain message was answered «شناسه باید عدد باشد.») able to get out.
``fallback_router`` goes **last** so it only sees callbacks nothing else claimed.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.admin_home import admin_home_screen
from bot.config import Settings
from bot.keyboards.reply_menu import main_reply_keyboard
from bot.texts import texts
from database.models import User
from services.access import is_admin as access_is_admin

logger = logging.getLogger(__name__)

router = Router(name="common")
fallback_router = Router(name="fallback")


async def edit_or_send(message: Message, body: str, markup: Any) -> None:
    """Replace the screen ``message`` shows, or send a new one.

    Editing keeps screens from piling up, but only the bot's own message can
    be edited. After a typed answer ``message`` is the person's own text:
    Telegram refuses that edit every time («message can't be edited»), so a
    typed answer gets a new message directly, without the doomed call.
    """
    # ``is False``, read defensively: only a message Telegram marks as a
    # person's is skipped; anything else keeps the edit-then-send path.
    author = getattr(message, "from_user", None)
    if getattr(author, "is_bot", None) is False:
        await message.answer(body, reply_markup=markup)
        return
    try:
        await message.edit_text(body, reply_markup=markup)
    except TelegramBadRequest as exc:
        # A second tap on the same button asks for the screen already shown.
        # That is not a message that cannot be edited: sending it again only
        # piles up copies of the screen.
        if "message is not modified" in str(exc).lower():
            return
        await message.answer(body, reply_markup=markup)
    except Exception:
        await message.answer(body, reply_markup=markup)


def is_admin(
    telegram_id: int | None,
    settings: Settings,
    db_user: User | None = None,
) -> bool:
    """Env admin list or the middleware-synced ``db_user`` flag."""
    return access_is_admin(telegram_id, settings, db_user)


@router.message(Command("cancel"))
async def cmd_cancel(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
) -> None:
    """Always clears the FSM, whatever screen or state the user was on."""
    had_state = await state.get_state() is not None
    await state.clear()
    body = texts.cancelled_done if had_state else texts.cancelled_nothing
    tg_id = message.from_user.id if message.from_user else None
    await message.answer(
        body,
        reply_markup=main_reply_keyboard(
            is_admin=is_admin(tg_id, settings, db_user),
            is_reseller=bool(getattr(db_user, "is_reseller", False)),
        ),
    )


@router.message(Command("admin"))
async def cmd_admin(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """Direct entry to the admin panel — no scrolling back for an inline button."""
    tg_id = message.from_user.id if message.from_user else None
    if not is_admin(tg_id, settings, db_user):
        await message.answer(texts.admin_only)
        return
    await state.clear()
    body, markup = await admin_home_screen(session, settings.web_panel_link)
    await message.answer(body, reply_markup=markup)


@fallback_router.callback_query(F.data)
async def unknown_callback(callback: CallbackQuery) -> None:
    """Answer callbacks no handler claimed.

    Without this the button spins until Telegram times out — which is what a
    stale keyboard from a previous deploy did to every admin.
    """
    logger.info("Unhandled callback data: %s", callback.data)
    await callback.answer(texts.stale_button, show_alert=True)


@fallback_router.message()
async def unknown_message(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
) -> None:
    """Answer text no handler claimed, instead of staying silent.

    Seen live: a customer migrating from a market bot still had its persistent
    reply keyboard, tapped «خرید سرویس 🛍», and the bot said nothing at all —
    the worst possible first impression.

    This used to send ``ReplyKeyboardRemove``, which was right while the bot had
    no keyboard of its own. It now has one, and removing it here would strip the
    customer's only permanent navigation on any unrecognized message. Sending
    ours replaces the old bot's keyboard just as effectively.
    """
    if await state.get_state() is not None:
        # A live prompt owns this message; its own handler answers it.
        return
    tg_id = message.from_user.id if message.from_user else None
    logger.info("Unhandled message from %s (len=%d)", tg_id, len(message.text or ""))
    await message.answer(
        texts.unknown_message,
        reply_markup=main_reply_keyboard(
            is_admin=is_admin(tg_id, settings, db_user),
            is_reseller=bool(getattr(db_user, "is_reseller", False)),
        ),
    )

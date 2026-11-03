"""Bottom-keyboard entry points.

A reply-keyboard button sends plain text, so these are ``Message`` handlers for
screens that until now could only be reached by an inline tap.

This router is registered **early**, right after ``common.router``, for the same
reason ``/cancel`` is: a navigation root must win over whatever state-scoped
handler is waiting for input. Tapping «🏦 کیف پول» while the bot is waiting for
a receipt should show the wallet, not «عکس رسید رو بفرست». Each handler clears
the FSM first, so the customer never lands half-in a flow they left.

Mid-flow only the exact label of a bottom button navigates; the keyword guess
that catches a previous bot's keyboard is made only when no flow is waiting
(``navigation_root``), or typed input that mentions «سرویس» would be eaten.
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.admin_home import admin_home_screen
from bot.config import Settings
from bot.handlers.account import show_services_list_for_message
from bot.handlers.common import is_admin
from bot.handlers.purchase import resume_open_order_if_any, start_purchase_flow
from bot.handlers.referral import render_referral, render_trial
from bot.handlers.wallet import render_wallet
from bot.keyboards.disruption import support_keyboard
from bot.keyboards.reply_menu import ReplyRoot, navigation_root
from bot.texts import texts
from database.models import User
from services.brand import with_support

logger = logging.getLogger(__name__)
router = Router(name="reply_menu")


def _root_is(expected: str):  # type: ignore[no-untyped-def]
    """aiogram filter: this message is a tap on ``expected``."""

    def _check(message: Message, raw_state: str | None = None) -> bool:
        return navigation_root(message.text or "", in_flow=raw_state is not None) == expected

    return _check


@router.message(F.text, _root_is(ReplyRoot.BUY))
async def reply_buy(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    await state.clear()
    if db_user is not None and await resume_open_order_if_any(
        message, state, session, settings, db_user
    ):
        return
    await start_purchase_flow(message, state, session, settings, db_user)


@router.message(F.text, _root_is(ReplyRoot.ACCOUNT))
async def reply_account(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    await state.clear()
    if db_user is None:
        await message.answer(texts.account_need_user)
        return
    await show_services_list_for_message(message, session, settings, db_user)


@router.message(F.text, _root_is(ReplyRoot.WALLET))
async def reply_wallet(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    await state.clear()
    await render_wallet(message, session, settings, db_user)


@router.message(F.text, _root_is(ReplyRoot.REFERRAL))
async def reply_referral(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    db_user: User | None,
) -> None:
    await state.clear()
    await render_referral(message, session, settings, bot, db_user)


@router.message(F.text, _root_is(ReplyRoot.TRIAL))
async def reply_trial(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    await state.clear()
    await render_trial(message, session, settings, db_user, state)


@router.message(F.text, _root_is(ReplyRoot.SUPPORT))
async def reply_support(message: Message, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    await message.answer(with_support(texts.support, settings), reply_markup=support_keyboard())


@router.message(F.text, _root_is(ReplyRoot.ADMIN))
async def reply_admin(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    tg_id = message.from_user.id if message.from_user else None
    if not is_admin(tg_id, settings, db_user):
        await message.answer(texts.admin_only)
        return
    await state.clear()
    body, markup = await admin_home_screen(session, settings.web_panel_link)
    await message.answer(body, reply_markup=markup)


__all__ = ["router"]

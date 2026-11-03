"""/start onboarding and referral payload parsing."""

from __future__ import annotations

import contextlib
from html import escape

from aiogram import Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.handlers.purchase import resume_open_order_if_any
from bot.keyboards.reply_menu import main_reply_keyboard
from bot.texts import texts
from database.repositories.user import get_or_create_user
from services.report_events import parse_admin_customer_start

router = Router(name="onboarding")


def _display_name(message: Message) -> str:
    user = message.from_user
    if user is None:
        return "دوست"
    # The welcome is HTML: a first name like «Ali <3» would make Telegram refuse it.
    return escape(user.first_name or user.username or "دوست")


_REFERRAL_NOTES: dict[str, str] = {
    "saved": texts.referral_saved,
    "self": texts.referral_ignored_self,
    "already": texts.referral_already_set,
    "established": texts.referral_too_late,
    # «invalid» stays silent on purpose: a mistyped or stale deep-link payload is
    # not the customer's problem, and telling them their code is wrong when they
    # never typed one only confuses them.
}


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
) -> None:
    if message.from_user is None:
        return

    payload = command.args
    # A report's «کارت مشتری» button uses this prefix. It is not an invite, and
    # feeding it to referral attribution would record a nonsense «invalid».
    customer_id = parse_admin_customer_start(payload)
    user, created, referral_note = await get_or_create_user(
        session,
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        language_code=message.from_user.language_code,
        admin_telegram_ids=settings.admin_telegram_ids,
        start_payload=None if customer_id is not None else payload,
    )

    name = _display_name(message)
    text = texts.welcome_new.format(name=name) if created else texts.welcome_back.format(name=name)

    # Answer the invite link whoever opened it. Notes used to be dropped for
    # anyone already in the database, so a link that was deliberately refused
    # looked exactly like a link that was broken.
    note = _REFERRAL_NOTES.get(referral_note or "")
    if note:
        text = f"{text}\n\n{note}"

    # One message: the bottom keyboard carries every main-menu entry, so a second
    # message with an inline copy of the same menu was pure duplication.
    await message.answer(
        text,
        reply_markup=main_reply_keyboard(
            is_admin=user.is_admin, is_reseller=bool(user.is_reseller)
        ),
    )

    # The card lives in the admin module. Importing it here at load time pulls
    # the whole admin router through purchase and back into this module.
    if customer_id is not None and user.is_admin:
        from bot.handlers.admin import _show_customer

        with contextlib.suppress(Exception):
            await _show_customer(message, session, customer_id)

    # Resume unpaid open order (do not orphan waiting_for_receipt).
    with contextlib.suppress(Exception):
        await resume_open_order_if_any(message, state, session, settings, user)

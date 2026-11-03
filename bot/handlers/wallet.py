"""Customer wallet balance screen."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.keyboards.customer import wallet_actions_keyboard
from bot.keyboards.main_menu import MenuCallback
from bot.texts import texts
from database.models import User
from services.brand import with_support
from services.ux_format import fa_toman as _fmt_toman
from services.wallet import WalletError, get_balance

logger = logging.getLogger(__name__)
router = Router(name="wallet")


async def render_wallet(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """The wallet screen, reachable from the inline menu or the bottom keyboard."""
    if db_user is None:
        await message.answer(texts.account_need_user)
        return
    try:
        balance = await get_balance(session, db_user.id)
    except WalletError:
        logger.exception("wallet balance failed user=%s", db_user.id)
        # BRAND.md rule 6: when it breaks, say so and point at a human. «دوباره
        # امتحان کن» on its own leaves someone whose balance will not load with
        # nowhere to go.
        await message.answer(with_support(texts.error_generic, settings))
        return
    text = texts.wallet_menu.format(balance=_fmt_toman(balance))
    empty = balance <= 0
    if empty:
        text = f"{text}\n\n{texts.wallet_empty_hint}"
    await message.answer(text, reply_markup=wallet_actions_keyboard(empty=empty))


@router.callback_query(F.data == MenuCallback.WALLET)
async def menu_wallet(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    await callback.answer()
    if callback.message is None or not isinstance(callback.message, Message):
        return
    await render_wallet(callback.message, session, settings, db_user)

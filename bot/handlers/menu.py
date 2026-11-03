"""Main menu callbacks (support). Referral lives in bot.handlers.referral."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.config import Settings
from bot.keyboards.disruption import support_keyboard
from bot.keyboards.main_menu import MenuCallback
from bot.texts import texts
from services.brand import with_support

router = Router(name="menu")


@router.callback_query(F.data == MenuCallback.SUPPORT)
async def menu_support(callback: CallbackQuery, settings: Settings) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            with_support(texts.support, settings), reply_markup=support_keyboard()
        )

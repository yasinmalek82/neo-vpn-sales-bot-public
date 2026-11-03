"""Inline main menu keyboards (Persian labels)."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.style import PRIMARY, styled
from services.ux_format import wallet_menu_label
from services.wallet import get_balance


class MenuCallback:
    BUY = "menu:buy"
    ACCOUNT = "menu:account"
    WALLET = "menu:wallet"
    REFERRAL = "menu:referral"
    TRIAL = "menu:trial"
    SUPPORT = "menu:support"
    ADMIN = "menu:admin"


def main_menu_keyboard(
    *,
    is_admin: bool,
    wallet_balance: int | None = None,
    is_reseller: bool = False,
) -> InlineKeyboardMarkup:
    """The menu, laid out by `bot.keyboards.style`. Wallet button carries the balance.

    Owner order: خرید → تست رایگان → سرویس‌های من → کیف پول → دعوت → پشتیبانی
    (+ مدیریت). Primary action alone on row 0; the rest paired.
    """
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="🛒 خرید اشتراک",
                callback_data=MenuCallback.BUY,
                style=styled(PRIMARY),
            )
        ],
        [
            InlineKeyboardButton(text="🎟 تست رایگان", callback_data=MenuCallback.TRIAL),
            InlineKeyboardButton(
                text="👥 مشتری‌های من" if is_reseller else "📡 سرویس‌های من",
                callback_data=MenuCallback.ACCOUNT,
            ),
        ],
        [
            InlineKeyboardButton(
                text=wallet_menu_label(wallet_balance),
                callback_data=MenuCallback.WALLET,
            ),
            InlineKeyboardButton(text="🎁 دعوت دوستان", callback_data=MenuCallback.REFERRAL),
        ],
    ]
    if is_admin:
        rows.append(
            [
                InlineKeyboardButton(text="💬 پشتیبانی", callback_data=MenuCallback.SUPPORT),
                InlineKeyboardButton(text="⚙️ مدیریت", callback_data=MenuCallback.ADMIN),
            ]
        )
    else:
        rows.append([InlineKeyboardButton(text="💬 پشتیبانی", callback_data=MenuCallback.SUPPORT)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def build_main_menu_keyboard(
    session: AsyncSession,
    *,
    user_id: int | None,
    is_admin: bool,
    is_reseller: bool = False,
) -> InlineKeyboardMarkup:
    """Async variant: loads wallet balance when ``user_id`` is set."""
    balance: int | None = None
    if user_id is not None:
        try:
            balance = await get_balance(session, user_id)
        except Exception:
            balance = None
    return main_menu_keyboard(is_admin=is_admin, wallet_balance=balance, is_reseller=is_reseller)

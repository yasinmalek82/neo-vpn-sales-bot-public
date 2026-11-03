"""Shared customer-facing CTAs (provision fail, receipt wait, wallet empty)."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.keyboards.account import AccountCB
from bot.keyboards.main_menu import MenuCallback


def provision_failed_keyboard() -> InlineKeyboardMarkup:
    """Clear next steps after payment OK but panel provision failed."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏦 کیف پول من", callback_data=MenuCallback.WALLET)],
            [InlineKeyboardButton(text="📡 سرویس‌های من", callback_data=MenuCallback.ACCOUNT)],
            [InlineKeyboardButton(text="🛒 خرید اشتراک", callback_data=MenuCallback.BUY)],
            [InlineKeyboardButton(text="💬 پشتیبانی", callback_data=MenuCallback.SUPPORT)],
        ]
    )


def receipt_waiting_keyboard() -> InlineKeyboardMarkup:
    """After receipt upload: wait for admin; light navigation."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data=AccountCB.BACK)],
            [InlineKeyboardButton(text="📡 سرویس‌های من", callback_data=MenuCallback.ACCOUNT)],
        ]
    )


def wallet_actions_keyboard(*, empty: bool = False) -> InlineKeyboardMarkup:
    """Wallet screen CTAs; emphasize buy when balance is empty/insufficient."""
    buy_label = "🛒 خرید اشتراک — از اینجا شروع کن" if empty else "🛒 خرید اشتراک"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=buy_label, callback_data=MenuCallback.BUY)],
            [InlineKeyboardButton(text="📡 سرویس‌های من", callback_data=MenuCallback.ACCOUNT)],
            [InlineKeyboardButton(text="💬 پشتیبانی", callback_data=MenuCallback.SUPPORT)],
        ]
    )

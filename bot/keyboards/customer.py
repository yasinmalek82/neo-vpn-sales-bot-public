"""Shared customer-facing CTAs (provision fail, receipt wait, wallet empty)."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from bot.keyboards.account import AccountCB
from bot.keyboards.main_menu import MenuCallback
from bot.keyboards.style import DANGER, styled


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


class SubmitPaymentReceiptCB(CallbackData, prefix="payreceipt"):
    order_id: int


class CancelPaymentCB(CallbackData, prefix="paycancel"):
    order_id: int


class ConfirmPaymentCancelCB(CallbackData, prefix="paycancelcheck"):
    order_id: int
    payment_message_id: int
    confirm: bool


def payment_cancel_confirmation_keyboard(
    order_id: int, payment_message_id: int
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="واریز نکردم، سفارش رو لغو کن",
                    callback_data=ConfirmPaymentCancelCB(
                        order_id=order_id, payment_message_id=payment_message_id, confirm=True
                    ).pack(),
                    style=styled(DANGER),
                )
            ],
            [
                InlineKeyboardButton(
                    text="برگشت به پرداخت",
                    callback_data=ConfirmPaymentCancelCB(
                        order_id=order_id, payment_message_id=payment_message_id, confirm=False
                    ).pack(),
                )
            ],
        ]
    )


def payment_copy_keyboard(
    card_number: str, amount_rial: int, *, order_id: int
) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text="کپی مبلغ (ریال)", copy_text=CopyTextButton(text=str(amount_rial))
        )
    ]
    if card_number:
        buttons.insert(
            0,
            InlineKeyboardButton(text="کپی شماره کارت", copy_text=CopyTextButton(text=card_number)),
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            buttons,
            [
                InlineKeyboardButton(
                    text="📸 ارسال رسید برای بررسی دستی",
                    callback_data=SubmitPaymentReceiptCB(order_id=order_id).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="لغو سفارش",
                    callback_data=CancelPaymentCB(order_id=order_id).pack(),
                    style=styled(DANGER),
                )
            ],
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

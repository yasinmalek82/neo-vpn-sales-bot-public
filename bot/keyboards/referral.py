"""Inline keyboards for referral / free trial."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.keyboards.main_menu import MenuCallback
from services.ux_format import to_persian_digits


class ReferralCB:
    TRIAL = "ref:trial"
    TRIAL_GROUP_PREFIX = "ref:trial:g:"  # + store group slug
    BACK = "ref:back"
    RESELLER_TRIAL_RESUME_PREFIX = "ref:reseller-retry:"


def referral_keyboard(*, trial_eligible: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if trial_eligible:
        rows.append(
            [InlineKeyboardButton(text="🎟 دریافت تست رایگان", callback_data=ReferralCB.TRIAL)]
        )
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت به منو", callback_data=ReferralCB.BACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def invite_share_keyboard(share_url: str) -> InlineKeyboardMarkup:
    """Under the forwardable pitch: one tap opens Telegram's share sheet."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📤 فرستادن برای دوستان", url=share_url)]]
    )


def trial_services_keyboard() -> InlineKeyboardMarkup:
    """A repeat tap on an existing trial. The link stays in «سرویس‌های من»."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📡 سرویس‌های من", callback_data=MenuCallback.ACCOUNT)],
            [InlineKeyboardButton(text="⬅️ بازگشت به منو", callback_data=ReferralCB.BACK)],
        ]
    )


def trial_exit_keyboard() -> InlineKeyboardMarkup:
    """Every way out of the trial screen.

    The trial is a leaf: whatever the answer — claimed, already used, disabled —
    the customer needs somewhere to go next, and the most useful next step for
    someone who just learned they have no trial left is the shop.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 خرید اشتراک", callback_data=MenuCallback.BUY)],
            [InlineKeyboardButton(text="⬅️ بازگشت به منو", callback_data=ReferralCB.BACK)],
        ]
    )


def trial_groups_keyboard(offers: Sequence[Any]) -> InlineKeyboardMarkup:
    """One button per group with a trial, labelled with what the trial is.

    Size and length are on the button because they are what differs: the
    tunnel trial and the direct one are not the same offer.
    """
    rows = [
        [
            InlineKeyboardButton(
                text=(
                    f"{offer.title_fa} | {to_persian_digits(offer.data_limit_gb)} گیگ • "
                    f"{to_persian_digits(offer.duration_days)} روز"
                ),
                callback_data=f"{ReferralCB.TRIAL_GROUP_PREFIX}{offer.slug}",
            )
        ]
        for offer in offers
    ]
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت به منو", callback_data=ReferralCB.BACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reseller_trial_recovery_keyboard(
    *, order_id: int, customer_label: str | None
) -> InlineKeyboardMarkup:
    """Explicit recovery action for one reseller giveaway order."""
    label = (customer_label or "مشتری ثبت‌شده").strip() or "مشتری ثبت‌شده"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🔄 تلاش دوباره برای {label[:40]}",
                    callback_data=f"{ReferralCB.RESELLER_TRIAL_RESUME_PREFIX}{order_id}",
                )
            ],
            *trial_exit_keyboard().inline_keyboard,
        ]
    )

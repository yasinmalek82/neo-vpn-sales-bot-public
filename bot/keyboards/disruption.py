"""Keyboards for «📡 گزارش اختلال»: support entry, ISP, city."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.keyboards.referral import ReferralCB
from services.disruption import CITIES, ISPS


class DisruptionCB:
    START = "dis:start"
    ISP_PREFIX = "dis:isp:"  # + key from services.disruption.ISPS
    CITY_PREFIX = "dis:city:"  # + index into CITIES
    CITY_WRITE = "dis:cityw"


def _back() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="⬅️ بازگشت به منو", callback_data=ReferralCB.BACK)]


def _pairs(buttons: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]


def support_keyboard() -> InlineKeyboardMarkup:
    """The support screen was a leaf with no way on; it now leads to the report."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📡 گزارش اختلال", callback_data=DisruptionCB.START)],
            _back(),
        ]
    )


def isp_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=label, callback_data=f"{DisruptionCB.ISP_PREFIX}{key}")
        for key, label in ISPS
    ]
    return InlineKeyboardMarkup(inline_keyboard=[*_pairs(buttons), _back()])


def city_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=city, callback_data=f"{DisruptionCB.CITY_PREFIX}{index}")
        for index, city in enumerate(CITIES)
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            *_pairs(buttons),
            [
                InlineKeyboardButton(
                    text="✍️ شهر دیگه، می‌نویسم", callback_data=DisruptionCB.CITY_WRITE
                )
            ],
            _back(),
        ]
    )


def done_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[_back()])

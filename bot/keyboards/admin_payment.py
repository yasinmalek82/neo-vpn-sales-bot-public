"""Admin approve/reject keyboards for payment review."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.keyboards.style import DANGER, PRIMARY, SUCCESS, styled

# Quick reject reason keys → shown as buttons (labels live in fa.py via handlers).
REJECT_QUICK_REASONS: list[tuple[str, str]] = [
    ("unreadable", "فیش ناخوانا"),
    ("wrong_amount", "مبلغ نادرست"),
    ("wrong_card", "کارت اشتباه"),
    ("duplicate", "تکراری"),
]


class PayCB:
    APPROVE_PREFIX = "pay:approve:"
    REJECT_PREFIX = "pay:reject:"  # start reject-reason flow
    REJECT_REASON_PREFIX = "pay:rjr:"  # pay:rjr:{order_id}:{reason_key}
    REJECT_CUSTOM_PREFIX = "pay:rjc:"  # pay:rjc:{order_id} → custom text FSM
    REJECT_ABORT_PREFIX = "pay:rjx:"  # abort reason entry


def payment_review_keyboard(order_id: int) -> InlineKeyboardMarkup:
    """Submit-time admin DM: approve/reject + open order detail (receipt context)."""
    from bot.keyboards.admin import AdminCB

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ تایید",
                    style=styled(SUCCESS),
                    callback_data=f"{PayCB.APPROVE_PREFIX}{order_id}",
                ),
                InlineKeyboardButton(
                    text="❌ رد",
                    style=styled(DANGER),
                    callback_data=f"{PayCB.REJECT_PREFIX}{order_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📋 جزئیات سفارش",
                    callback_data=f"{AdminCB.ORDER}{order_id}",
                )
            ],
        ]
    )


def late_receipt_keyboard(order_id: int, amount_toman: int) -> InlineKeyboardMarkup:
    """Under a receipt for an EXPIRED order: pay it into the wallet, or drop it.

    The amount on the button is the card due; the tap re-derives it from the order,
    so the label is information and never the value that gets credited.
    """
    from bot.keyboards.admin import AdminCB
    from services.ux_format import format_toman

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🏦 شارژ کیف پول ({format_toman(amount_toman, persian_digits=True)})",
                    style=styled(PRIMARY),
                    callback_data=f"{AdminCB.LATE_CREDIT}{order_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ نادیده",
                    callback_data=f"{AdminCB.LATE_IGNORE}{order_id}",
                ),
                InlineKeyboardButton(
                    text="📋 جزئیات سفارش",
                    callback_data=f"{AdminCB.ORDER}{order_id}",
                ),
            ],
        ]
    )


def reject_reason_keyboard(order_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key, label in REJECT_QUICK_REASONS:
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"{PayCB.REJECT_REASON_PREFIX}{order_id}:{key}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="✏️ دلیل سفارشی",
                callback_data=f"{PayCB.REJECT_CUSTOM_PREFIX}{order_id}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="لغو رد",
                callback_data=f"{PayCB.REJECT_ABORT_PREFIX}{order_id}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_resolved_keyboard(*, has_next: bool = True) -> InlineKeyboardMarkup:
    """After approve/reject: next pending + home."""
    from bot.keyboards.admin import AdminCB

    rows: list[list[InlineKeyboardButton]] = []
    if has_next:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⏭ بعدی در انتظار",
                    callback_data=AdminCB.NEXT_PENDING,
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="صف در انتظار", callback_data=AdminCB.PENDING),
            InlineKeyboardButton(text="خانه ادمین", callback_data=AdminCB.HOME),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)

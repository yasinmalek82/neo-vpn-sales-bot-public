"""Read-only legacy history navigation; no settlement/credit callback."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.keyboards.admin import AdminCB
from bot.keyboards.nav import nav_rows
from services.mirza_history import PAGE_SIZE, HistoryPage
from services.ux_format import to_persian_digits


class MirzaCB:
    PAGE = AdminCB.MIRZA_HISTORY


def history_callback(user_id: int | None, offset: int, review_only: bool) -> str:
    return f"{MirzaCB.PAGE}{user_id if user_id is not None else 'all'}:{offset}:{int(review_only)}"


def mirza_history_keyboard(
    page: HistoryPage, *, user_id: int | None, review_only: bool
) -> InlineKeyboardMarkup:
    rows = []
    pager = []
    if page.offset:
        pager.append(
            InlineKeyboardButton(
                text="قبلی",
                callback_data=history_callback(
                    user_id, max(0, page.offset - PAGE_SIZE), review_only
                ),
            )
        )
    if page.offset + PAGE_SIZE < page.total:
        pager.append(
            InlineKeyboardButton(
                text="بعدی",
                callback_data=history_callback(user_id, page.offset + PAGE_SIZE, review_only),
            )
        )
    if pager:
        rows.append(pager)
    if user_id is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text="همهٔ سوابق" if review_only else "فقط نیازمند بررسی",
                    callback_data=history_callback(user_id, 0, not review_only),
                )
            ]
        )
    else:
        seen = set()
        for record in page.rows:
            if record.user_id is not None and record.user_id not in seen:
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=f"👤 مشتری {to_persian_digits(record.telegram_id or record.user_id)}",
                            callback_data=f"{AdminCB.CUSTOMER_PICK}{record.user_id}",
                        )
                    ]
                )
                seen.add(record.user_id)
    rows.extend(
        nav_rows(
            back_to=f"{AdminCB.CUSTOMER_PICK}{user_id}" if user_id else AdminCB.SEC_SALES,
            back_label="مشتری" if user_id else "فروش و سفارش",
        )
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)

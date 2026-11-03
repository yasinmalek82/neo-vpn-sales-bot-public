"""Review and explicit confirmation for linking an existing service."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.keyboards.admin import AdminCB
from bot.keyboards.nav import nav_rows
from bot.keyboards.style import SUCCESS, styled


class LinkCB:
    START = AdminCB.CUST_LINK
    CONFIRM = AdminCB.CUST_LINK_GO


def account_link_keyboard(
    user_id: int | None = None, *, nonce: str | None = None
) -> InlineKeyboardMarkup:
    rows = []
    if user_id is not None and nonce is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ تأیید اتصال",
                    callback_data=f"{LinkCB.CONFIRM}{user_id}:{nonce}",
                    style=styled(SUCCESS),
                )
            ]
        )
    rows.extend(
        nav_rows(
            back_to=f"{AdminCB.CUSTOMER_PICK}{user_id}" if user_id else None, back_label="مشتری"
        )
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)

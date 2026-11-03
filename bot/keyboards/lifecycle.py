"""«🗓 هشدار و حذف سرویس» keyboards (admin).

Marks follow the owner's rule (2026-10-03): state first, ✅ / ❌ for what is on
or off, and the value in the label for a number. Colour goes through
``styled()``, one PRIMARY per keyboard.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.keyboards.main_menu import MenuCallback
from bot.keyboards.nav import cancel_button, nav_rows
from bot.keyboards.style import DANGER, PRIMARY, styled
from bot.texts import texts
from database.models.lifecycle import DELETE_MODE_OFF, DELETE_MODE_ON, DELETE_MODE_REHEARSAL
from services.lifecycle_batches import BatchRow, reason_fa
from services.lifecycle_settings import LifecycleConfig
from services.ux_format import to_persian_digits


class LifeCB:
    OPEN = "adm:lc"
    EDIT = "adm:lc:e:"  # + field code
    MODE = "adm:lc:m:"  # + off | rehearsal | on
    MODE_ON_OK = "adm:lc:mok"  # the confirmed «روشن»
    PREVIEW = "adm:lc:pv"
    LIST = "adm:lc:l:"  # + batch id:page
    ROW = "adm:lc:r:"  # + batch id:account id:page — that service's card
    EXEMPT = "adm:lc:x:"  # + batch id:account id:page — «حذف نشود» / «حذف شود»
    APPROVE = "adm:lc:ap:"  # + batch id — the confirm screen
    APPROVE_OK = "adm:lc:ok:"  # + batch id — the confirmed approval
    FRESH = "adm:lc:nb:"  # + preview batch id — build an approvable list from it


# Short codes keep callback_data far under Telegram's 64 bytes.
FIELD_CODES: dict[str, str] = {
    "wd": "warn_days",
    "wv": "warn_volume_percents",
    "de": "delete_after_expiry_days",
    "dl": "delete_after_limited_days",
    "dt": "delete_trial_after_days",
    "dw": "delete_warn_days_before",
}
CODE_OF_FIELD: dict[str, str] = {v: k for k, v in FIELD_CODES.items()}


def _fa(n: int) -> str:
    return to_persian_digits(n)


def _list_label(label: str, values: tuple[int, ...], *, suffix: str = "") -> str:
    if not values:
        return f"❌ {label}"
    return f"✅ {label}: {' '.join(_fa(v) + suffix for v in values)}"


def _days_label(label: str, days: int) -> str:
    if days <= 0:
        return f"❌ {label}"
    return f"✅ {label}: {_fa(days)} روز"


def lifecycle_keyboard(
    config: LifecycleConfig, *, can_edit: bool = True, batch: tuple[int, int] | None = None
) -> InlineKeyboardMarkup:
    """``batch`` is (batch id, number of services) of the open list waiting for the owner."""
    rows: list[list[InlineKeyboardButton]] = []
    if can_edit:
        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.admin_life_preview_button,
                    style=styled(PRIMARY),
                    callback_data=LifeCB.PREVIEW,
                )
            ]
        )

        def edit(text: str, field: str) -> InlineKeyboardButton:
            return InlineKeyboardButton(
                text=text, callback_data=f"{LifeCB.EDIT}{CODE_OF_FIELD[field]}"
            )

        rows.append(
            [
                edit(_list_label("هشدار روز", config.warn_days), "warn_days"),
                edit(
                    _list_label("هشدار حجم", config.warn_volume_percents, suffix="٪"),
                    "warn_volume_percents",
                ),
            ]
        )
        rows.append(
            [
                edit(
                    _days_label("حذف پس از انقضا", config.delete_after_expiry_days),
                    "delete_after_expiry_days",
                ),
                edit(
                    _days_label("حذف پس از اتمام حجم", config.delete_after_limited_days),
                    "delete_after_limited_days",
                ),
            ]
        )
        rows.append(
            [
                edit(
                    _days_label("حذف تست", config.delete_trial_after_days),
                    "delete_trial_after_days",
                ),
                edit(
                    _days_label("هشدار پیش از حذف", config.delete_warn_days_before),
                    "delete_warn_days_before",
                ),
            ]
        )
        mode = config.delete_mode

        def mode_button(text: str, value: str) -> InlineKeyboardButton:
            label = f"✅ {text}" if mode == value else text
            return InlineKeyboardButton(text=label, callback_data=f"{LifeCB.MODE}{value}")

        rows.append(
            [
                mode_button("خاموش", DELETE_MODE_OFF),
                mode_button("👁 تمرین", DELETE_MODE_REHEARSAL),
                mode_button("روشن", DELETE_MODE_ON),
            ]
        )
    if batch is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.admin_life_batch_button.format(n=_fa(batch[1])),
                    callback_data=f"{LifeCB.LIST}{batch[0]}:0",
                )
            ]
        )
    rows.extend(nav_rows(back_to="adm:hx:set", back_label="تنظیمات", home=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def lifecycle_input_keyboard() -> InlineKeyboardMarkup:
    """Under a typed-number prompt: a way out, and a way back to the screen."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [cancel_button()],
            *nav_rows(back_to=LifeCB.OPEN, back_label="هشدار و حذف سرویس", home=True),
        ]
    )


def lifecycle_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=nav_rows(back_to=LifeCB.OPEN, back_label="هشدار و حذف سرویس", home=True)
    )


def lifecycle_mode_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.admin_life_mode_confirm_yes,
                    style=styled(DANGER),
                    callback_data=LifeCB.MODE_ON_OK,
                )
            ],
            *nav_rows(back_to=LifeCB.OPEN, back_label="هشدار و حذف سرویس", home=True),
        ]
    )


LIST_PAGE_SIZE = 8
_PARENT = "هشدار و حذف سرویس"


def _trim(text: str, room: int) -> str:
    return text if len(text) <= room else text[: max(1, room - 1)] + "…"


def batch_row_label(row: BatchRow) -> str:
    """«خدمت ۰۳ • علی • ۱۴ روز از انقضا»: the title, the owner, why it is listed.

    The title is the name the admin's service lists use; only names are trimmed,
    never the reason.
    """
    reason = reason_fa(row.reason_code, row.reason_days)
    if row.action == "warn":
        reason = f"⚠️ {reason}"
    if row.deleted:
        reason = f"✅ {reason}"
    return f"{_trim(row.title, 16)} • {_trim(row.owner, 10)} • {reason}"


def batch_list_keyboard(
    rows: list[BatchRow],
    *,
    batch_id: int,
    page: int,
    total: int,
    delete_count: int,
    warn_count: int = 0,
    approvable: bool,
    voided: bool,
    preview: bool,
) -> InlineKeyboardMarkup:
    """One button per service, a page at a time; the delete button only on a valid list."""
    keyboard: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=batch_row_label(row),
                callback_data=f"{LifeCB.ROW}{batch_id}:{row.account_id}:{page}",
            )
        ]
        for row in rows
    ]
    pager: list[InlineKeyboardButton] = []
    if page > 0:
        pager.append(
            InlineKeyboardButton(
                text="⬅️ قبلی", callback_data=f"{LifeCB.LIST}{batch_id}:{page - 1}"
            )
        )
    if (page + 1) * LIST_PAGE_SIZE < total:
        pager.append(
            InlineKeyboardButton(
                text="بعدی ➡️", callback_data=f"{LifeCB.LIST}{batch_id}:{page + 1}"
            )
        )
    if pager:
        keyboard.append(pager)
    if approvable and (delete_count > 0 or (warn_count > 0 and not preview)):
        label = (
            texts.admin_life_approve_button.format(n=_fa(delete_count))
            if delete_count > 0
            else texts.admin_life_approve_warn_button.format(n=_fa(warn_count))
        )
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=label,
                    style=styled(DANGER if delete_count > 0 else None),
                    callback_data=f"{LifeCB.APPROVE}{batch_id}",
                )
            ]
        )
    if voided or (preview and delete_count > 0):
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.admin_life_fresh_button, callback_data=f"{LifeCB.FRESH}{batch_id}"
                )
            ]
        )
    keyboard.extend(nav_rows(back_to=LifeCB.OPEN, back_label=_PARENT, home=True))
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def card_flow_rows(
    batch_id: int, account_id: int, page: int, *, exempt: bool
) -> list[list[InlineKeyboardButton]]:
    """Under the existing service card, in this flow only: the exemption and the way back."""
    toggle = texts.admin_life_unexempt_button if exempt else texts.admin_life_exempt_button
    return [
        [
            InlineKeyboardButton(
                text=toggle, callback_data=f"{LifeCB.EXEMPT}{batch_id}:{account_id}:{page}"
            )
        ],
        [
            InlineKeyboardButton(
                text=texts.admin_life_back_to_list,
                callback_data=f"{LifeCB.LIST}{batch_id}:{page}",
            )
        ],
    ]


def approve_confirm_keyboard(batch_id: int, count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.admin_life_approve_yes.format(n=_fa(count)),
                    style=styled(DANGER),
                    callback_data=f"{LifeCB.APPROVE_OK}{batch_id}",
                )
            ],
            *nav_rows(back_to=f"{LifeCB.LIST}{batch_id}:0", back_label="فهرست حذف", home=True),
        ]
    )


def buy_keyboard() -> InlineKeyboardMarkup:
    """Under «سرویست حذف شد» and «تست تموم می‌شه»: the way to buy a plan (no renew button)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 خرید اشتراک", callback_data=MenuCallback.BUY)],
        ]
    )

"""Purchase flow inline keyboards."""

from __future__ import annotations

from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.keyboards.account import AccountCB
from bot.keyboards.main_menu import MenuCallback
from bot.keyboards.style import PRIMARY, styled
from services.store_catalog import shelf_plan_label
from services.ux_format import to_persian_digits


class PurchaseCB:
    TYPE_VOLUME = "buy:type:volume"
    TYPE_UNLIMITED = "buy:type:unlimited"
    CAT_PREFIX = "buy:cat:"  # + category id (published catalog)
    DUR_PREFIX = "buy:d:"  # + duration_days, or "n" for بدون انقضا
    TIER_PREFIX = "buy:t:"  # + "economy" | "premium"
    READY_PREFIX = "buy:ready:"  # + package id
    CUSTOM = "buy:custom"  # fall through to custom volume/users flow
    VOL_PREFIX = "buy:vol:"
    USERS_PREFIX = "buy:users:"
    DAYS_PREFIX = "buy:days:"
    MONTHS_PREFIX = "buy:months:"
    PROMO_SKIP = "buy:promo:skip"
    CONFIRM = "buy:confirm"
    RESTART = "buy:restart"
    CANCEL = "buy:cancel"
    RENEW_SAME = "buy:renew:same"
    RENEW_NEW = "buy:renew:new"
    SWITCH_PREFIX = "buy:switch:"  # + account id — another plan on this same service
    RENEW_CUSTOM_OK = "buy:rc:ok"  # «تمدید از نو»: take the volume and days on screen
    RENEW_CUSTOM_DAYS_PREFIX = "buy:rc:d:"  # + duration_days


def encode_duration_cb(duration_days: int | None) -> str:
    """``None`` → ``n``; a real day count (including ``0``) stays decimal."""
    if duration_days is None:
        return "n"
    return str(int(duration_days))


def parse_duration_cb(raw: str) -> int | None:
    """Inverse of :func:`encode_duration_cb`. ``"n"`` → ``None``; ``"0"`` → ``0``."""
    token = (raw or "").strip()
    if token == "n":
        return None
    if token.lstrip("-").isdigit():
        return int(token)
    raise ValueError(f"invalid duration callback: {raw!r}")


def duration_toggle_label(duration_days: int | None, *, selected: bool) -> str:
    from bot.texts import texts

    if duration_days is None:
        label = texts.purchase_duration_none
    else:
        months = max(1, int(duration_days) // 30)
        label = texts.purchase_duration_month.format(n=to_persian_digits(months))
    if selected:
        label = f"{label} ✅"
    return label


def tier_toggle_label(tier: str, *, selected: bool) -> str:
    from bot.texts import texts

    label = texts.purchase_tier_economy if tier == "economy" else texts.purchase_tier_premium
    if selected:
        label = f"{label} ✅"
    return label


def plan_type_keyboard() -> InlineKeyboardMarkup:
    """Legacy fallback when no published store categories are available."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📦 پلن حجمی", callback_data=PurchaseCB.TYPE_VOLUME)],
            [InlineKeyboardButton(text="♾️ پلن نامحدود", callback_data=PurchaseCB.TYPE_UNLIMITED)],
            [InlineKeyboardButton(text="❌ انصراف", callback_data=PurchaseCB.CANCEL)],
        ]
    )


def categories_keyboard(categories: list[Any]) -> InlineKeyboardMarkup:
    """Layer-1 free-form product groups from published store."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for cat in categories:
        label = str(getattr(cat, "title_fa", cat))
        if len(label) > 28:
            label = label[:25] + "…"
        row.append(
            InlineKeyboardButton(
                text=label,
                callback_data=f"{PurchaseCB.CAT_PREFIX}{cat.id}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="❌ انصراف", callback_data=PurchaseCB.CANCEL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ready_plans_keyboard(
    packages: list[Any],
    *,
    include_custom: bool,
    prices_by_slug: dict[str, int] | None = None,
    durations: list[int | None] | None = None,
    tiers: list[str] | None = None,
    selected_duration: int | None = None,
    selected_tier: str = "economy",
    flat_mode: bool = False,
) -> InlineKeyboardMarkup:
    """Shelf with duration (and optional tier) toggles above the plan list.

    ``flat_mode`` hides duration/tier rows and labels each plan from its own
    dimensions instead of ``title_fa``.
    """
    rows: list[list[InlineKeyboardButton]] = []

    if not flat_mode:
        dur_list = list(durations or [])
        if dur_list:
            dur_row: list[InlineKeyboardButton] = []
            for d in dur_list:
                dur_row.append(
                    InlineKeyboardButton(
                        text=duration_toggle_label(d, selected=(d == selected_duration)),
                        callback_data=f"{PurchaseCB.DUR_PREFIX}{encode_duration_cb(d)}",
                    )
                )
                if len(dur_row) == 3:
                    rows.append(dur_row)
                    dur_row = []
            if dur_row:
                rows.append(dur_row)

        tier_list = list(tiers or [])
        if len(tier_list) > 1:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=tier_toggle_label(t, selected=(t == selected_tier)),
                        callback_data=f"{PurchaseCB.TIER_PREFIX}{t}",
                    )
                    for t in tier_list
                ]
            )

    for pkg in packages:
        slug = str(getattr(pkg, "slug", "") or "")
        price = None
        if prices_by_slug and slug in prices_by_slug:
            price = prices_by_slug[slug]
        elif hasattr(pkg, "_shelf_price_toman"):
            price = pkg._shelf_price_toman
        label = shelf_plan_label(pkg, price, flat_mode=flat_mode)
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"{PurchaseCB.READY_PREFIX}{pkg.id}",
                )
            ]
        )

    if include_custom:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🧩 خرید سفارشی",
                    callback_data=PurchaseCB.CUSTOM,
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="🔁 شروع دوباره", callback_data=PurchaseCB.RESTART),
            InlineKeyboardButton(text="❌ انصراف", callback_data=PurchaseCB.CANCEL),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def options_keyboard(
    prefix: str,
    values: list[int],
    *,
    suffix: str = "",
    label_for: Any | None = None,
    columns: int = 1,
) -> InlineKeyboardMarkup:
    """Build option buttons. Prefer label_for(value) for Persian unit labels.

    ``columns=1`` (default) stacks options vertically — clearer for گیگ/ماه.
    """
    cols = max(1, int(columns))
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for value in values:
        label = str(label_for(value)) if label_for is not None else f"{value}{suffix}"
        row.append(
            InlineKeyboardButton(
                text=label,
                callback_data=f"{prefix}{value}",
            )
        )
        if len(row) == cols:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(text="🔁 شروع دوباره", callback_data=PurchaseCB.RESTART),
            InlineKeyboardButton(text="❌ انصراف", callback_data=PurchaseCB.CANCEL),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def promo_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎫 بدون کد تخفیف", callback_data=PurchaseCB.PROMO_SKIP)],
            [
                InlineKeyboardButton(text="🔁 شروع دوباره", callback_data=PurchaseCB.RESTART),
                InlineKeyboardButton(text="❌ انصراف", callback_data=PurchaseCB.CANCEL),
            ],
        ]
    )


def confirm_keyboard(*, switch_account_id: int | None = None) -> InlineKeyboardMarkup:
    """Confirm the order. On a renew, also offer another plan for the same service.

    That second button is how an unlimited customer whose direct stopped
    working moves to the tunnel without a new link to import.
    """
    rows = [
        [
            InlineKeyboardButton(
                text="✅ ثبت سفارش",
                callback_data=PurchaseCB.CONFIRM,
                style=styled(PRIMARY),
            )
        ]
    ]
    if switch_account_id is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔁 پلن دیگه روی همین سرویس",
                    callback_data=f"{PurchaseCB.SWITCH_PREFIX}{switch_account_id}",
                )
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            *rows,
            [
                InlineKeyboardButton(text="🔁 شروع دوباره", callback_data=PurchaseCB.RESTART),
                InlineKeyboardButton(text="❌ انصراف", callback_data=PurchaseCB.CANCEL),
            ],
        ]
    )


def renew_custom_keyboard(
    durations: list[int], *, selected_days: int | None
) -> InlineKeyboardMarkup:
    """The renew-from-scratch screen: «✅ همین» alone and coloured, the durations
    under it with the current one marked, «❌ انصراف» last and alone."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="✅ همین", callback_data=PurchaseCB.RENEW_CUSTOM_OK, style=styled(PRIMARY)
            )
        ]
    ]
    row: list[InlineKeyboardButton] = []
    for days in durations:
        row.append(
            InlineKeyboardButton(
                text=duration_toggle_label(int(days), selected=int(days) == selected_days),
                callback_data=f"{PurchaseCB.RENEW_CUSTOM_DAYS_PREFIX}{int(days)}",
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="❌ انصراف", callback_data=PurchaseCB.CANCEL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def renew_plan_off_keyboard() -> InlineKeyboardMarkup:
    """The bought plan is off; the next step is a new service, not a retry."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 سرویس جدید",
                    callback_data=MenuCallback.BUY,
                    style=styled(PRIMARY),
                )
            ],
            [InlineKeyboardButton(text="⬅️ بازگشت به سرویس‌ها", callback_data=AccountCB.LIST)],
        ]
    )


def renew_or_new_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 تمدید همان سرویس", callback_data=PurchaseCB.RENEW_SAME)],
            [InlineKeyboardButton(text="✨ سرویس جدید", callback_data=PurchaseCB.RENEW_NEW)],
            [
                InlineKeyboardButton(text="🔁 شروع دوباره", callback_data=PurchaseCB.RESTART),
                InlineKeyboardButton(text="❌ انصراف", callback_data=PurchaseCB.CANCEL),
            ],
        ]
    )

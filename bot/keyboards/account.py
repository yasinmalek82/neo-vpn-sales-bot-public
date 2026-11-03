"""Inline keyboards for the user account / services screen."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.keyboards.main_menu import MenuCallback
from bot.keyboards.referral import ReferralCB
from bot.keyboards.style import PRIMARY, styled
from services.accounts import account_label
from services.ux_format import service_status_emoji

_HOWTO_PLATFORMS = frozenset({"android", "ios", "windows", "macos"})


class _AccountLike(Protocol):
    id: int
    panel_username: str | None
    display_name: str | None
    service_name: str | None


class AccountCB:
    REFRESH = "acct:refresh"
    ORDERS = "acct:orders"
    LINK = "acct:link"
    QR = "acct:qr"
    BACK = "acct:back"
    LIST = "acct:list"
    SVC_PREFIX = "acct:svc:"
    SVC_REFRESH_PREFIX = "acct:svc:refresh:"
    SVC_LINK_PREFIX = "acct:svc:link:"
    SVC_QR_PREFIX = "acct:svc:qr:"
    SVC_HOWTO_PREFIX = "acct:svc:howto:"
    SVC_RENEW_PREFIX = "acct:svc:renew:"
    SVC_REVOKE_PREFIX = "acct:svc:revoke:"
    # Reseller desk: a shop with thirty customers needs paging and search.
    RS_PAGE = "acct:rs:p:"  # + offset
    RS_SEARCH = "acct:rs:find"
    RS_CLEAR = "acct:rs:all"
    RS_RENAME = "acct:rs:name:"  # + account id

    @staticmethod
    def svc(account_id: int) -> str:
        return f"{AccountCB.SVC_PREFIX}{account_id}"

    @staticmethod
    def svc_refresh(account_id: int) -> str:
        return f"{AccountCB.SVC_REFRESH_PREFIX}{account_id}"

    @staticmethod
    def svc_link(account_id: int) -> str:
        return f"{AccountCB.SVC_LINK_PREFIX}{account_id}"

    @staticmethod
    def svc_qr(account_id: int) -> str:
        return f"{AccountCB.SVC_QR_PREFIX}{account_id}"

    @staticmethod
    def svc_howto(account_id: int, platform: str | None = None) -> str:
        if platform:
            return f"{AccountCB.SVC_HOWTO_PREFIX}{account_id}:{platform}"
        return f"{AccountCB.SVC_HOWTO_PREFIX}{account_id}"

    @staticmethod
    def svc_renew(account_id: int) -> str:
        return f"{AccountCB.SVC_RENEW_PREFIX}{account_id}"

    @staticmethod
    def svc_revoke(account_id: int) -> str:
        return f"{AccountCB.SVC_REVOKE_PREFIX}{account_id}"

    @staticmethod
    def svc_revoke_confirm(account_id: int) -> str:
        return f"{AccountCB.SVC_REVOKE_PREFIX}{account_id}:confirm"


def parse_howto_callback(data: str | None) -> tuple[int, str | None] | None:
    """Parse ``acct:svc:howto:{id}`` or ``acct:svc:howto:{id}:{platform}``."""
    if not data or not data.startswith(AccountCB.SVC_HOWTO_PREFIX):
        return None
    rest = data[len(AccountCB.SVC_HOWTO_PREFIX) :]
    if not rest:
        return None
    if ":" in rest:
        id_part, platform = rest.split(":", 1)
        platform = platform.strip().lower()
        if not id_part.isdigit() or platform not in _HOWTO_PLATFORMS:
            return None
        return int(id_part), platform
    if not rest.isdigit():
        return None
    return int(rest), None


def parse_revoke_callback(data: str | None) -> tuple[int, bool] | None:
    """Parse ``acct:svc:revoke:{id}`` or ``acct:svc:revoke:{id}:confirm``."""
    if not data or not data.startswith(AccountCB.SVC_REVOKE_PREFIX):
        return None
    rest = data[len(AccountCB.SVC_REVOKE_PREFIX) :]
    if not rest:
        return None
    if rest.endswith(":confirm"):
        id_part = rest[: -len(":confirm")]
        if not id_part.isdigit():
            return None
        return int(id_part), True
    if not rest.isdigit():
        return None
    return int(rest), False


def services_list_keyboard(
    services: list[_AccountLike],
    *,
    status_by_id: dict[int, tuple[str | None, datetime | None]] | None = None,
    warn_days: int = 7,
) -> InlineKeyboardMarkup:
    """Named services as inline buttons with optional color-status emoji."""
    rows: list[list[InlineKeyboardButton]] = []
    for svc in services:
        label = account_label(svc)  # type: ignore[arg-type]
        status: str | None = None
        expire: datetime | None = None
        if status_by_id and svc.id in status_by_id:
            status, expire = status_by_id[svc.id]
        emoji = service_status_emoji(status, expire, warn_days=warn_days)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{emoji} {label}",
                    callback_data=AccountCB.svc(svc.id),
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="🛒 خرید اشتراک", callback_data=MenuCallback.BUY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت به منو", callback_data=AccountCB.BACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reseller_services_keyboard(
    page: Any,
    *,
    status_by_id: dict[int, tuple[str | None, datetime | None]] | None = None,
    warn_days: int = 7,
) -> InlineKeyboardMarkup:
    """A shop's customer list: label first, paged, searchable.

    The customer version labels a row with the panel username, which is the
    right answer for someone with two services of their own. A reseller has
    thirty belonging to thirty different people, and `neo_a7f3` identifies none
    of them — so the label they typed comes first here.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for svc in page.rows:
        status: str | None = None
        expire: datetime | None = None
        if status_by_id and svc.id in status_by_id:
            status, expire = status_by_id[svc.id]
        emoji = service_status_emoji(status, expire, warn_days=warn_days)
        name = (svc.display_name or svc.service_name or "").strip() or svc.panel_username
        label = f"{emoji} {name}"
        if len(label) > 55:
            label = label[:52] + "…"
        rows.append([InlineKeyboardButton(text=label, callback_data=AccountCB.svc(svc.id))])

    pager: list[InlineKeyboardButton] = []
    if page.has_prev:
        pager.append(
            InlineKeyboardButton(
                text="⬅️ قبلی", callback_data=f"{AccountCB.RS_PAGE}{page.prev_offset}"
            )
        )
    if page.has_next:
        pager.append(
            InlineKeyboardButton(
                text="بعدی ➡️", callback_data=f"{AccountCB.RS_PAGE}{page.next_offset}"
            )
        )
    if pager:
        rows.append(pager)

    if page.query:
        rows.append(
            [InlineKeyboardButton(text="✖️ همهٔ سرویس‌ها", callback_data=AccountCB.RS_CLEAR)]
        )
    else:
        rows.append(
            [InlineKeyboardButton(text="🔎 جستجوی مشتری", callback_data=AccountCB.RS_SEARCH)]
        )
    rows.append([InlineKeyboardButton(text="🛒 سرویس جدید", callback_data=MenuCallback.BUY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت به منو", callback_data=AccountCB.BACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reseller_search_cancel_keyboard() -> InlineKeyboardMarkup:
    """A prompt with no way out is the dead end this project keeps closing."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ انصراف", callback_data=AccountCB.RS_CLEAR)]]
    )


def service_detail_keyboard(
    account_id: int,
    *,
    has_link: bool,
    show_renew: bool = False,
    can_rename: bool = False,
    is_trial: bool = False,
) -> InlineKeyboardMarkup:
    """Detail actions for one named service; back returns to service list.

    **A trial has no «📅 تمدید»**: there is nothing to renew, and the next step is
    buying. «🛒 خرید اشتراک» takes its place (and its accent when expiring).

    Laid out per `bot.keyboards.style`: seven full-width rows of equal weight
    emphasised nothing, so the action the screen exists for now takes the first
    row alone and the rest pair up.

    **Which action that is depends on the service.** One that is expiring is
    here to be renewed, so renew takes the accent. A healthy one is here to be
    connected to, but renew stays on the card — the owner renews before the
    warning window, and hiding the button made that impossible to test.
    """
    primary: InlineKeyboardButton
    secondary: list[InlineKeyboardButton] = []

    renew = (
        InlineKeyboardButton(text="🛒 خرید اشتراک", callback_data=MenuCallback.BUY)
        if is_trial
        else InlineKeyboardButton(
            # 📅, not the 🔄 it used to carry: «به‌روزرسانی» sits right beside it and
            # two identical icons on one screen make both of them meaningless.
            text="📅 تمدید این سرویس",
            callback_data=AccountCB.svc_renew(account_id),
        )
    )
    howto = InlineKeyboardButton(
        text="📖 راهنمای اتصال",
        callback_data=AccountCB.svc_howto(account_id),
    )
    refresh = InlineKeyboardButton(
        text="🔄 به‌روزرسانی",
        callback_data=AccountCB.svc_refresh(account_id),
    )

    if show_renew:
        primary = renew
        primary.style = styled(PRIMARY)
        if has_link:
            secondary.append(howto)
    elif has_link:
        primary = howto
        primary.style = styled(PRIMARY)
        secondary.append(renew)
    else:
        # Nothing to connect to — refreshing is the accent, renew still sits below.
        primary = refresh
        secondary.append(renew)

    if primary is not refresh:
        secondary.append(refresh)
    if has_link:
        secondary.append(
            InlineKeyboardButton(
                text="🔐 تغییر لینک",
                callback_data=AccountCB.svc_revoke(account_id),
            )
        )
    if can_rename:
        # A shop's label is their only handle on a service. Services made
        # before they were a reseller — or before the bot asked — carry none,
        # and «later» never comes without a button.
        secondary.append(
            InlineKeyboardButton(
                text="✏️ اسم/شمارهٔ مشتری",
                callback_data=f"{AccountCB.RS_RENAME}{account_id}",
            )
        )

    rows: list[list[InlineKeyboardButton]] = [[primary]]
    rows.extend(secondary[i : i + 2] for i in range(0, len(secondary), 2))
    if not is_trial:  # a trial's buy button already sits where renew would
        rows.append([InlineKeyboardButton(text="🛒 خرید اشتراک", callback_data=MenuCallback.BUY)])
    # Back on its own row, below everything: «تغییر لینک» is one tap from a
    # confirm screen, and a thumb reaching for «بازگشت» must not land on it.
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت به سرویس‌ها", callback_data=AccountCB.LIST)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def howto_platform_keyboard(account_id: int) -> InlineKeyboardMarkup:
    """OS picker for connection howto (do not guess OS from Telegram)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🤖 Android",
                    callback_data=AccountCB.svc_howto(account_id, "android"),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🍎 iOS",
                    callback_data=AccountCB.svc_howto(account_id, "ios"),
                )
            ],
            [
                InlineKeyboardButton(
                    text="💻 Windows",
                    callback_data=AccountCB.svc_howto(account_id, "windows"),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🖥️ macOS",
                    callback_data=AccountCB.svc_howto(account_id, "macos"),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ بازگشت به سرویس",
                    callback_data=AccountCB.svc(account_id),
                )
            ],
        ]
    )


def service_revoke_confirm_keyboard(account_id: int) -> InlineKeyboardMarkup:
    """Danger confirm for subscription link revoke / regenerate."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚠️ بله، لینک جدید بساز",
                    callback_data=AccountCB.svc_revoke_confirm(account_id),
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ انصراف",
                    callback_data=AccountCB.svc(account_id),
                )
            ],
        ]
    )


def account_keyboard(*, has_link: bool) -> InlineKeyboardMarkup:
    """Legacy single-account keyboard (kept for compatibility)."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="🔄 به‌روزرسانی", callback_data=AccountCB.REFRESH),
        ]
    ]
    if has_link:
        rows.append(
            [
                InlineKeyboardButton(text="🔗 نمایش لینک", callback_data=AccountCB.LINK),
                InlineKeyboardButton(text="📱 QR کد", callback_data=AccountCB.QR),
            ]
        )
    rows.append([InlineKeyboardButton(text="🛒 خرید اشتراک", callback_data=MenuCallback.BUY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت به منو", callback_data=AccountCB.BACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_empty_keyboard(*, trial_eligible: bool = False) -> InlineKeyboardMarkup:
    """Inviting empty-state CTAs for «سرویس‌های من» (buy + optional trial)."""
    rows: list[list[InlineKeyboardButton]] = []
    if trial_eligible:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🎟 دریافت تست رایگان",
                    callback_data=ReferralCB.TRIAL,
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🛒 اولین اشتراک را بخر",
                callback_data=MenuCallback.BUY,
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت به منو", callback_data=AccountCB.BACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_orders_keyboard() -> InlineKeyboardMarkup:
    """Kept for leftover inline messages; customer service screens no longer open this."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ بازگشت به سرویس‌ها", callback_data=AccountCB.LIST)],
        ]
    )

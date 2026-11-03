"""Admin panel keyboards (Telegram inline — not a web UI)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from bot.keyboards.admin_payment import PayCB
from bot.keyboards.lifecycle import LifeCB
from bot.keyboards.marketing import MktCB
from bot.keyboards.nav import home_button, nav_rows
from bot.keyboards.purchase import encode_duration_cb
from bot.keyboards.style import DANGER, PRIMARY, SUCCESS, styled
from bot.texts import texts
from database.models import OrderStatus
from services.reseller_admin import duration_tab_label, tier_tab_label
from services.ux_format import (
    days_until,
    format_jalali,
    format_toman,
    time_left_fa,
    to_persian_digits,
)

PAGE_SIZE = 8

# «🗓 هشدار و حذف سرویس» lives in `bot/handlers/lifecycle_admin.py`.
LIFECYCLE_OPEN_CB = LifeCB.OPEN
MARKETING_OPEN_CB = MktCB.OPEN


class AdminCB:
    HOME = "adm:home"
    # The home's sections (owner, 2026-09-30): each old home button lives in one.
    MIRZA_HISTORY = "adm:mh:"
    SEC_SALES = "adm:hx:sales"
    SEC_PEOPLE = "adm:hx:people"
    SEC_SETTINGS = "adm:hx:set"
    PENDING = "adm:pending"
    ORDERS = "adm:orders"
    ORDERS_FILTER = "adm:of:"  # adm:of:{STATUS|ALL}:{offset}
    LOOKUP = "adm:lookup"
    BAN = "adm:ban"
    UNBAN = "adm:unban"
    STATS = "adm:stats"
    PANEL = "adm:panel"
    PANEL_EDIT = "adm:pset:"  # adm:pset:{field}
    PANEL_TEST = "adm:ptest"
    PANEL_STATUS = "adm:pstat"  # live status screen (also its refresh)
    PANEL_NODE_RECONNECT = "adm:pnrc:"  # + node id
    PANEL_CLEAR = "adm:pclear"  # preview only
    PANEL_CLEAR_OK = "adm:pcok"  # applies the reset (not a PANEL_CLEAR prefix)
    PANEL_GROUPS_LIST = "adm:pglst"  # live list from panel
    ORDER = "adm:ord:"  # adm:ord:{order_id}[:{src}:{key}:{offset}]
    SHOW_RECEIPT = "adm:rcpt:"  # adm:rcpt:{order_id} — re-send receipt photo
    RETRY = "adm:retry:"  # adm:retry:{order_id}
    BAN_FROM_ORDER = "adm:banord:"  # adm:banord:{user_id}
    UNBAN_FROM_ORDER = "adm:unbanord:"  # adm:unbanord:{user_id}
    CANCEL_INPUT = "adm:cancel"
    NEXT_PENDING = "adm:nextpend"  # jump to next PAYMENT_REVIEW
    PENDING_FILTER = "adm:pf:"
    # Customer desk — search by anything the customer can tell you.
    CUSTOMER = "adm:cust"  # opens the browse list
    CUSTOMER_SEARCH = "adm:cusq"  # the search prompt, reached from inside the list
    CUSTOMER_PAGE = "adm:cup:"  # + {filter}:{offset}
    CUSTOMER_FILTER = "adm:cuf:"  # + filter value
    CUSTOMER_PICK = "adm:cu:"  # + user id
    WALLET_ADD = "adm:wa:"  # + user id — prompt an amount
    WALLET_OK = "adm:wok:"  # + user id — apply the pending amount
    LATE_CREDIT = "adm:lrc:"  # + order id — late receipt: open the wallet confirm, pre-filled
    LATE_IGNORE = "adm:lri:"  # + order id — late receipt: dismiss the message
    WALLET_ZERO = "adm:wz:"  # + user id — preview zeroing the wallet
    WALLET_ZERO_OK = "adm:wzok:"  # + user id — apply it
    CUST_DM = "adm:cdm:"  # + user id — write to this customer
    CUST_ORDERS = "adm:cord:"  # + user id[:offset] — their orders, as buttons
    CUST_SERVICES = "adm:csvc:"  # + user id — their services, as buttons
    CUST_REFS = "adm:cref:"  # + user id — who they brought in
    CUST_TRIAL_RESET = "adm:ctr:"  # + user id — let them take a trial again
    CUST_RESELLER = "adm:crs:"  # + user id — grant the role, or ask before revoking it
    CUST_RESELLER_OK = "adm:crsok:"  # + user id — the confirmed revoke
    CUST_BAN = "adm:cbn:"  # + user id — ask: ban / unban, and the services too?
    CUST_BAN_OK = "adm:cbk:"  # + user id:{b|s|ub|us} — apply it
    # «🎁 ساخت سرویس»: group → plan → name (typed) → confirm.
    CUST_LINK = "adm:clink:"
    CUST_LINK_GO = "adm:clgo:"
    CUST_GIFT = "adm:cgf:"  # + user id — pick a group
    CUST_GIFT_GROUP = "adm:cgg:"  # + user id:category id — pick a plan
    CUST_GIFT_PLAN = "adm:cgp:"  # + user id:package id — ask for the name
    CUST_GIFT_GO = "adm:cgo:"  # + user id:{g|w}:{8-hex request key} — create it
    RESELLERS = "adm:rslist"  # browse list of reseller shops
    RS_PANEL = "adm:rsp:"  # + user id — that reseller's price list
    RS_RATE = "adm:rsr:"  # + user id:{gb|day|quota|tprice} — edit one number
    RS_PLAN = "adm:rspl:"  # + user id:slug — set the price of one plan
    RS_PLAN_CLEAR = "adm:rspc:"  # + user id:slug — back to retail
    RS_SEC = "adm:rssec:"  # + user id:{custom|trial|plans}
    RS_DUR = "adm:rs:d:"  # + duration token (n or days) — tab duration
    RS_TIER = "adm:rs:t:"  # + economy|premium
    RS_CELL = "adm:rs:c:"  # + package slug
    RS_PCT = "adm:rs:pct"  # start percent for visible tab
    RS_PCT_OK = "adm:rs:pctok"
    RS_PCT_NO = "adm:rs:pctno"
    RS_UNDO = "adm:rs:undo"
    RS_CAT = "adm:rs:cat:"  # + category id — which product group matrix

    NOOP = "adm:noop"  # a header row that answers instead of spinning
    # Live service operations (PasarGuard modify_user) — + account id
    SVC = "adm:sv:"
    SVC_EXTEND = "adm:sve:"
    SVC_DATA = "adm:svd:"
    SVC_DATA_OK = "adm:svdok:"  # + account id — the confirmed «افزودن حجم»
    SVC_TOGGLE = "adm:svt:"
    SVC_TOGGLE_OK = "adm:svtok:"
    SVC_DEVICES = "adm:svh:"  # devices registered on the service
    SVC_DEV_RESET = "adm:svhr:"  # ask: forget them all?
    SVC_DEV_RESET_OK = "adm:svhk:"  # apply
    SVC_LINK = "adm:svl:"  # link + QR screen
    SVC_LINK_SEND = "adm:svls:"  # send the card to the customer
    SVC_LINK_QR = "adm:svlq:"  # QR to the admin
    SVC_LINK_NEW = "adm:svln:"  # ask: rotate the link?
    SVC_LINK_NEW_OK = "adm:svlk:"  # apply
    # Report group
    REPORTS = "adm:rep"
    REP_ATTACH = "adm:rep:att"
    REP_TOPIC = "adm:rep:t:"  # + topic key
    REP_VERIFY = "adm:rep:v:"  # + topic key
    REP_TOGGLE = "adm:rep:on"
    # Broadcast
    CAST = "adm:bc"
    QR_BG = "adm:qrbg"  # service QR background: preview + upload
    QR_BG_RESET = "adm:qrbg:rs"  # ask: back to the template?
    QR_BG_RESET_OK = "adm:qrbg:rsok"  # apply
    CAST_NEW = "adm:bc:new"
    CAST_AUD = "adm:bc:a:"  # + audience value
    CAST_SEND = "adm:bc:go"
    CAST_TEST = "adm:bc:me"  # the composed message, to the tapping admin only
    CAST_STATUS = "adm:bc:st:"  # + broadcast id
    CAST_CANCEL = "adm:bc:x:"  # + broadcast id  # adm:pf:{all|stale}:{offset}
    # Outage announcement (rides the broadcast pipeline)
    ANN = "adm:an"
    ANN_OUTAGE = "adm:an:out"
    ANN_RESTORE = "adm:an:back"
    ANN_NOW = "adm:an:now"
    ANN_SKIP = "adm:an:skip"
    STORE = "adm:store"  # مدیریت فروشگاه (STORE_ADMIN hub)


_STATUS_FILTERS: list[tuple[str, str | None]] = [
    ("📋 همه", None),
    ("🔎 بررسی پرداخت", OrderStatus.PAYMENT_REVIEW.value),
    ("✅ فعال", OrderStatus.ACTIVE.value),
    ("⚠️ ناموفق پنل", OrderStatus.PROVISION_FAILED.value),
    ("❌ رد شده", OrderStatus.REJECTED.value),
    ("⏳ در انتظار پرداخت", OrderStatus.PENDING_PAYMENT.value),
]


def admin_home_keyboard(*, pending_count: int = 0, web_panel_url: str = "") -> InlineKeyboardMarkup:
    """Payments first, then four sections and the broadcast.

    The home was a wall of eleven buttons. Payments to approve are the one
    thing that cannot wait, so that button is alone on top and coloured only
    when something is waiting; the rest sit one tap down, each in one section.

    ``web_panel_url`` (``settings.web_panel_link``, empty while the panel is off)
    adds «🌐 پنل وب», a Mini App button beside «⚙️ تنظیمات». It is never coloured:
    the one accent on this screen belongs to the payments button.
    """
    if pending_count > 0:
        pending_label = f"💳 پرداخت‌ها ({to_persian_digits(pending_count)})"
    else:
        pending_label = "💳 پرداخت‌ها"
    settings_row = [InlineKeyboardButton(text="⚙️ تنظیمات", callback_data=AdminCB.SEC_SETTINGS)]
    if web_panel_url:
        settings_row.append(
            InlineKeyboardButton(text="🌐 پنل وب", web_app=WebAppInfo(url=web_panel_url))
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=pending_label,
                    callback_data=AdminCB.PENDING,
                    style=styled(PRIMARY) if pending_count > 0 else None,
                )
            ],
            [
                InlineKeyboardButton(text="💰 فروش و سفارش", callback_data=AdminCB.SEC_SALES),
                InlineKeyboardButton(text="👥 مشتری‌ها", callback_data=AdminCB.SEC_PEOPLE),
            ],
            [
                InlineKeyboardButton(text="🛍 فروشگاه", callback_data=AdminCB.STORE),
                InlineKeyboardButton(text="📢 پیام همگانی", callback_data=AdminCB.CAST),
            ],
            settings_row,
        ]
    )


def admin_sales_keyboard() -> InlineKeyboardMarkup:
    """💰 فروش و سفارش — orders, order search, finance."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📦 سفارش‌ها", callback_data=AdminCB.ORDERS),
                InlineKeyboardButton(text="🔎 جستجوی سفارش", callback_data=AdminCB.LOOKUP),
            ],
            [InlineKeyboardButton(text="💰 گزارش مالی", callback_data=AdminCB.STATS)],
            [
                InlineKeyboardButton(
                    text="🗃 بررسی میرزا", callback_data=f"{AdminCB.MIRZA_HISTORY}all:0:1"
                )
            ],
            *nav_rows(home=True),
        ]
    )


def admin_people_keyboard() -> InlineKeyboardMarkup:
    """👥 مشتری‌ها — customers, resellers, and ban by id."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👥 کاربران", callback_data=AdminCB.CUSTOMER),
                InlineKeyboardButton(text="🏪 نمایندگان", callback_data=AdminCB.RESELLERS),
            ],
            [
                InlineKeyboardButton(
                    text="🚫 مسدود کردن", style=styled(DANGER), callback_data=AdminCB.BAN
                ),
                InlineKeyboardButton(text="✅ رفع مسدودی", callback_data=AdminCB.UNBAN),
            ],
            *nav_rows(home=True),
        ]
    )


def admin_settings_keyboard() -> InlineKeyboardMarkup:
    """⚙️ تنظیمات — panel connection, report group, QR background."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔌 اتصال پنل", callback_data=AdminCB.PANEL),
                InlineKeyboardButton(text="📡 گروه گزارش", callback_data=AdminCB.REPORTS),
            ],
            [InlineKeyboardButton(text="🎨 پس‌زمینهٔ QR", callback_data=AdminCB.QR_BG)],
            [InlineKeyboardButton(text="🗓 هشدار و حذف سرویس", callback_data=LIFECYCLE_OPEN_CB)],
            [InlineKeyboardButton(text="📣 پیام‌های بازاریابی", callback_data=MARKETING_OPEN_CB)],
            *nav_rows(home=True),
        ]
    )


def qr_background_keyboard(*, custom: bool) -> InlineKeyboardMarkup:
    """Upload happens by sending a photo; the one button is the way back."""
    rows: list[list[InlineKeyboardButton]] = []
    if custom:
        rows.append(
            [
                InlineKeyboardButton(
                    text="♻️ برگشت به قالب پیش‌فرض", callback_data=AdminCB.QR_BG_RESET
                )
            ]
        )
    rows.append([home_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def qr_background_reset_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="♻️ بله، قالب پیش‌فرض",
                    style=styled(DANGER),
                    callback_data=AdminCB.QR_BG_RESET_OK,
                )
            ],
            [InlineKeyboardButton(text="⬅️ نه، برگرد", callback_data=AdminCB.QR_BG)],
        ]
    )


def panel_settings_keyboard(can_edit: bool = True) -> InlineKeyboardMarkup:
    """The panel screen. Writes are the owner's; every admin may look and test.

    «📊 وضعیت پنل» is the screen's one PRIMARY, alone on the first row, for every admin.
    """

    def edit(label: str, field: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=label, callback_data=f"{AdminCB.PANEL_EDIT}{field}")

    groups = InlineKeyboardButton(
        text="📋 لیست گروه‌های پنل", callback_data=AdminCB.PANEL_GROUPS_LIST
    )
    test = InlineKeyboardButton(text="🔌 تست اتصال", callback_data=AdminCB.PANEL_TEST)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="📊 وضعیت پنل",
                callback_data=AdminCB.PANEL_STATUS,
                style=styled(PRIMARY),
            )
        ]
    ]
    if can_edit:
        rows += [
            [edit("🌐 آدرس پایه", "base_url"), edit("🔑 تنظیم API Key", "api_key")],
            [edit("🔗 مسیر ساب", "sub_path"), edit("⏱ تایم‌اوت", "timeout")],
            # A trial's size and length are set per store group; the device
            # count is the one trial setting that stays here.
            [edit("🧪 دستگاه تست", "trial_hwid")],
        ]
    rows += [[groups, test]]
    if can_edit:
        rows.append(
            [
                InlineKeyboardButton(
                    text="♻️ بازگشت به پیش‌فرض .env",
                    callback_data=AdminCB.PANEL_CLEAR,
                    style=styled(DANGER),
                )
            ]
        )
    rows += nav_rows()
    return InlineKeyboardMarkup(inline_keyboard=rows)


def panel_status_keyboard(reconnectable: Sequence[Any] = ()) -> InlineKeyboardMarkup:
    """Status screen: a reconnect button per node that can use one, refresh, back, home.

    Nothing here is PRIMARY: the screen is read-only and its action is the refresh.
    """
    rows = [
        [
            InlineKeyboardButton(
                text=n.button_label, callback_data=f"{AdminCB.PANEL_NODE_RECONNECT}{int(n.id)}"
            )
        ]
        for n in reconnectable
    ]
    rows.append([InlineKeyboardButton(text="🔄 به‌روزرسانی", callback_data=AdminCB.PANEL_STATUS)])
    rows += nav_rows(back_to=AdminCB.PANEL, back_label="پنل")
    return InlineKeyboardMarkup(inline_keyboard=rows)


def panel_cancel_keyboard() -> InlineKeyboardMarkup:
    """Cancel a panel prompt back to the panel screen, not all the way home."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ لغو", callback_data=AdminCB.PANEL)]]
    )


def panel_groups_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=nav_rows(back_to=AdminCB.PANEL, back_label="پنل"))


def admin_cancel_input_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ لغو", callback_data=AdminCB.CANCEL_INPUT)],
        ]
    )


def admin_back_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [home_button()],
        ]
    )


def admin_resellers_keyboard(
    users: list[Any], stats: dict[int, tuple[int, int, int]] | None = None
) -> InlineKeyboardMarkup:
    """One button per shop: «name • n فروش • تست used/quota», this Jalali month.

    ``stats`` maps user id to ``(sales, trials used, trial quota)``. The trial
    part is left out for a shop with no quota and no trials — «تست ۰/۰» would
    read as a limit it does not have.
    """
    stats = stats or {}
    rows: list[list[InlineKeyboardButton]] = []
    for user in users:
        label = str(
            getattr(user, "first_name", None)
            or getattr(user, "username", None)
            or str(getattr(user, "telegram_id", user.id))
        )
        if len(label) > 20:
            label = label[:19] + "…"
        if user.id in stats:
            sales, used, quota = stats[user.id]
            label += f" • {to_persian_digits(sales)} فروش"
            if quota or used:
                label += f" • تست {to_persian_digits(used)}/{to_persian_digits(quota)}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"{AdminCB.RS_PANEL}{user.id}",
                )
            ]
        )
    rows.extend(nav_rows(back_to=AdminCB.SEC_PEOPLE, back_label="مشتری‌ها", home=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _pager_row(prefix: str, offset: int, limit: int, total: int) -> list[InlineKeyboardButton]:
    row: list[InlineKeyboardButton] = []
    if offset > 0:
        prev_off = max(0, offset - limit)
        row.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"{prefix}{prev_off}"))
    if offset + limit < total:
        next_off = offset + limit
        row.append(InlineKeyboardButton(text="بعدی ➡️", callback_data=f"{prefix}{next_off}"))
    return row


def _pending_button_label(order: Any) -> str:
    """Badge 📷 when receipt exists; keep under Telegram's ~64-char button limit."""
    payment = getattr(order, "payment", None)
    has_receipt = bool(getattr(payment, "receipt_file_id", None)) if payment is not None else False
    badge = " 📷" if has_receipt else ""
    label = f"#{order.id} — {order.price_toman:,} ت{badge}"
    if len(label) > 64:
        label = label[:61] + "…"
    return label


SRC_PENDING = "p"
SRC_ORDERS = "o"
SRC_CUSTOMER = "c"  # key = user id: back goes to that customer's order list


def order_cb(order_id: int, *, src: str, key: str, offset: int) -> str:
    """Order row callback carrying the list it was opened from.

    Reviewing a queue used to cost three taps per order because the detail
    screen only offered «🏠 خانه ادمین»; the filter and page were lost each time.
    """
    return f"{AdminCB.ORDER}{order_id}:{src}:{key}:{offset}"


def order_back_cb(src: str, key: str, offset: int) -> str:
    """Rebuild the list callback an order row was opened from."""
    if src == SRC_PENDING:
        return f"{AdminCB.PENDING_FILTER}{key}:{offset}"
    if src == SRC_CUSTOMER:
        return f"{AdminCB.CUST_ORDERS}{key}:{offset}"
    return f"{AdminCB.ORDERS_FILTER}{key}:{offset}"


def parse_order_cb(data: str) -> tuple[int | None, str | None]:
    """``adm:ord:<id>[:<src>:<key>:<offset>]`` → (order_id, back_callback)."""
    rest = data[len(AdminCB.ORDER) :] if data.startswith(AdminCB.ORDER) else data
    parts = rest.split(":")
    if not parts or not parts[0].isdigit():
        return None, None
    order_id = int(parts[0])
    if len(parts) == 4 and parts[3].isdigit():
        return order_id, order_back_cb(parts[1], parts[2], int(parts[3]))
    return order_id, None


def pending_list_keyboard(
    orders: list,
    *,
    offset: int,
    limit: int,
    total: int,
    filter_key: str = "all",
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    # Filter row first
    all_mark = "✅ " if filter_key == "all" else ""
    stale_mark = "✅ " if filter_key == "stale" else ""
    rows.append(
        [
            InlineKeyboardButton(
                text=f"{all_mark}📋 همه",
                callback_data=f"{AdminCB.PENDING_FILTER}all:0",
            ),
            InlineKeyboardButton(
                text=f"{stale_mark}⏰ کهنه (>۱س)",
                callback_data=f"{AdminCB.PENDING_FILTER}stale:0",
            ),
        ]
    )
    for order in orders:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_pending_button_label(order),
                    callback_data=order_cb(
                        order.id, src=SRC_PENDING, key=filter_key, offset=offset
                    ),
                )
            ]
        )
    pager_prefix = f"{AdminCB.PENDING_FILTER}{filter_key}:"
    pager = _pager_row(pager_prefix, offset, limit, total)
    if pager:
        rows.append(pager)
    rows.extend(nav_rows())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def orders_filter_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for label, status in _STATUS_FILTERS:
        key = status or "ALL"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"{AdminCB.ORDERS_FILTER}{key}:0",
                )
            ]
        )
    rows.extend(nav_rows())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def order_status_fa(status: str) -> str:
    """The order status as the list text above the buttons says it."""
    return texts.account_status_labels[status]


def orders_list_keyboard(
    orders: list,
    *,
    status_key: str,
    offset: int,
    limit: int,
    total: int,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for order in orders:
        st = order.status.value if hasattr(order.status, "value") else str(order.status)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"#{order.id} | {order_status_fa(st)}",
                    callback_data=order_cb(order.id, src=SRC_ORDERS, key=status_key, offset=offset),
                )
            ]
        )
    pager = _pager_row(f"{AdminCB.ORDERS_FILTER}{status_key}:", offset, limit, total)
    if pager:
        rows.append(pager)
    rows.append(
        [
            InlineKeyboardButton(text="🧰 فیلترها", callback_data=AdminCB.ORDERS),
            home_button(),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def order_detail_keyboard(order: Any, *, back_cb: str | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    status = order.status
    if status == OrderStatus.PAYMENT_REVIEW:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ تایید",
                    style=styled(SUCCESS),
                    callback_data=f"{PayCB.APPROVE_PREFIX}{order.id}",
                ),
                InlineKeyboardButton(
                    text="❌ رد",
                    style=styled(DANGER),
                    callback_data=f"{PayCB.REJECT_PREFIX}{order.id}",
                ),
            ]
        )
    payment = getattr(order, "payment", None)
    receipt_id = getattr(payment, "receipt_file_id", None) if payment is not None else None
    if receipt_id:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🖼 نمایش مجدد فیش",
                    callback_data=f"{AdminCB.SHOW_RECEIPT}{order.id}",
                )
            ]
        )
    if status == OrderStatus.PROVISION_FAILED:
        rows.append(
            [
                InlineKeyboardButton(
                    text="تلاش مجدد فعال‌سازی",
                    callback_data=f"{AdminCB.RETRY}{order.id}",
                )
            ]
        )
    user = order.user
    if user is not None:
        if user.is_banned:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="رفع مسدودی کاربر سفارش",
                        callback_data=f"{AdminCB.UNBAN_FROM_ORDER}{user.id}",
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="مسدود کردن کاربر سفارش",
                        callback_data=f"{AdminCB.BAN_FROM_ORDER}{user.id}",
                    )
                ]
            )
    rows.extend(nav_rows(back_to=back_cb, back_label="فهرست"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_keyboard(
    *,
    confirm_cb: str,
    confirm_label: str,
    back_to: str,
    back_label: str,
) -> InlineKeyboardMarkup:
    """Two-step guard for a destructive action.

    Rejecting a payment already cost two or three deliberate taps while wiping
    the panel settings cost one; this makes the risky side of the panel match.
    """
    rows = [[InlineKeyboardButton(text=confirm_label, callback_data=confirm_cb)]]
    rows.extend(nav_rows(back_to=back_to, back_label=back_label))
    return InlineKeyboardMarkup(inline_keyboard=rows)


LABEL_MAX = 40  # what a phone shows uncut
_NAME_MAX = 18


def _trim(text: str, room: int) -> str:
    """Cut the name part only, with an ellipsis; numbers are never cut."""
    room = max(1, room)
    return text if len(text) <= room else text[: room - 1] + "…"


def _short_toman(amount: int) -> str:
    """«۱۲ هزار» when it divides by a thousand, else the full «۱۲٬۳۴۵»."""
    if amount % 1000 == 0:
        return f"{to_persian_digits(amount // 1000)} هزار"
    return to_persian_digits(f"{amount:,}".replace(",", "٬"))


def user_row_label(row: Any) -> str:
    """«🚫رضا • 🏦 ۱۲ هزار • 📱 ۲», at most 40 characters.

    The old label put the wallet as a bare number after the name, and a phone
    cut the name. Wallet and service count appear only when they are not zero.
    """
    marks = "".join(("🚫" if row.is_banned else "", "⚙️" if row.is_admin else ""))
    suffix = ""
    if int(row.wallet_balance_toman or 0) > 0:
        suffix += f" • 🏦 {_short_toman(int(row.wallet_balance_toman))}"
    if int(row.service_count or 0) > 0:
        suffix += f" • 📱 {to_persian_digits(int(row.service_count))}"
    name = _trim(str(row.label), min(_NAME_MAX, LABEL_MAX - len(marks) - len(suffix)))
    return f"{marks}{name}{suffix}"


def users_list_keyboard(page: Any, *, filters: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """The browse list: one row per user, then search and paging.

    Offset rides inside the callback (the same trick `parse_order_cb` uses for
    orders) so «بازگشت» from a customer card returns to the page the admin was
    on instead of dumping them back at the top.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for row in page.rows:
        rows.append(
            [
                InlineKeyboardButton(
                    text=user_row_label(row),
                    callback_data=f"{AdminCB.CUSTOMER_PICK}{row.user_id}",
                )
            ]
        )

    current = getattr(page.user_filter, "value", str(page.user_filter))
    pager: list[InlineKeyboardButton] = []
    if page.has_prev:
        pager.append(
            InlineKeyboardButton(
                text="⬅️ قبلی",
                callback_data=f"{AdminCB.CUSTOMER_PAGE}{current}:{page.prev_offset}",
            )
        )
    if page.has_next:
        pager.append(
            InlineKeyboardButton(
                text="بعدی ➡️",
                callback_data=f"{AdminCB.CUSTOMER_PAGE}{current}:{page.next_offset}",
            )
        )
    if pager:
        rows.append(pager)

    # Filters, two per row, with the active one marked. Paging carries the
    # filter inside the callback so «بعدی» never silently drops it.
    filter_buttons = [
        InlineKeyboardButton(
            text=f"{'● ' if value == current else ''}{label}",
            callback_data=f"{AdminCB.CUSTOMER_FILTER}{value}",
        )
        for value, label in filters
    ]
    for i in range(0, len(filter_buttons), 2):
        rows.append(filter_buttons[i : i + 2])

    rows.append(
        [InlineKeyboardButton(text="🔎 جستجو با آیدی", callback_data=AdminCB.CUSTOMER_SEARCH)]
    )
    rows.extend(nav_rows(home=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def customer_results_keyboard(users: list[Any]) -> InlineKeyboardMarkup:
    """Search hits. A support message rarely carries an exact handle, so the
    admin picks from a short list rather than retyping."""
    rows: list[list[InlineKeyboardButton]] = []
    for user in users:
        name = user.first_name or user.username or str(user.telegram_id)
        handle = f"@{user.username}" if user.username else str(user.telegram_id)
        label = f"{name} • {handle}"
        if len(label) > 55:
            label = label[:52] + "…"
        rows.append(
            [InlineKeyboardButton(text=label, callback_data=f"{AdminCB.CUSTOMER_PICK}{user.id}")]
        )
    rows.append(
        [InlineKeyboardButton(text="🔎 جستجوی دیگر", callback_data=AdminCB.CUSTOMER_SEARCH)]
    )
    rows.extend(nav_rows(home=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def customer_card_keyboard(
    user_id: int,
    *,
    banned: bool,
    services: list[Any] | None = None,
    trial_taken: bool = False,
    reseller: bool = False,
    wallet_toman: int = 0,
    orders_total: int = 0,
    legacy_records_total: int = 0,
    referrer_id: int | None = None,
) -> InlineKeyboardMarkup:
    """Actions on one customer: services, then everyday actions, then switches.

    Deliberately unlike the previous bot's flat wall of ~25 buttons, half of
    which were opposite pairs («مسدود کردن» beside «رفع مسدودی»). A switch is
    one button whose mark and words say what the tap does («🚫 مسدود کردن»,
    «✅ رفع مسدودی»; the owner asked for the marks back on 2026-10-03),
    and a button shows only when it would do something now: the trial reset
    once they took one, the reseller prices once they hold the role, zeroing
    once there is a balance. One screen, no sections — the owner opens it
    often and a section would cost a tap on every action (2026-09-30).
    """
    ban_cb = AdminCB.CUST_BAN  # asks first; the confirm screen knows which way it goes
    ban_label = "✅ رفع مسدودی" if banned else "🚫 مسدود کردن"
    reseller_label = "🏪 حذف نمایندگی" if reseller else "🏪 نماینده کردن"
    # Services and orders are one button each, opening a list of buttons: a
    # row per service on the card pushed every action off a phone screen for
    # anyone with a few services (owner, 2026-09-30).
    rows: list[list[InlineKeyboardButton]] = []
    history: list[InlineKeyboardButton] = []
    if services:
        history.append(
            InlineKeyboardButton(
                text=f"📱 سرویس‌ها ({to_persian_digits(len(services))})",
                callback_data=f"{AdminCB.CUST_SERVICES}{user_id}",
            )
        )
    if orders_total:
        history.append(
            InlineKeyboardButton(
                text=f"🛍 سفارش‌ها ({to_persian_digits(orders_total)})",
                callback_data=f"{AdminCB.CUST_ORDERS}{user_id}",
            )
        )
    if history:
        rows.append(history)
    if legacy_records_total:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🗃 سوابق میرزا ({to_persian_digits(legacy_records_total)})",
                    callback_data=f"{AdminCB.MIRZA_HISTORY}{user_id}:0:0",
                )
            ]
        )
    rows += [
        [
            InlineKeyboardButton(text="🏦 کیف پول", callback_data=f"{AdminCB.WALLET_ADD}{user_id}"),
            InlineKeyboardButton(text="✍️ پیام", callback_data=f"{AdminCB.CUST_DM}{user_id}"),
        ],
    ]
    referral_row = [
        InlineKeyboardButton(text="🎁 زیرمجموعه‌ها", callback_data=f"{AdminCB.CUST_REFS}{user_id}")
    ]
    if referrer_id:
        referral_row.append(
            InlineKeyboardButton(
                text="👤 معرف", callback_data=f"{AdminCB.CUSTOMER_PICK}{referrer_id}"
            )
        )
    rows.append(referral_row)
    rows.append(
        [InlineKeyboardButton(text="🎁 ساخت سرویس", callback_data=f"{AdminCB.CUST_GIFT}{user_id}")]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔗 اتصال سرویس موجود", callback_data=f"{AdminCB.CUST_LINK}{user_id}"
            )
        ]
    )
    if trial_taken:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🧪 اجازهٔ تست دوباره",
                    callback_data=f"{AdminCB.CUST_TRIAL_RESET}{user_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=reseller_label, callback_data=f"{AdminCB.CUST_RESELLER}{user_id}"
            )
        ]
    )
    if reseller:
        rows.append(
            [
                InlineKeyboardButton(
                    text="💵 قیمت‌های نمایندگی",
                    callback_data=f"{AdminCB.RS_PANEL}{user_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text=ban_label, callback_data=f"{ban_cb}{user_id}")])
    if wallet_toman != 0:
        rows.append(
            [
                InlineKeyboardButton(
                    text="0️⃣ صفر کردن کیف پول",
                    style=styled(DANGER),
                    callback_data=f"{AdminCB.WALLET_ZERO}{user_id}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="🔎 جستجوی دیگر", callback_data=AdminCB.CUSTOMER_SEARCH)]
    )
    rows.extend(nav_rows(home=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def customer_ban_confirm_keyboard(
    user_id: int, *, unban: bool, services_count: int
) -> InlineKeyboardMarkup:
    """Ban or unban, asked every time: «فقط ربات» or «ربات + سرویس‌ها».

    The second choice is offered only when the customer has services — with
    none it would be the first choice under another name.
    """
    n = to_persian_digits(services_count)
    choices: list[tuple[str, str, Any]]
    if unban:
        choices = [("رفع مسدودی", "ub", None)]
        if services_count:
            choices.append((f"رفع مسدودی + وصل سرویس‌ها ({n})", "us", None))
    else:
        choices = [("مسدود کردن (فقط ربات)", "b", DANGER)]
        if services_count:
            choices.append((f"مسدود + قطع سرویس‌ها ({n})", "s", DANGER))
    rows = [
        [
            InlineKeyboardButton(
                text=label,
                style=styled(colour),
                callback_data=f"{AdminCB.CUST_BAN_OK}{user_id}:{mode}",
            )
        ]
        for label, mode, colour in choices
    ]
    rows.append(
        [InlineKeyboardButton(text="انصراف", callback_data=f"{AdminCB.CUSTOMER_PICK}{user_id}")]
    )
    rows.extend(nav_rows(home=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def gift_groups_keyboard(user_id: int, groups: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Sellable store groups, one per row. ``groups`` is ``(category id, label)``."""
    rows = [
        [
            InlineKeyboardButton(
                text=label[:40], callback_data=f"{AdminCB.CUST_GIFT_GROUP}{user_id}:{cat_id}"
            )
        ]
        for cat_id, label in groups
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ کارت مشتری", callback_data=f"{AdminCB.CUSTOMER_PICK}{user_id}"
            )
        ]
    )
    rows.extend(nav_rows(home=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def gift_plans_keyboard(user_id: int, plans: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Plans of one group. ``plans`` is ``(package id, label)``; the label is
    already built by ``plan_button_label`` (≤ 40 characters)."""
    rows = [
        [
            InlineKeyboardButton(
                text=label, callback_data=f"{AdminCB.CUST_GIFT_PLAN}{user_id}:{pkg_id}"
            )
        ]
        for pkg_id, label in plans
    ]
    rows.append(
        [InlineKeyboardButton(text="⬅️ گروه‌ها", callback_data=f"{AdminCB.CUST_GIFT}{user_id}")]
    )
    rows.extend(nav_rows(home=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def gift_confirm_keyboard(
    user_id: int, *, request_key: str, wallet_label: str | None
) -> InlineKeyboardMarkup:
    """Gift, or the customer's wallet when it would go through.

    The key is minted once when this screen is drawn and rides in both buttons:
    a second tap is the same request, never a second service.
    """
    rows = [
        [
            InlineKeyboardButton(
                text="🎁 رایگان (هدیه)",
                style=styled(PRIMARY),
                callback_data=f"{AdminCB.CUST_GIFT_GO}{user_id}:g:{request_key}",
            )
        ]
    ]
    if wallet_label:
        rows.append(
            [
                InlineKeyboardButton(
                    text=wallet_label[:40],
                    callback_data=f"{AdminCB.CUST_GIFT_GO}{user_id}:w:{request_key}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="انصراف", callback_data=f"{AdminCB.CUSTOMER_PICK}{user_id}")]
    )
    rows.extend(nav_rows(home=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def gift_retry_keyboard(user_id: int, go_data: str) -> InlineKeyboardMarkup:
    """After a failed create: the **same** confirm callback, so the retry resumes
    the same order instead of starting another."""
    rows = [[InlineKeyboardButton(text="🔁 دوباره تلاش", callback_data=go_data)]]
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ کارت مشتری", callback_data=f"{AdminCB.CUSTOMER_PICK}{user_id}"
            )
        ]
    )
    rows.extend(nav_rows(home=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def customer_referrals_keyboard(
    user_id: int, people: list[Any], *, offset: int, limit: int, total: int
) -> InlineKeyboardMarkup:
    """Who this customer brought in, as buttons that open each person's card."""
    rows: list[list[InlineKeyboardButton]] = []
    for person in people:
        name = person.first_name or (f"@{person.username}" if person.username else "")
        handle = f"@{person.username}" if person.username else str(person.telegram_id)
        text = f"{_trim(str(name), _NAME_MAX)} • {handle}" if name else handle
        rows.append(
            [
                InlineKeyboardButton(
                    text=_trim(text, LABEL_MAX),
                    callback_data=f"{AdminCB.CUSTOMER_PICK}{person.id}",
                )
            ]
        )
    pager: list[InlineKeyboardButton] = []
    if offset > 0:
        pager.append(
            InlineKeyboardButton(
                text="⬅️ قبلی",
                callback_data=f"{AdminCB.CUST_REFS}{user_id}:{max(0, offset - limit)}",
            )
        )
    if offset + limit < total:
        pager.append(
            InlineKeyboardButton(
                text="بعدی ➡️", callback_data=f"{AdminCB.CUST_REFS}{user_id}:{offset + limit}"
            )
        )
    if pager:
        rows.append(pager)
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ کارت مشتری", callback_data=f"{AdminCB.CUSTOMER_PICK}{user_id}"
            )
        ]
    )
    rows.extend(nav_rows(home=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def customer_order_label(order: Any, status_fa: str) -> str:
    """One order as a button: «#۱۲۳ • فعال • ۱۵۰٬۰۰۰ • ۱۴۰۵/۰۷/۰۱».

    Short enough that a phone never cuts it: id, status, price (no «تومان»,
    the list title says it once), and the date without the time.
    """
    price = int(getattr(order, "price_toman", 0) or 0)
    money = format_toman(price, persian_digits=True) if price > 0 else "رایگان"
    created = getattr(order, "created_at", None)
    date = format_jalali(created, persian_digits=True).split(" ")[0] if created else "—"
    return f"#{to_persian_digits(order.id)} • {status_fa} • {money} • {date}"


def customer_orders_keyboard(
    user_id: int,
    rows: list[tuple[int, str]],
    *,
    offset: int,
    limit: int,
    total: int,
) -> InlineKeyboardMarkup:
    """A customer's orders as buttons; a tap opens the order, «فهرست» comes back here."""
    buttons = [
        [
            InlineKeyboardButton(
                text=label,
                callback_data=order_cb(order_id, src=SRC_CUSTOMER, key=str(user_id), offset=offset),
            )
        ]
        for order_id, label in rows
    ]
    pager: list[InlineKeyboardButton] = []
    if offset > 0:
        pager.append(
            InlineKeyboardButton(
                text="⬅️ قبلی",
                callback_data=f"{AdminCB.CUST_ORDERS}{user_id}:{max(0, offset - limit)}",
            )
        )
    if offset + limit < total:
        pager.append(
            InlineKeyboardButton(
                text="بعدی ➡️", callback_data=f"{AdminCB.CUST_ORDERS}{user_id}:{offset + limit}"
            )
        )
    if pager:
        buttons.append(pager)
    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ کارت مشتری", callback_data=f"{AdminCB.CUSTOMER_PICK}{user_id}"
            )
        ]
    )
    buttons.extend(nav_rows(home=True))
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def service_row_label(name: str, snapshot: Any | None) -> str:
    """One service as a button, with its live state (≤ 40 characters).

    «🟢 name • ۱۲ روز • ۳٫۵ گیگ», «🔴 name • منقضی», «⛔ name • قطع», and
    «⚪️ name» when the panel could not be read — an unknown is not a fault.
    Only the name is trimmed; the numbers are what the row is for.
    """
    if snapshot is None:
        return f"⚪️ {_trim(name, LABEL_MAX - 3)}"
    status = (snapshot.status or "").lower()
    expire = snapshot.expire_at
    days = days_until(expire) if expire is not None else None
    if status == "disabled":
        icon, tail = "⛔", " • قطع"
    elif status == "expired" or (
        days is not None and expire is not None and expire < datetime.now(UTC)
    ):
        icon, tail = "🔴", " • منقضی"
    elif status == "limited":
        icon, tail = "🔴", " • حجم تموم"
    else:
        icon, tail = "🟢", ""
        if expire is not None:
            tail += f" • {time_left_fa(expire)}"
        if not snapshot.is_unlimited_data:
            gb = (snapshot.remaining_bytes or 0) / 1024**3
            tail += (
                f" • {to_persian_digits(f'{gb:.1f}'.rstrip('0').rstrip('.')).replace('.', '٫')} گیگ"
            )
    return f"{icon} {_trim(name, LABEL_MAX - 2 - len(tail))}{tail}"


def customer_services_keyboard(
    user_id: int,
    services: list[Any],
    *,
    snapshots: dict[int, Any] | None = None,
    offset: int = 0,
    limit: int = PAGE_SIZE,
    total: int | None = None,
) -> InlineKeyboardMarkup:
    """One page of a customer's services, each opening its operations.

    ``services`` is the page already sliced; ``snapshots`` maps account id to
    the live panel read (missing or None renders as unknown).
    """
    total = len(services) if total is None else total
    snapshots = snapshots or {}
    rows: list[list[InlineKeyboardButton]] = []
    for svc in services:
        rows.append(
            [
                InlineKeyboardButton(
                    text=service_row_label(str(svc.display_name), snapshots.get(svc.account_id)),
                    callback_data=f"{AdminCB.SVC}{svc.account_id}",
                )
            ]
        )
    pager: list[InlineKeyboardButton] = []
    if offset > 0:
        pager.append(
            InlineKeyboardButton(
                text="⬅️ قبلی",
                callback_data=f"{AdminCB.CUST_SERVICES}{user_id}:{max(0, offset - limit)}",
            )
        )
    if offset + limit < total:
        pager.append(
            InlineKeyboardButton(
                text="بعدی ➡️", callback_data=f"{AdminCB.CUST_SERVICES}{user_id}:{offset + limit}"
            )
        )
    if pager:
        rows.append(pager)
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ کارت مشتری", callback_data=f"{AdminCB.CUSTOMER_PICK}{user_id}"
            )
        ]
    )
    rows.extend(nav_rows(home=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _money_fa(value: int) -> str:
    return to_persian_digits(f"{int(value):,}".replace(",", "٬"))


def reseller_panel_keyboard(
    user_id: int, *, priced_count: int, plan_count: int
) -> InlineKeyboardMarkup:
    """The hub: one entry per thing that has its own price, not one flat wall.

    It started as a single screen holding the rate card and every plan at once.
    The owner's verdict was that it needed «جزییات بیشتر و هر بخش جدا» — and he
    is right that these are three unrelated decisions: what a gigabyte costs,
    what a giveaway costs, and what each shelf plan costs.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🧩 نرخ خرید سفارشی",
                    callback_data=f"{AdminCB.RS_SEC}{user_id}:custom",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🧪 تست نمایندگی",
                    callback_data=f"{AdminCB.RS_SEC}{user_id}:trial",
                )
            ],
            [
                InlineKeyboardButton(
                    text=(
                        f"🎁 پلن‌های آماده "
                        f"({to_persian_digits(priced_count)} از {to_persian_digits(plan_count)})"
                    ),
                    callback_data=f"{AdminCB.RS_SEC}{user_id}:plans",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ کارت مشتری",
                    callback_data=f"{AdminCB.CUSTOMER_PICK}{user_id}",
                )
            ],
            *nav_rows(home=True),
        ]
    )


def reseller_custom_rate_keyboard(user_id: int, *, rate: Any) -> InlineKeyboardMarkup:
    """Toman per gigabyte and per day — the two numbers behind «خرید سفارشی»."""
    gb = int(getattr(rate, "price_per_gb_toman", 0) or 0)
    day = int(getattr(rate, "price_per_day_toman", 0) or 0)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"هر گیگ: {_money_fa(gb)}",
                    callback_data=f"{AdminCB.RS_RATE}{user_id}:gb",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"هر روز: {_money_fa(day)}",
                    callback_data=f"{AdminCB.RS_RATE}{user_id}:day",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ قیمت‌های نمایندگی",
                    callback_data=f"{AdminCB.RS_PANEL}{user_id}",
                )
            ],
            *nav_rows(home=True),
        ]
    )


def reseller_trial_settings_keyboard(user_id: int, *, rate: Any) -> InlineKeyboardMarkup:
    """How many giveaways a month, and what an extra one costs them."""
    quota = int(getattr(rate, "trial_quota_monthly", 0) or 0)
    price = int(getattr(rate, "trial_price_toman", 0) or 0)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"سهمیهٔ ماهانه: {to_persian_digits(quota)}",
                    callback_data=f"{AdminCB.RS_RATE}{user_id}:quota",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"هر تست اضافه: {_money_fa(price)}",
                    callback_data=f"{AdminCB.RS_RATE}{user_id}:tprice",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ قیمت‌های نمایندگی",
                    callback_data=f"{AdminCB.RS_PANEL}{user_id}",
                )
            ],
            *nav_rows(home=True),
        ]
    )


def reseller_plans_keyboard(
    user_id: int,
    *,
    groups: list[tuple[str, list[Any]]],
    priced: dict[str, int],
    retail_by_slug: dict[str, int] | None = None,
) -> InlineKeyboardMarkup:
    """Legacy flat list — kept for any stray caller; prefer reseller_matrix_keyboard."""
    rows: list[list[InlineKeyboardButton]] = []
    for group_title, plans in groups:
        rows.append([InlineKeyboardButton(text=f"— {group_title} —", callback_data=AdminCB.NOOP)])
        for plan in plans:
            agreed = priced.get(plan.slug)
            retail = int((retail_by_slug or {}).get(plan.slug) or 0)
            label = (
                f"{plan.title_fa} — {_money_fa(agreed)}"
                if agreed is not None
                else f"{plan.title_fa} — خرده {_money_fa(retail)}"
            )
            if len(label) > 55:
                label = label[:52] + "…"
            row = [InlineKeyboardButton(text=label, callback_data=f"{AdminCB.RS_CELL}{plan.slug}")]
            if agreed is not None:
                row.append(
                    InlineKeyboardButton(
                        text="↩️",
                        callback_data=f"{AdminCB.RS_PLAN_CLEAR}{user_id}:{plan.slug}",
                    )
                )
            rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ قیمت‌های نمایندگی",
                callback_data=f"{AdminCB.RS_PANEL}{user_id}",
            )
        ]
    )
    rows.extend(nav_rows(home=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reseller_matrix_keyboard(
    *,
    user_id: int,
    packages: list[Any],
    retail_by_slug: dict[str, int],
    agreed_by_slug: dict[str, int],
    durations: list[int | None],
    tiers: list[str],
    selected_duration: int | None,
    selected_tier: str,
    can_undo: bool,
    categories: list[tuple[int, str]] | None = None,
    selected_category_id: int | None = None,
    flat: bool = False,
) -> InlineKeyboardMarkup:
    """One tab of the reseller price matrix — mirrors the customer shelf toggles."""
    rows: list[list[InlineKeyboardButton]] = []

    if categories and len(categories) > 1:
        cat_row: list[InlineKeyboardButton] = []
        for cid, title in categories:
            mark = (
                " ✅"
                if selected_category_id is not None and int(cid) == int(selected_category_id)
                else ""
            )
            label = f"{title}{mark}"
            if len(label) > 28:
                label = label[:25] + "…"
            cat_row.append(InlineKeyboardButton(text=label, callback_data=f"{AdminCB.RS_CAT}{cid}"))
        # Telegram max 8 per row — chunk
        for i in range(0, len(cat_row), 3):
            rows.append(cat_row[i : i + 3])

    dur_row: list[InlineKeyboardButton] = []
    for d in durations:
        label = duration_tab_label(d)
        if d == selected_duration:
            label = f"{label} ✅"
        dur_row.append(
            InlineKeyboardButton(
                text=label,
                callback_data=f"{AdminCB.RS_DUR}{encode_duration_cb(d)}",
            )
        )
    if dur_row:
        rows.append(dur_row)

    if len(tiers) > 1:
        tier_row: list[InlineKeyboardButton] = []
        for tier in tiers:
            label = tier_tab_label(tier)
            if tier == selected_tier:
                label = f"{label} ✅"
            tier_row.append(
                InlineKeyboardButton(text=label, callback_data=f"{AdminCB.RS_TIER}{tier}")
            )
        rows.append(tier_row)

    none_label = texts.admin_reseller_cell_none
    for pkg in packages:
        slug = str(pkg.slug)
        retail = retail_by_slug.get(slug)
        if retail is None:
            continue
        agreed = agreed_by_slug.get(slug)
        agreed_txt = (
            format_toman(int(agreed), persian_digits=True) if agreed is not None else none_label
        )
        label = texts.admin_reseller_cell_row.format(
            plan=str(pkg.title_fa),
            retail=format_toman(int(retail), persian_digits=True),
            agreed=agreed_txt,
        )
        if len(label) > 64:
            label = label[:61] + "…"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"{AdminCB.RS_CELL}{slug}",
                )
            ]
        )

    undo_label = texts.admin_reseller_undo_btn
    if not can_undo:
        undo_label = f"▫️ {undo_label}"
    pct_label = texts.admin_reseller_pct_btn_flat if flat else texts.admin_reseller_pct_btn
    rows.append(
        [
            InlineKeyboardButton(text=pct_label, callback_data=AdminCB.RS_PCT),
            InlineKeyboardButton(text=undo_label, callback_data=AdminCB.RS_UNDO),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ قیمت‌های نمایندگی",
                callback_data=f"{AdminCB.RS_PANEL}{user_id}",
            )
        ]
    )
    rows.extend(nav_rows(home=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reseller_pct_preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ تأیید",
                    style=styled(SUCCESS),
                    callback_data=AdminCB.RS_PCT_OK,
                ),
                InlineKeyboardButton(
                    text="❌ انصراف",
                    style=styled(DANGER),
                    callback_data=AdminCB.RS_PCT_NO,
                ),
            ],
            *nav_rows(home=True),
        ]
    )


def back_to_customer_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Every sub-screen of a customer card returns to that same card."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ کارت مشتری",
                    callback_data=f"{AdminCB.CUSTOMER_PICK}{user_id}",
                )
            ],
            *nav_rows(home=True),
        ]
    )


def wallet_zero_confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Taking a balance to zero is money leaving — two taps, like every other
    destructive action in this panel."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ بله، صفر کن",
                    style=styled(DANGER),
                    callback_data=f"{AdminCB.WALLET_ZERO_OK}{user_id}",
                ),
                InlineKeyboardButton(
                    text="انصراف", callback_data=f"{AdminCB.CUSTOMER_PICK}{user_id}"
                ),
            ]
        ]
    )


def wallet_confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Manual wallet changes are money, so they get the same two-step treatment
    as every other destructive action."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ تأیید و اعمال",
                    style=styled(SUCCESS),
                    callback_data=f"{AdminCB.WALLET_OK}{user_id}",
                ),
                InlineKeyboardButton(
                    text="انصراف", callback_data=f"{AdminCB.CUSTOMER_PICK}{user_id}"
                ),
            ]
        ]
    )


def service_ops_keyboard(
    account_id: int,
    user_id: int,
    *,
    disabled: bool,
    has_expiry: bool = True,
    extra_rows: list[list[InlineKeyboardButton]] | None = None,
) -> InlineKeyboardMarkup:
    """Operations on one live service.

    Deleting a panel user is deliberately absent: disable keeps usage, expiry
    and the subscription link, so an accidental cut-off is one tap to undo.

    ``has_expiry=False`` («بدون انقضا») drops «📅 تمدید»: there are no days to
    add, and the button used to refuse only after the number was typed.

    ``extra_rows`` go under the operations, above the way back: the deletion list
    (`bot/handlers/lifecycle_admin.py`) puts «🛡 حذف نشود» and «⬅️ فهرست حذف» there.
    """
    toggle_label = "✅ فعال‌سازی سرویس" if disabled else "⛔️ قطع سرویس"
    top = [
        InlineKeyboardButton(text="📦 افزودن حجم", callback_data=f"{AdminCB.SVC_DATA}{account_id}")
    ]
    if has_expiry:
        top.insert(
            0,
            InlineKeyboardButton(
                text="📅 تمدید", callback_data=f"{AdminCB.SVC_EXTEND}{account_id}"
            ),
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            top,
            [
                InlineKeyboardButton(
                    text="📱 دستگاه‌ها", callback_data=f"{AdminCB.SVC_DEVICES}{account_id}"
                ),
                InlineKeyboardButton(
                    text="🔗 لینک و QR", callback_data=f"{AdminCB.SVC_LINK}{account_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text=toggle_label, callback_data=f"{AdminCB.SVC_TOGGLE}{account_id}"
                )
            ],
            *(extra_rows or []),
            [
                InlineKeyboardButton(
                    text="⬅️ سرویس‌ها",
                    callback_data=f"{AdminCB.CUST_SERVICES}{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ کارت مشتری",
                    callback_data=f"{AdminCB.CUSTOMER_PICK}{user_id}",
                )
            ],
            *nav_rows(home=True),
        ]
    )


def _back_to_service_row(account_id: int) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="⬅️ سرویس", callback_data=f"{AdminCB.SVC}{account_id}")]


def service_devices_keyboard(account_id: int, *, count: int) -> InlineKeyboardMarkup:
    """The reset shows only when there is something to forget, alone on a DANGER row."""
    rows: list[list[InlineKeyboardButton]] = []
    if count > 0:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🧹 ریست دستگاه‌ها",
                    style=styled(DANGER),
                    callback_data=f"{AdminCB.SVC_DEV_RESET}{account_id}",
                )
            ]
        )
    rows.append(_back_to_service_row(account_id))
    rows.extend(nav_rows(home=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def service_devices_reset_confirm_keyboard(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🧹 بله، ریست کن",
                    style=styled(DANGER),
                    callback_data=f"{AdminCB.SVC_DEV_RESET_OK}{account_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="انصراف", callback_data=f"{AdminCB.SVC_DEVICES}{account_id}"
                )
            ],
        ]
    )


def service_link_keyboard(account_id: int, *, with_link: bool = True) -> InlineKeyboardMarkup:
    """Send / QR / new link. Without a stored link only «لینک جدید» would do anything."""
    rows: list[list[InlineKeyboardButton]] = []
    if with_link:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📤 بفرست برای مشتری",
                    style=styled(PRIMARY),
                    callback_data=f"{AdminCB.SVC_LINK_SEND}{account_id}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="🖼 QR برای من", callback_data=f"{AdminCB.SVC_LINK_QR}{account_id}"
                ),
                InlineKeyboardButton(
                    text="♻️ لینک جدید", callback_data=f"{AdminCB.SVC_LINK_NEW}{account_id}"
                ),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="♻️ لینک جدید", callback_data=f"{AdminCB.SVC_LINK_NEW}{account_id}"
                )
            ]
        )
    rows.append(_back_to_service_row(account_id))
    rows.extend(nav_rows(home=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def service_link_back_keyboard(account_id: int) -> InlineKeyboardMarkup:
    """Under the QR photo sent to the admin: back to the link screen, never the customer card."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ لینک و QR", callback_data=f"{AdminCB.SVC_LINK}{account_id}"
                )
            ],
            *nav_rows(home=True),
        ]
    )


def service_link_revoke_confirm_keyboard(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="♻️ بله، لینک جدید بساز",
                    style=styled(DANGER),
                    callback_data=f"{AdminCB.SVC_LINK_NEW_OK}{account_id}",
                )
            ],
            [InlineKeyboardButton(text="انصراف", callback_data=f"{AdminCB.SVC_LINK}{account_id}")],
        ]
    )


def service_toggle_confirm_keyboard(account_id: int, *, disable: bool) -> InlineKeyboardMarkup:
    """Cutting a customer off mid-subscription is worth a second tap."""
    label = "⛔️ بله، قطع کن" if disable else "✅ بله، فعال کن"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label,
                    style=styled(DANGER) if disable else None,
                    callback_data=f"{AdminCB.SVC_TOGGLE_OK}{account_id}",
                )
            ],
            [InlineKeyboardButton(text="انصراف", callback_data=f"{AdminCB.SVC}{account_id}")],
        ]
    )


def service_data_confirm_keyboard(account_id: int) -> InlineKeyboardMarkup:
    """The typed gigabytes are only a question until this is tapped."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ بله، اضافه کن",
                    style=styled(PRIMARY),
                    callback_data=f"{AdminCB.SVC_DATA_OK}{account_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ کارت سرویس", callback_data=f"{AdminCB.SVC}{account_id}"
                )
            ],
        ]
    )


def reseller_revoke_confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⛔️ بله، حذف کن",
                    style=styled(DANGER),
                    callback_data=f"{AdminCB.CUST_RESELLER_OK}{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ کارت مشتری", callback_data=f"{AdminCB.CUSTOMER_PICK}{user_id}"
                )
            ],
        ]
    )


def report_customer_link_keyboard(user_id: int, *, bot_username: str) -> InlineKeyboardMarkup:
    """Opens this customer's card in the private chat. The group message stays put."""
    handle = bot_username.strip().lstrip("@")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="کارت مشتری",
                    style=styled(PRIMARY),
                    url=f"https://t.me/{handle}?start=admcu_{int(user_id)}",
                )
            ]
        ]
    )


def report_group_keyboard(readiness: Any) -> InlineKeyboardMarkup:
    """Setup screen. Every row states the truth about one route.

    A topic shows «آماده» only once a real message reached it — «تنظیم شد» is
    not proof, and a wrong thread id looks identical in the database.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if not readiness.attached:
        rows.append(
            [InlineKeyboardButton(text="🔗 وصل کردن گروه", callback_data=AdminCB.REP_ATTACH)]
        )
    else:
        for topic in readiness.topics:
            mark = "✅" if topic.ready else ("🟡" if topic.configured else "⚪️")
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{mark} {topic.title_fa}",
                        callback_data=f"{AdminCB.REP_TOPIC}{topic.key}",
                    ),
                    InlineKeyboardButton(
                        text="🧪 تست",
                        callback_data=f"{AdminCB.REP_VERIFY}{topic.key}",
                    ),
                ]
            )
        toggle = "⛔️ خاموش کردن گزارش‌ها" if readiness.enabled else "✅ روشن کردن گزارش‌ها"
        rows.append([InlineKeyboardButton(text=toggle, callback_data=AdminCB.REP_TOGGLE)])
        rows.append([InlineKeyboardButton(text="🔗 تغییر گروه", callback_data=AdminCB.REP_ATTACH)])
    rows.extend(nav_rows(home=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def broadcast_home_keyboard(running_id: int | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="✍️ پیام جدید", callback_data=AdminCB.CAST_NEW)],
        # Its own entry because during an outage the operator has no attention
        # to spare for composing: this one asks for facts and writes the rest.
        [InlineKeyboardButton(text="📢 اعلان قطعی", callback_data=AdminCB.ANN)],
    ]
    if running_id is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📊 وضعیت ارسال جاری",
                    callback_data=f"{AdminCB.CAST_STATUS}{running_id}",
                )
            ]
        )
    rows.extend(nav_rows(home=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def announcement_home_keyboard() -> InlineKeyboardMarkup:
    """Outage and its follow-up, side by side.

    The restore button sits here rather than being remembered later, because the
    promise «وقتی برگشت همین‌جا خبر می‌دم» is the half that earns the trust.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 اعلام قطعی", callback_data=AdminCB.ANN_OUTAGE)],
            [InlineKeyboardButton(text="✅ سرویس برگشت", callback_data=AdminCB.ANN_RESTORE)],
            [InlineKeyboardButton(text="⬅️ پیام همگانی", callback_data=AdminCB.CAST)],
        ]
    )


def announcement_since_keyboard() -> InlineKeyboardMarkup:
    """«الان» covers the common case without making the operator read a clock."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🕐 همین الان", callback_data=AdminCB.ANN_NOW)],
            [InlineKeyboardButton(text="❌ لغو", callback_data=AdminCB.CANCEL_INPUT)],
        ]
    )


def announcement_alternative_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏭ رد کن", callback_data=AdminCB.ANN_SKIP)],
            [InlineKeyboardButton(text="❌ لغو", callback_data=AdminCB.CANCEL_INPUT)],
        ]
    )


def broadcast_audience_keyboard(counts: dict[str, int]) -> InlineKeyboardMarkup:
    """Audience choices, each showing how many people it reaches.

    The count is on the button because «همهٔ مشتری‌ها» means nothing until you
    know whether that is nine people or nine thousand.
    """
    rows = [
        [
            InlineKeyboardButton(
                text=f"{title} ({to_persian_digits(counts.get(key, 0))})",
                callback_data=f"{AdminCB.CAST_AUD}{key}",
            )
        ]
        for key, title in _AUDIENCE_LABELS
    ]
    rows.append([InlineKeyboardButton(text="انصراف", callback_data=AdminCB.CAST)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


_AUDIENCE_LABELS: tuple[tuple[str, str], ...] = (
    ("all_customers", "👥 همهٔ مشتری‌ها"),
    ("with_service", "📱 دارندگان سرویس"),
    ("expiring_soon", "⏳ سرویس‌های رو به انقضا"),
    ("without_service", "🕸 بدون سرویس"),
)


def broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    """Sending to every customer cannot be undone, so it takes a second tap."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧪 اول برای خودم بفرست", callback_data=AdminCB.CAST_TEST)],
            [
                InlineKeyboardButton(
                    text="📤 بله، ارسال کن", style=styled(SUCCESS), callback_data=AdminCB.CAST_SEND
                ),
                InlineKeyboardButton(text="انصراف", callback_data=AdminCB.CAST),
            ],
        ]
    )


def broadcast_progress_keyboard(broadcast_id: int, *, running: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="🔄 به‌روزرسانی",
                callback_data=f"{AdminCB.CAST_STATUS}{broadcast_id}",
            )
        ]
    ]
    if running:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⛔️ توقف ارسال",
                    style=styled(DANGER),
                    callback_data=f"{AdminCB.CAST_CANCEL}{broadcast_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ پیام همگانی", callback_data=AdminCB.CAST)])
    rows.extend(nav_rows(home=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)

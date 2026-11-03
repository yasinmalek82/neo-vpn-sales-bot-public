"""The business events worth reporting, as Persian text.

One builder per event, each returning a :class:`Report` so the caller only has
to enqueue it. Keeping the wording here — rather than at each call site — is
what stops the same event being phrased three different ways.

``event_key`` names the **transition**, never just the order: a wallet credit,
a retry debit and a refund on one order are three reports, while the same
transition reported twice is one.

Empty facts are omitted. A line that says «none» or a blank label is the bot
claiming it looked and found nothing, which is a lie when the field was never
stored.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import Any

from database.models.report import (
    TOPIC_CUSTOMERS,
    TOPIC_DISRUPTIONS,
    TOPIC_ERRORS,
    TOPIC_PAYMENTS,
    TOPIC_SALES,
    TOPIC_SYSTEM,
    TOPIC_WALLET,
)
from services.panel_status import format_uptime_fa
from services.ux_format import format_jalali, format_toman, to_persian_digits

__all__ = [
    "Report",
    "disruption_reported",
    "late_receipt",
    "nightly_digest",
    "order_activated",
    "payment_received",
    "panel_node_down",
    "panel_node_limited",
    "panel_node_recovered",
    "panel_recovered",
    "panel_unreachable",
    "payment_rejected",
    "provision_failed",
    "reseller_price_crossed_over",
    "translate_panel_error",
    "trial_issued",
    "user_joined",
    "wallet_changed",
]

# Start-payload prefix for the customer-card button under a report.
# Longer than a referral code and not a code a customer would type.
ADMIN_CUSTOMER_START_PREFIX = "admcu_"


@dataclass(frozen=True)
class Report:
    event_key: str
    topic_key: str
    body: str
    link_user_id: int | None = None


@dataclass(frozen=True)
class DigestLeader:
    """One row of the nightly «who spent the most» list."""

    username: str | None
    name: str | None
    is_reseller: bool
    amount_toman: int


_METHOD_FA: dict[str, str] = {
    "card_to_card": "کارت‌به‌کارت",
    "card": "کارت‌به‌کارت",
    "crypto": "رمزارز",
    "manual": "دستی",
    "wallet": "کیف پول",
}

_WALLET_TITLE: dict[str, str] = {
    "admin_manual_credit": "🏦 <b>موجودی افزایش یافت</b>",
    "admin_manual_debit": "📉 <b>موجودی کم شد</b>",
    "referral_reward": "🎁 <b>پاداش معرفی واریز شد</b>",
    "provision_fail_credit": "💳 <b>جبران فعال‌سازی ناموفق</b>",
    "reseller_trial": "🎁 <b>هزینهٔ تست نمایندگی</b>",
}

_WALLET_REASON_FA: dict[str, str] = {
    "admin_manual_credit": "شارژ دستی",
    "migration_opening": "ماندهٔ انتقال میرزا",
    "admin_manual_debit": "کسر دستی",
    "referral_reward": "پاداش معرفی",
    "provision_fail_credit": "جبران فعال‌سازی ناموفق",
    "reseller_trial": "تست نمایندگی",
    "topup": "شارژ",
}

# Substrings of panel errors the owner can act on without reading English.
_PANEL_ERROR_FA: tuple[tuple[str, str], ...] = (
    ("could not resolve host", "میزبان پنل پیدا نشد"),
    ("email already in use", "این نام کانفیگ قبلاً ساخته شده"),
    ("record not found", "رکورد در پنل پیدا نشد"),
    ("resource not found", "سرویس در پنل پیدا نشد"),
    ("core is not started", "هستهٔ پنل هنوز بالا نیامده"),
)


def translate_panel_error(detail: str) -> str:
    """Persian for the panel failures we have actually seen; otherwise a short raw line.

    A known phrase replaces the whole detail, so a host name sitting after
    «Could not resolve host» never reaches the group. Anything else is escaped
    and trimmed; the outbox still redacts links on the way in.
    """
    raw = (detail or "").strip()
    if not raw:
        return "دلیل نامشخص"
    return known_panel_error_fa(raw) or escape(raw[:200])


def known_panel_error_fa(detail: str) -> str | None:
    """The Persian line for a panel failure we know by its wording, else ``None``."""
    folded = (detail or "").casefold()
    for needle, persian in _PANEL_ERROR_FA:
        if needle in folded:
            return persian
    return None


def admin_customer_start_payload(user_id: int) -> str:
    return f"{ADMIN_CUSTOMER_START_PREFIX}{int(user_id)}"


def parse_admin_customer_start(payload: str | None) -> int | None:
    """Database user id carried by a report's customer-card button, or None."""
    raw = (payload or "").strip()
    prefix = ADMIN_CUSTOMER_START_PREFIX
    if not raw.startswith(prefix):
        return None
    rest = raw[len(prefix) :]
    if not rest.isdigit():
        return None
    return int(rest)


def _toman(value: int) -> str:
    return f"{format_toman(int(value), persian_digits=True)} تومان"


def _esc(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    return escape(text)


def _username(username: str | None) -> str | None:
    raw = (username or "").strip().lstrip("@")
    if not raw:
        return None
    return f"@{escape(raw)}"


def _role(is_reseller: bool) -> str:
    return "نماینده" if is_reseller else "مشتری"


def _line(label: str, value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    return f"{label}: {value}"


def _block(title: str, lines: list[str | None]) -> str | None:
    kept = [line for line in lines if line]
    if not kept:
        return None
    return title + "\n" + "\n".join(kept)


def _join(parts: list[str | None]) -> str:
    return "\n\n".join(part for part in parts if part)


def _when(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return format_jalali(moment, persian_digits=True)


def _gb(value: int | None) -> str | None:
    if value is None:
        return None
    return f"{to_persian_digits(int(value))} گیگ"


def _days(value: int | None) -> str | None:
    if value is None:
        return None
    return f"{to_persian_digits(int(value))} روز"


def product_shape_fa(volume_gb: int | None, duration_days: int | None) -> str | None:
    """A product with no shelf name (custom, legacy), said by what it holds."""
    parts = [_gb(volume_gb) if volume_gb else None, _days(duration_days) if duration_days else None]
    return "، ".join(p for p in parts if p) or None


def _method_fa(method: str) -> str:
    key = str(method or "").strip()
    return _METHOD_FA.get(key, escape(key) if key else "نامشخص")


def _admin_id_from_adjust_key(idempotency_key: str | None) -> int | None:
    """``admin_adjust:{telegram_id}:{user_id}:{stamp}`` — the ledger has no admin column."""
    parts = (idempotency_key or "").split(":")
    if len(parts) >= 2 and parts[0] == "admin_adjust" and parts[1].isdigit():
        return int(parts[1])
    return None


def _person_block(
    title: str,
    *,
    telegram_id: int | None,
    username: str | None,
    is_reseller: bool | None,
    name_label: str | None = None,
    name: str | None = None,
    phone: str | None = None,
) -> str | None:
    lines: list[str | None] = []
    if telegram_id:
        lines.append(_line("آیدی", to_persian_digits(int(telegram_id))))
    lines.append(_line("نام کاربری", _username(username)))
    if is_reseller is not None:
        lines.append(_line("نقش", _role(is_reseller)))
    if name_label:
        lines.append(_line(name_label, _esc(name)))
    lines.append(_line("تلفن", _esc(phone)))
    return _block(title, lines)


def order_activated(
    *,
    order_id: int,
    telegram_id: int,
    username: str | None,
    is_reseller: bool,
    price_toman: int,
    product: str | None,
    is_renewal: bool,
    link_user_id: int | None = None,
    service_name: str | None = None,
    buyer_name: str | None = None,
    phone: str | None = None,
    panel_username: str | None = None,
    volume_gb: int | None = None,
    duration_days: int | None = None,
    balance_before: int | None = None,
    balance_after: int | None = None,
    when: datetime | None = None,
) -> Report:
    """A paid activation. A renewal and a new purchase are different sentences."""
    title = "🔄 <b>تمدید ثبت شد</b>" if is_renewal else "🛒 <b>خرید جدید ثبت شد</b>"
    name_label = "نام روی سرویس" if (service_name or "").strip() else "نام"
    shown_name = service_name if (service_name or "").strip() else buyer_name
    money_lines: list[str | None] = [
        _line("مبلغ تمدید" if is_renewal else "قیمت", _toman(price_toman)),
    ]
    if balance_before is not None and balance_after is not None:
        money_lines.append(_line("موجودی قبل", _toman(balance_before)))
        money_lines.append(_line("موجودی بعد", _toman(balance_after)))
    body = _join(
        [
            title,
            _person_block(
                "👤 خریدار",
                telegram_id=telegram_id,
                username=username,
                is_reseller=is_reseller,
                name_label=name_label,
                name=shown_name,
                phone=phone,
            ),
            _block(
                "📦 سرویس",
                [
                    _line("کانفیگ", _esc(panel_username)),
                    _line("محصول", _esc(product) or product_shape_fa(volume_gb, duration_days)),
                    _line("حجم", _gb(volume_gb)),
                    _line("مدت", _days(duration_days)),
                ],
            ),
            _block("💰 حساب", money_lines),
            _line("زمان", _when(when)),
            _line("کد سفارش", to_persian_digits(order_id)),
        ]
    )
    return Report(
        event_key=f"order:{order_id}:active",
        topic_key=TOPIC_SALES,
        body=body,
        link_user_id=link_user_id,
    )


def bank_sms_manual_review(*, event_key: str, amount_toman: int, order_id: int | None) -> Report:
    return Report(
        event_key=f"bank_sms:{event_key}:manual",
        topic_key=TOPIC_PAYMENTS,
        body=_join(
            [
                "🏛 <b>واریز بلو نیاز به بررسی دستی دارد</b>",
                _line("مبلغ", _toman(amount_toman)),
                _line("کد سفارش", str(order_id) if order_id is not None else "بدون تطبیق"),
                _line("شناسه بررسی", event_key),
                "پرداخت خودکار تأیید نشده؛ گردش حساب و سفارش را بررسی کن.",
            ]
        ),
    )


def payment_received(
    *,
    order_id: int,
    telegram_id: int,
    name: str | None,
    amount_toman: int,
    method: str,
    username: str | None = None,
    is_reseller: bool = False,
    admin_telegram_id: int | None = None,
    admin_username: str | None = None,
    when: datetime | None = None,
    link_user_id: int | None = None,
) -> Report:
    body = _join(
        [
            "✅ <b>پرداخت تأیید شد</b>",
            _person_block(
                "👤 خریدار",
                telegram_id=telegram_id,
                username=username,
                is_reseller=is_reseller,
                name_label="نام",
                name=name,
            ),
            _block(
                "💳 پرداخت",
                [
                    _line("مبلغ", _toman(amount_toman)),
                    _line("روش", _method_fa(method)),
                    _line("کد سفارش", to_persian_digits(order_id)),
                    _line("زمان", _when(when)),
                ],
            ),
            _person_block(
                "🛡️ ادمین",
                telegram_id=admin_telegram_id,
                username=admin_username,
                is_reseller=None,
            ),
        ]
    )
    return Report(
        event_key=f"order:{order_id}:paid",
        topic_key=TOPIC_PAYMENTS,
        body=body,
        link_user_id=link_user_id,
    )


def payment_rejected(
    *,
    order_id: int,
    telegram_id: int,
    name: str | None,
    reason: str,
    username: str | None = None,
    is_reseller: bool = False,
    admin_telegram_id: int | None = None,
    admin_username: str | None = None,
    link_user_id: int | None = None,
) -> Report:
    body = _join(
        [
            "❌ <b>پرداخت رد شد</b>",
            _person_block(
                "👤 خریدار",
                telegram_id=telegram_id,
                username=username,
                is_reseller=is_reseller,
                name_label="نام",
                name=name,
            ),
            _block(
                "💳 پرداخت",
                [
                    _line("کد سفارش", to_persian_digits(order_id)),
                    _line("دلیل", _esc(reason) or "—"),
                ],
            ),
            _person_block(
                "🛡️ ادمین",
                telegram_id=admin_telegram_id,
                username=admin_username,
                is_reseller=None,
            ),
        ]
    )
    return Report(
        event_key=f"order:{order_id}:rejected",
        topic_key=TOPIC_PAYMENTS,
        body=body,
        link_user_id=link_user_id,
    )


def late_receipt(
    *,
    order_id: int,
    telegram_id: int,
    name: str | None,
    amount_toman: int,
    expired_at: datetime | None,
    username: str | None = None,
    is_reseller: bool = False,
    link_user_id: int | None = None,
) -> Report:
    """A card receipt for an order that had already expired: real money, no owner.

    ``event_key`` is the order, not the receipt: a customer who sends the same
    photo three times is one thing for the owner to look at.
    """
    body = _join(
        [
            "⏰ <b>رسید برای سفارش منقضی‌شده</b>",
            _person_block(
                "👤 مشتری",
                telegram_id=telegram_id,
                username=username,
                is_reseller=is_reseller,
                name_label="نام",
                name=name,
            ),
            _block(
                "💳 پرداخت",
                [
                    _line("مبلغ کارت", _toman(amount_toman)),
                    _line("کد سفارش", to_persian_digits(order_id)),
                    _line("منقضی شد", _when(expired_at)),
                ],
            ),
            "سفارش فعال نمی‌شه. رسید برای ادمین‌ها رفته تا کیف پول مشتری شارژ بشه.",
        ]
    )
    return Report(
        event_key=f"late_receipt:{order_id}",
        topic_key=TOPIC_PAYMENTS,
        body=body,
        link_user_id=link_user_id,
    )


def provision_failed(
    *,
    order_id: int,
    telegram_id: int,
    detail: str,
    username: str | None = None,
    panel_username: str | None = None,
    link_user_id: int | None = None,
) -> Report:
    """Payment captured but the panel did not deliver — needs a human now."""
    body = _join(
        [
            "🚨 <b>ساخت اشتراک انجام نشد</b>",
            _block(
                "👤 مشتری",
                [
                    _line("آیدی", to_persian_digits(telegram_id) if telegram_id else None),
                    _line("نام کاربری", _username(username)),
                    _line("کانفیگ", _esc(panel_username)),
                    _line("کد سفارش", to_persian_digits(order_id)),
                ],
            ),
            _line("دلیل", translate_panel_error(detail)),
            "پول گرفته شده ولی سرویس تحویل نشده — از «پرداخت در انتظار» تلاش مجدد بزن.",
        ]
    )
    return Report(
        event_key=f"order:{order_id}:provision_failed",
        topic_key=TOPIC_ERRORS,
        body=body,
        link_user_id=link_user_id,
    )


def wallet_changed(
    *,
    entry_id: int,
    telegram_id: int,
    name: str | None,
    amount_toman: int,
    balance_after: int,
    reason: str,
    username: str | None = None,
    idempotency_key: str | None = None,
    when: datetime | None = None,
    link_user_id: int | None = None,
) -> Report:
    """Keyed on the ledger entry, so three moves on one order stay three reports.

    ``amount_toman`` is signed: positive credits, negative debits.
    Checkout debits are not enqueued — the sale card already shows that balance.
    """
    title = _WALLET_TITLE.get(reason)
    if title is None:
        title = "🏦 <b>کیف پول شارژ شد</b>" if amount_toman > 0 else "📉 <b>از کیف پول کم شد</b>"
    admin_id = _admin_id_from_adjust_key(idempotency_key)
    before = int(balance_after) - int(amount_toman)
    reason_fa = _WALLET_REASON_FA.get(reason)
    body = _join(
        [
            title,
            _person_block(
                "👤 مشتری",
                telegram_id=telegram_id,
                username=username,
                is_reseller=None,
                name_label="نام",
                name=name,
            ),
            _block(
                "🏦 کیف پول",
                [
                    _line("مبلغ", _toman(abs(int(amount_toman)))),
                    _line("بابت", reason_fa),
                    _line("موجودی قبل", _toman(before)),
                    _line("موجودی بعد", _toman(int(balance_after))),
                    _line("زمان", _when(when)),
                ],
            ),
            _person_block(
                "🛡️ ادمین",
                telegram_id=admin_id,
                username=None,
                is_reseller=None,
            ),
        ]
    )
    return Report(
        event_key=f"wallet_entry:{entry_id}",
        topic_key=TOPIC_WALLET,
        body=body,
        link_user_id=link_user_id,
    )


def trial_issued(
    *,
    telegram_id: int,
    name: str | None,
    gb: int,
    days: int,
    order_id: int | None = None,
    username: str | None = None,
    is_reseller: bool = False,
    service_name: str | None = None,
    phone: str | None = None,
    panel_username: str | None = None,
    when: datetime | None = None,
    link_user_id: int | None = None,
) -> Report:
    """One report per trial.

    ``order_id`` keys the report. A reseller hands out many trials and a
    customer takes one per store group, so a key of «trial:{telegram_id}» would
    report the first and silently swallow every one after it.
    """
    name_label = "نام روی سرویس" if (service_name or "").strip() else "نام"
    shown_name = service_name if (service_name or "").strip() else name
    body = _join(
        [
            "🎁 <b>تست صادر شد</b>",
            _person_block(
                "👤 مشتری",
                telegram_id=telegram_id,
                username=username,
                is_reseller=is_reseller,
                name_label=name_label,
                name=shown_name,
                phone=phone,
            ),
            _block(
                "📦 تست",
                [
                    _line("کانفیگ", _esc(panel_username)),
                    _line("حجم", _gb(gb)),
                    _line("مدت", _days(days)),
                    _line("زمان", _when(when)),
                    _line("کد سفارش", to_persian_digits(order_id) if order_id else None),
                ],
            ),
        ]
    )
    key = f"trial:{telegram_id}" if order_id is None else f"trial:order:{order_id}"
    return Report(
        event_key=key,
        topic_key=TOPIC_CUSTOMERS,
        body=body,
        link_user_id=link_user_id,
    )


def user_joined(
    *,
    telegram_id: int,
    name: str | None,
    username: str | None,
    link_user_id: int | None = None,
) -> Report:
    body = _join(
        [
            "🎉 <b>کاربر جدید</b>",
            _block(
                "👤 کاربر",
                [
                    _line("نام", _esc(name)),
                    _line("نام کاربری", _username(username)),
                    _line("آیدی", to_persian_digits(telegram_id)),
                ],
            ),
        ]
    )
    return Report(
        event_key=f"user:{telegram_id}:joined",
        topic_key=TOPIC_CUSTOMERS,
        body=body,
        link_user_id=link_user_id,
    )


def disruption_reported(
    *,
    report_id: int,
    telegram_id: int,
    name: str | None,
    username: str | None,
    is_reseller: bool,
    isp: str,
    city: str,
    service_groups: str | None,
    when: datetime | None = None,
    link_user_id: int | None = None,
) -> Report:
    """One customer's «it does not work here». Keyed on the report row."""
    body = _join(
        [
            "📡 <b>گزارش اختلال</b>",
            _block(
                "📍 کجا",
                [
                    _line("اپراتور", _esc(isp)),
                    _line("شهر", _esc(city)),
                    _line("سرویس‌ها", _esc(service_groups) or "بدون سرویس فعال"),
                    _line("زمان", _when(when)),
                ],
            ),
            _person_block(
                "👤 مشتری",
                telegram_id=telegram_id,
                username=username,
                is_reseller=is_reseller,
                name_label="نام",
                name=name,
            ),
        ]
    )
    return Report(
        event_key=f"disruption:{report_id}",
        topic_key=TOPIC_DISRUPTIONS,
        body=body,
        link_user_id=link_user_id,
    )


def reseller_price_crossed_over(
    *,
    telegram_id: int,
    name: str | None,
    what: str,
    reseller_price_toman: int,
    retail_price_toman: int,
    username: str | None = None,
    link_user_id: int | None = None,
) -> Report:
    """A reseller's agreed price landed above retail, so retail was charged.

    Keyed on the combination rather than the sale, so a rate that is wrong for
    one shape reports once and not on every repeat purchase.
    """
    body = _join(
        [
            "📉 <b>قیمت نمایندگی از خرده بالاتر زد</b>",
            _person_block(
                "👤 نماینده",
                telegram_id=telegram_id,
                username=username,
                is_reseller=None,
                name_label="نام",
                name=name,
            ),
            _block(
                "💰 قیمت",
                [
                    _line("مورد", _esc(what)),
                    _line("قیمت نمایندگی", _toman(reseller_price_toman)),
                    _line("قیمت خرده", _toman(retail_price_toman)),
                ],
            ),
            "قیمت خرده گرفته شد تا ضرر نکند. نرخ نمایندگی این محدوده را درست کن.",
        ]
    )
    return Report(
        event_key=f"reseller_crossover:{telegram_id}:{what}",
        topic_key=TOPIC_ERRORS,
        body=body,
        link_user_id=link_user_id,
    )


def renew_group_not_found(
    *,
    telegram_id: int,
    name: str | None,
    username: str | None,
    account_id: int,
    panel_username: str,
    link_user_id: int | None = None,
) -> Report:
    """A renew from scratch had no single store group to belong to.

    The customer was told it is not available; the owner has to fix the group
    (the order named none, and the panel groups matched none or several).
    Keyed on the account, so one stuck service reports once, not on every tap.
    """
    body = _join(
        [
            "⚠️ <b>تمدید یه سرویس گروه فروشگاه پیدا نکرد</b>",
            _person_block(
                "👤 مشتری",
                telegram_id=telegram_id,
                username=username,
                is_reseller=None,
                name_label="نام",
                name=name,
            ),
            _block(
                "📡 سرویس",
                [_line("نام کاربری پنل", f"<code>{escape(panel_username)}</code>")],
            ),
            "گروه این سرویس از سفارش یا گروه‌های پنلش مشخص نشد و بیشتر از یه گروه حجمی هم "
            "هست. به مشتری گفته شد فعلاً تمدید نمی‌شه. گروه‌های پنل گروه فروشگاه رو "
            "چک کن.",
        ]
    )
    return Report(
        event_key=f"renew_no_group:{int(account_id)}",
        topic_key=TOPIC_ERRORS,
        body=body,
        link_user_id=link_user_id,
    )


def nightly_digest(
    *,
    jalali_key: str,
    day_label: str,
    fresh_count: int,
    fresh_toman: int,
    renew_count: int,
    renew_toman: int,
    trials: int,
    volume_gb: int,
    new_users: int,
    leaders: list[DigestLeader],
    disruptions: Sequence[Any] = (),
) -> Report:
    """One message for the Tehran day that just ended. Keyed on that date."""
    lines = [
        "📌 <b>جمع‌بندی شب</b>",
        day_label,
        "",
        f"خرید تازه: {to_persian_digits(fresh_count)} • {_toman(fresh_toman)}",
        f"تمدید: {to_persian_digits(renew_count)} • {_toman(renew_toman)}",
        f"تست: {to_persian_digits(trials)}",
        f"حجم فروخته‌شده: {_gb(volume_gb)}",
        f"کاربر جدید: {to_persian_digits(new_users)}",
    ]
    if leaders:
        lines.append("")
        lines.append("🏆 برترین خریدها")
        for index, leader in enumerate(leaders, start=1):
            who = _username(leader.username) or _esc(leader.name) or "بدون نام"
            role = _role(leader.is_reseller)
            lines.append(
                f"{to_persian_digits(index)}. {who} • {role} • {_toman(leader.amount_toman)}"
            )
    if disruptions:
        lines.append("")
        lines.append("📡 گزارش اختلال (بیشترین اول)")
        for row in disruptions:
            city = f" • بیشتر {_esc(row.top_city)}" if row.top_city else ""
            lines.append(f"{_esc(row.label)}: {to_persian_digits(row.count)}{city}")
    return Report(
        event_key=f"digest:{jalali_key}",
        topic_key=TOPIC_SYSTEM,
        body="\n".join(lines),
    )


# ── panel watch ──────────────────────────────────────────────────────────
#
# One report per *transition* of a node (or of the panel, node id 0). The key is
# ``panel_node:{node_id}:{status}:{status_since}``: a node that goes down twice in
# a day is two alerts, while the same down reported twice is one.

PANEL_NODE_MESSAGE_MAX = 80


def panel_node_event_key(node_id: int, status: str, since: datetime) -> str:
    return f"panel_node:{int(node_id)}:{status}:{since.isoformat()}"


def _outage_duration(seconds: int) -> str:
    if seconds < 60:
        return "کمتر از یک دقیقه"
    return format_uptime_fa(seconds)


def _short_message(message: str | None) -> str | None:
    text = " ".join((message or "").split())
    if not text:
        return None
    if len(text) > PANEL_NODE_MESSAGE_MAX:
        text = text[: PANEL_NODE_MESSAGE_MAX - 1].rstrip() + "…"
    return escape(text)


def panel_node_down(
    *, node_id: int, name: str, status: str, since: datetime, message: str | None = None
) -> Report:
    """A node has been in ``error`` for two checks in a row."""
    body = _join(
        [
            f"🔴 <b>سرور «{escape(name)}» قطع شد</b>",
            _block(
                "📍 جزئیات",
                [
                    _line("وضعیت", escape(status)),
                    _line("پیام پنل", _short_message(message)),
                    _line("از", _when(since)),
                ],
            ),
            "از «📊 وضعیت پنل» می‌شه وصلش دوباره زد.",
        ]
    )
    return Report(
        event_key=panel_node_event_key(node_id, status, since),
        topic_key=TOPIC_SYSTEM,
        body=body,
    )


def panel_node_limited(
    *, node_id: int, name: str, since: datetime, message: str | None = None
) -> Report:
    """A node hit its data limit and is out of rotation."""
    body = _join(
        [
            f"📉 <b>سرور «{escape(name)}» به سقف حجم رسید</b>",
            _block(
                "📍 جزئیات",
                [
                    _line("پیام پنل", _short_message(message)),
                    _line("از", _when(since)),
                ],
            ),
        ]
    )
    return Report(
        event_key=panel_node_event_key(node_id, "limited", since),
        topic_key=TOPIC_SYSTEM,
        body=body,
    )


def panel_node_recovered(*, node_id: int, name: str, since: datetime, down_seconds: int) -> Report:
    """A node that had an alert out is ``connected`` again. ``since`` is when it came back."""
    body = _join(
        [
            f"🟢 <b>سرور «{escape(name)}» دوباره وصل شد</b>",
            _block(
                "📍 جزئیات",
                [
                    _line("مدت قطعی", _outage_duration(int(down_seconds))),
                    _line("وصل‌شدن", _when(since)),
                ],
            ),
        ]
    )
    return Report(
        event_key=panel_node_event_key(node_id, "connected", since),
        topic_key=TOPIC_SYSTEM,
        body=body,
    )


def panel_unreachable(*, since: datetime) -> Report:
    """The bot could not read the panel twice in a row (down, or the key is refused)."""
    body = _join(
        [
            "🔴 <b>پنل در دسترس نیست</b>",
            _block("📍 جزئیات", [_line("از", _when(since))]),
            "تا پنل جواب نده، سفارش جدید، تمدید و تست رد می‌شه. آدرس و کلید را از «🔌 اتصال پنل» ببین.",
        ]
    )
    return Report(
        event_key=panel_node_event_key(0, "unreachable", since),
        topic_key=TOPIC_SYSTEM,
        body=body,
    )


def panel_recovered(*, since: datetime, down_seconds: int) -> Report:
    body = _join(
        [
            "🟢 <b>پنل دوباره در دسترس است</b>",
            _block(
                "📍 جزئیات",
                [
                    _line("مدت قطعی", _outage_duration(int(down_seconds))),
                    _line("وصل‌شدن", _when(since)),
                ],
            ),
            "سفارش جدید، تمدید و تست دوباره باز شد.",
        ]
    )
    return Report(
        event_key=panel_node_event_key(0, "connected", since),
        topic_key=TOPIC_SYSTEM,
        body=body,
    )


def provision_pending(*, order_id: int, user_id: int) -> Report:
    """A paid intent remains unresolved; customer desk gives the operator recovery tools."""
    return Report(
        event_key=f"order:{order_id}:provision_aged",
        topic_key=TOPIC_ERRORS,
        body=(
            "🚨 <b>فعال‌سازی سفارش معلق مانده</b>\n"
            f"🧾 کد سفارش: {to_persian_digits(order_id)}\n"
            "نتیجهٔ پنل هنوز تأیید نشده؛ بازیابی خودکار ادامه دارد. "
            "در پروندهٔ مشتری وضعیت سفارش و پنل را بررسی کن. "
            "پیش از روشن‌شدن نتیجه، بازپرداخت یا سرویس جایگزین ثبت نکن."
        ),
        link_user_id=user_id,
    )


def account_linked(
    *, account_id: int, user_id: int, telegram_id: int, panel_username: str, actor_telegram_id: int
) -> Report:
    return Report(
        event_key=f"account:{account_id}:linked",
        topic_key=TOPIC_CUSTOMERS,
        body=(
            "🔗 <b>سرویس موجود به مشتری وصل شد</b>\n"
            f"👤 مشتری: {to_persian_digits(telegram_id)}\n"
            f"📡 سرویس: <code>{escape(panel_username)}</code>\n"
            f"ادمین: {to_persian_digits(actor_telegram_id)}"
        ),
        link_user_id=user_id,
    )

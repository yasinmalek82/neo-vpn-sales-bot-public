"""Section 8, the owner's customer desk: service operations, ban, search, gifts, the expiring list.

Rows ۸.۵ to ۸.۸, ۸.۱۵, ۸.۲۰ to ۸.۲۸ and ۸.۳۱.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from bot.keyboards.reply_menu import ACCOUNT
from bot.texts import texts
from database.models import Order, OrderStatus
from services.customer_lookup import USER_FILTER_LABELS, UserFilter
from tests.e2e._s07_helpers import (
    credit_by_service,
    db_user,
    list_names,
    open_customer_card,
    press_on_last,
    row_name,
    take_trial,
    users_list,
)
from tests.e2e._s07b_helpers import tap_starting
from tests.e2e._s08_helpers import buy_active, expire_in, open_service
from tests.e2e.harness import GB, App, Store, publish_store, smoke
from tests.e2e.harness.expectations import report_plain

FILTER = dict(USER_FILTER_LABELS)


@pytest.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


# ------------------------------------------------------------- extend, add data, cut, restore


@smoke("۸.۵")
async def test_extending_thirty_days_lands_on_the_panel(app: App, store: Store) -> None:
    ali = app.customer("ali")
    account = await buy_active(app, store, ali)
    expire_in(account, days=10)
    before = account.expire
    admin = app.admin()
    await open_service(admin, "ali")
    await tap_starting(admin, "📅 تمدید")
    await admin.send("30")
    assert admin.last_text.startswith("✅ تمدید شد"), admin.last_text
    gained = (app.panel.user(account.username).expire - before).total_seconds() / 86400
    assert 29.9 < gained < 30.1, gained
    assert app.panel.user(account.username).data_limit == 50 * GB, "only the date moved"


@smoke("۸.۶")
async def test_adding_ten_gigabytes_adds_to_the_limit_it_does_not_replace_it(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    account = await buy_active(app, store, ali)
    assert account.data_limit == 50 * GB
    admin = app.admin()
    await open_service(admin, "ali")
    await tap_starting(admin, "📦 افزودن حجم")
    await admin.send("10")
    await tap_starting(admin, "✅ بله، اضافه کن")
    assert app.panel.user(account.username).data_limit == 60 * GB
    assert "60 گیگ" in admin.last_text
    await tap_starting(admin, "📦 افزودن حجم")
    await admin.send("10")
    await tap_starting(admin, "✅ بله، اضافه کن")
    assert app.panel.user(account.username).data_limit == 70 * GB, "a second add stacks too"


@smoke("۸.۷")
async def test_cutting_a_service_disables_it_on_the_panel_and_deletes_nothing(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    account = await buy_active(app, store, ali)
    expire, limit, token = account.expire, account.data_limit, account.token
    admin = app.admin()
    await open_service(admin, "ali")
    await tap_starting(admin, "⛔️ قطع سرویس")
    assert app.panel.user(account.username).status == "active", "the first tap only asks"
    await tap_starting(admin, "بله، قطع کن")
    live = app.panel.user(account.username)
    assert live.status == "disabled"
    assert (live.expire, live.data_limit, live.token) == (expire, limit, token)
    assert not app.panel.requests_to("DELETE /api/user/{username}")
    assert "⛔️ قطع‌شده" in admin.last_text


@smoke("۸.۸")
async def test_restoring_a_cut_service_brings_back_the_same_service_and_link(
    app: App, store: Store
) -> None:
    """STALE: there is no «✅ سرویس برگشت» button (that is the outage notice); the toggle is
    «✅ فعال‌سازی سرویس» and a second confirm."""
    ali = app.customer("ali")
    account = await buy_active(app, store, ali)
    token = account.token
    admin = app.admin()
    await open_service(admin, "ali")
    await tap_starting(admin, "⛔️ قطع سرویس")
    await tap_starting(admin, "بله، قطع کن")
    await tap_starting(admin, "✅ فعال‌سازی سرویس")
    assert app.panel.user(account.username).status == "disabled", "the first tap only asks"
    await tap_starting(admin, "بله، فعال کن")
    live = app.panel.user(account.username)
    assert (
        live.status == "active" and live.token == token and list(app.panel.users) == [live.username]
    )
    assert "✅ فعال" in admin.last_text


# ----------------------------------------------------------------- what an unused day is worth


def _value_line(text: str) -> str | None:
    return next((ln for ln in text.splitlines() if "ارزش روزهای باقی" in ln), None)


@smoke("۸.۱۵")
async def test_a_paid_service_shows_what_its_unused_days_are_worth_rounded_down(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    account = await buy_active(app, store, ali)  # 150 000 for 30 days
    expire_in(account, days=10, hours=20)
    admin = app.admin()
    await open_service(admin, "ali")
    assert "50,000 تومان" in admin.last_text, admin.last_text
    assert "(150,000 پرداختی ÷ 30 روز × 10 روز مانده)" in admin.last_text
    expire_in(account, days=10, hours=3)
    await tap_starting(admin, "⬅️ سرویس")
    await admin.tap(next(b for b in admin.buttons if b.startswith("🟢")))
    assert "50,000 تومان" in admin.last_text, "10 days and 3 hours are worth 10 days, not 11"


@smoke("۸.۱۵")
async def test_a_trial_and_an_expired_service_show_no_value_line(app: App, store: Store) -> None:
    ali = app.customer("ali")
    await ali.start()
    await take_trial(ali)
    trial = app.panel.only_user()
    admin = app.admin()
    await open_service(admin, "ali")
    assert _value_line(admin.last_text) is None, admin.last_text

    bob = app.customer("bob")
    paid = await buy_active(app, store, bob)
    expire_in(paid, days=-2)
    paid.status = "expired"
    await open_service(admin, "bob")
    assert _value_line(admin.last_text) is None, admin.last_text
    assert trial.username != paid.username


# ------------------------------------------------------------------------------- ban


@smoke("۸.۲۰")
async def test_bot_only_ban_asks_first_closes_the_bot_and_leaves_the_vpn_alone(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    account = await buy_active(app, store, ali)
    admin = app.admin()
    await open_customer_card(admin, "ali")
    assert "🚫 مسدود کردن" in admin.buttons
    await tap_starting(admin, "🚫 مسدود کردن")
    assert admin.last_text.startswith("🚫 مسدود کردن ali؟"), admin.last_text
    assert not (await db_user(app, ali)).is_banned, "asking changes nothing"
    await tap_starting(admin, "مسدود کردن (فقط ربات)")
    assert admin.last_text.startswith("👤 ali") and "🚫 مسدود" in admin.last_text
    assert "✅ رفع مسدودی" in admin.buttons
    assert (await db_user(app, ali)).is_banned
    assert app.panel.user(account.username).status == "active", "their VPN still works"
    mark = ali.mark()
    await ali.send(ACCOUNT)
    assert report_plain(texts.banned) in ali.texts_since(mark)[0], ali.texts_since(mark)


@smoke("۸.۲۱")
async def test_ban_with_service_cut_disables_every_service_and_unban_restores_them(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    first = await buy_active(app, store, ali, "special_50", "ali12")
    second = await buy_active(app, store, ali, "economy_50", "ali34")
    admin = app.admin()

    await open_customer_card(admin, "ali")
    await tap_starting(admin, "🚫 مسدود کردن")
    await tap_starting(admin, "مسدود کردن (فقط ربات)")
    await tap_starting(admin, "✅ رفع مسدودی")
    await tap_starting(admin, "رفع مسدودی")
    assert not (await db_user(app, ali)).is_banned

    await tap_starting(admin, "🚫 مسدود کردن")
    await tap_starting(admin, "مسدود + قطع سرویس‌ها")
    assert (await db_user(app, ali)).is_banned
    assert {u.status for u in app.panel.users.values()} == {"disabled"}, "both services cut"
    assert {first.username, second.username} == set(app.panel.users), "and none deleted"

    await tap_starting(admin, "✅ رفع مسدودی")
    await tap_starting(admin, "رفع مسدودی + وصل سرویس‌ها")
    assert not (await db_user(app, ali)).is_banned
    assert {u.status for u in app.panel.users.values()} == {"active"}


# ----------------------------------------------------------------------------- search


@smoke("۸.۲۲")
async def test_a_config_name_a_subscription_link_and_an_order_number_find_the_same_customer(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    bob = app.customer("bob")
    ali_account = await buy_active(app, store, ali, "special_50", "ali12")
    await buy_active(app, store, bob, "economy_50", "bobo1")
    ali_order = await app.one(
        select(Order).join(Order.user).where(Order.panel_username == ali_account.username)
    )
    admin = app.admin()
    await users_list(admin)
    queries = [
        ali_account.username,
        f"{app.panel.base_url}/sub/{ali_account.token}",
        f"#{ali_order.id}",
    ]
    await tap_starting(admin, "🔎 جستجو")
    for query in queries:
        await admin.send(query)
        assert admin.last_text.startswith("👤 ali"), (query, admin.last_text)
        assert (
            "بدون یوزرنیم • آیدی" in admin.last_text
            and "bob" not in admin.last_text.splitlines()[0]
        )
        await tap_starting(admin, "🔎 جستجوی دیگر")
    await admin.send(
        next(u for u in app.panel.users.values() if u.username != ali_account.username).username
    )
    assert admin.last_text.startswith("👤 bob"), admin.last_text


# ------------------------------------------------------------------- the service list


@smoke("۸.۲۳")
async def test_the_service_list_shows_state_days_and_volume_and_dates_are_jalali(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    live = await buy_active(app, store, ali, "special_50", "live1")
    cut = await buy_active(app, store, ali, "special_50", "cut1")
    old = await buy_active(app, store, ali, "unlimited_1", "old1")
    expire_in(live, days=12)
    cut.status = "disabled"
    old.expire = live.expire.replace(year=2025)  # long over
    admin = app.admin()
    await open_customer_card(admin, "ali")
    await tap_starting(admin, "📱 سرویس‌ها")
    labels = {b.split(" ")[1]: b for b in admin.buttons if b[:1] in "🟢🔴⛔"}
    assert labels["live1"] == "🟢 live1 • ۱۲ روز • ۵۰ گیگ", labels
    assert labels["cut1"].startswith("⛔ cut1") and "قطع" in labels["cut1"]
    assert labels["old1"].startswith("🔴 old1") and "منقضی" in labels["old1"]
    await admin.tap(labels["live1"])
    assert re.search(r"انقضا: 14\d\d/\d\d/\d\d", admin.last_text), admin.last_text
    assert not re.search(r"20\d\d-\d\d-\d\d", admin.last_text)


# ---------------------------------------------------------------------------- devices


@smoke("۸.۲۴")
async def test_devices_are_listed_then_reset_and_the_list_is_empty(app: App, store: Store) -> None:
    ali = app.customer("ali")
    account = await buy_active(app, store, ali)
    app.panel.add_hwid(account.username, "phone-1", device_os="Android")
    app.panel.add_hwid(account.username, "phone-2", device_os="iOS")
    admin = app.admin()
    await open_service(admin, "ali")
    await tap_starting(admin, "📱 دستگاه")
    assert (
        "تعداد: 2" in admin.last_text and "Android" in admin.last_text and "iOS" in admin.last_text
    )
    await tap_starting(admin, "🧹 ریست")
    assert len(app.panel.hwids[account.id]) == 2, "the first tap only asks"
    await tap_starting(admin, "🧹 بله، ریست کن")
    assert app.panel.hwids[account.id] == []
    assert "تعداد: 0" in admin.last_text
    assert "🧹 ریست دستگاه‌ها" not in admin.buttons, "nothing left to reset"


# ----------------------------------------------------------------------- link and QR


@smoke("۸.۲۵")
async def test_the_qr_goes_to_the_owner_and_send_gives_the_customer_the_full_card(
    app: App, store: Store
) -> None:
    app.tg.accept(
        "wrong_media"
    )  # «⬅️ لینک و QR» under the QR photo tries edit_text, then sends anew
    ali = app.customer("ali")
    account = await buy_active(app, store, ali)
    admin = app.admin()
    await open_service(admin, "ali")
    await tap_starting(admin, "🔗 لینک و QR")
    link = f"{app.panel.base_url}/sub/{account.token}"
    assert link in admin.last_text
    mark_ali, mark_admin = ali.mark(), admin.mark()
    await tap_starting(admin, "QR برای من")
    qr = [m for m in admin.messages_since(mark_admin) if m.kind == "photo"]
    assert len(qr) == 1 and (qr[0].file_bytes or b"").startswith(b"\x89PNG")
    assert ali.messages_since(mark_ali) == [], "the customer saw nothing of it"
    await tap_starting(admin, "⬅️ لینک و QR")
    await tap_starting(admin, "بفرست برای مشتری")
    card = [m for m in ali.messages_since(mark_ali) if m.kind == "photo"]
    assert len(card) == 1 and link in card[0].text and account.username in card[0].text
    assert admin.popup == texts.admin_service_link_sent


@smoke("۸.۲۶")
async def test_a_new_link_warns_first_then_replaces_the_old_one(app: App, store: Store) -> None:
    ali = app.customer("ali")
    account = await buy_active(app, store, ali)
    old = account.token
    admin = app.admin()
    await open_service(admin, "ali")
    await tap_starting(admin, "🔗 لینک و QR")
    await tap_starting(admin, "♻️ لینک جدید")
    assert "لینک قبلی از همین لحظه کار نمی‌کنه" in admin.last_text
    assert app.panel.user(account.username).token == old, "the warning changed nothing"
    await tap_starting(admin, "بله، لینک جدید")
    new = app.panel.user(account.username).token
    assert new != old and all(u.token != old for u in app.panel.users.values())
    assert f"/sub/{new}" in admin.last_text and old not in admin.last_text
    assert report_plain(texts.admin_service_link_revoked) in admin.last_text


# ------------------------------------------------------------------------- gifts


async def _to_gift_confirm(admin, plan_title: str, name: str) -> None:
    await tap_starting(admin, "ساخت سرویس")
    await tap_starting(admin, "حجمی / ویژه")
    await tap_starting(admin, plan_title.split(" /", maxsplit=1)[0])
    await admin.send(name)


async def _today_sales(admin) -> str:
    await admin.send("/admin")
    await tap_starting(admin, "فروش و سفارش")
    await tap_starting(admin, "گزارش مالی")
    return next(ln for ln in admin.last_text.splitlines() if ln.startswith("امروز"))


@smoke("۸.۲۷")
async def test_a_free_gift_service_reaches_the_customer_and_is_not_a_sale(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await ali.start()
    admin = app.admin()
    before = await _today_sales(admin)
    await open_customer_card(admin, "ali")
    mark = ali.mark()
    await _to_gift_confirm(admin, store.plans["special_50"].title, "gift1")
    await tap_starting(admin, "🎁 رایگان")
    delivered = [m for m in ali.messages_since(mark) if m.kind == "photo"]
    assert len(delivered) == 1 and "gift1_" in delivered[0].text
    account = app.panel.only_user()
    assert account.username.startswith("gift1_") and account.data_limit == 50 * GB
    assert await _today_sales(admin) == before, "a giveaway is not a sale"
    order = await app.latest_order(ali)
    assert order.price_toman == 0 and order.status == OrderStatus.ACTIVE
    assert await app.wallet_balance(ali) == 0


@smoke("۸.۲۸")
async def test_a_wallet_gift_charges_once_and_a_second_tap_builds_nothing(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await ali.start()
    await credit_by_service(app, ali, 500_000)
    admin = app.admin()
    await open_customer_card(admin, "ali")
    await _to_gift_confirm(admin, store.plans["special_50"].title, "wal1")
    screen = admin.last_message
    cb = next(
        b.callback_data
        for row in screen.markup.inline_keyboard
        for b in row
        if b.text.startswith("💳 از کیف پول")
    )
    await admin.press(cb, message=screen)
    await admin.press(cb, message=screen)
    assert await app.wallet_balance(ali) == 500_000 - 150_000, "charged once"
    assert len(app.panel.users) == 1, "the second tap built no second service"
    orders = await app.all(
        select(Order).where(Order.panel_username == app.panel.only_user().username)
    )
    assert len(orders) == 1


# ------------------------------------------------------------------ expiring list


def _listed(admin) -> list[str]:
    """People on the list screen (the active filter's own button is not a person)."""
    return [row_name(n) for n in list_names(admin) if not n.startswith("●")]


@smoke("۸.۳۱")
async def test_the_expiring_filter_lists_only_services_ending_within_a_week(
    app: App, store: Store
) -> None:
    soon, later, none = (app.customer(n) for n in ("soon", "later", "never"))
    soon_account = await buy_active(app, store, soon, "special_50", "soon1")
    await buy_active(app, store, later, "special_50", "late1")
    await none.start()
    soon_order = await app.latest_order(soon)
    await app.age(Order, soon_order.id, days=25, columns=("expires_at",))  # five days left
    admin = app.admin()
    await users_list(admin)
    await press_on_last(admin, FILTER[UserFilter.EXPIRING])
    assert _listed(admin) == ["soon"], admin.rows

    # An owner's manual extension takes the customer off the list.
    await open_service(admin, "soon")
    await tap_starting(admin, "📅 تمدید")
    await admin.send("30")
    await users_list(admin)
    await press_on_last(admin, FILTER[UserFilter.EXPIRING])
    assert _listed(admin) == [], admin.rows
    assert soon_account.username in app.panel.users

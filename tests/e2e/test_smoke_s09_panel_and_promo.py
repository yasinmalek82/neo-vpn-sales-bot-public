"""Section ۹, part 2: panel groups, moving services, deleting a plan, promo codes.

Rows ۹.۱۲ ۹.۱۲الف ۹.۱۲ب ۹.۱۳ ۹.۱۴ ۹.۱۵.
STALE: ۹.۱۲ names the field `panel_group_ids`; it is now a picker of the panel's own groups under
«🖥 پنل» on the group dashboard. ۹.۱۲ب: a service that switched to another product is not even
counted in the old group (it belongs by its latest active order), so «دست نخورده» is shown as
0 and the proof is that the panel was never written for it.
"""

from __future__ import annotations

import pytest

from tests.e2e._s01_helpers import card_order_from_summary, pay_and_approve
from tests.e2e._s07b_helpers import approve_order, tap_starting
from tests.e2e._s08_helpers import buy_active, expire_in, my_service
from tests.e2e._s09_helpers import (
    live_plans,
    make_group,
    new_plan,
    open_store,
    set_panel_group,
    shelf,
    skip_promo_rules,
)
from tests.e2e.harness import App, Store, digits, publish_store, smoke


@pytest.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


async def _open_group(admin, *path: str) -> None:  # type: ignore[no-untyped-def]
    await open_store(admin)
    for title in path:
        await tap_starting(admin, title)


async def _switch_to_economy(app: App, store: Store, customer) -> None:  # type: ignore[no-untyped-def]
    """«سرویس‌های من» → تمدید → «🔁 پلن دیگه روی همین سرویس» → اقتصادی → pay → approve."""
    await my_service(customer)
    await tap_starting(customer, "تمدید این سرویس")
    await tap_starting(customer, "بدون کد")
    await tap_starting(customer, "🔁 پلن دیگه روی همین سرویس")
    await tap_starting(customer, store.groups["volume"].title)
    await tap_starting(customer, store.groups["economy"].title)
    await tap_starting(customer, store.plans["economy_50"].title)
    await tap_starting(customer, "بدون کد")
    await tap_starting(customer, "ثبت سفارش")
    order = await app.latest_order(customer)
    await customer.send_photo()
    await approve_order(app, order.id)


async def _repoint(admin, *, drop: str, pick: str) -> None:  # type: ignore[no-untyped-def]
    """On a group dashboard: «🖥 پنل» → «🔗 گروه پنل» → untick one panel group, tick another, save."""
    await tap_starting(admin, "🖥 پنل")
    await tap_starting(admin, "🔗 گروه پنل")
    await tap_starting(admin, f"✅ {drop}")
    await tap_starting(admin, f"▫️ {pick}")
    await tap_starting(admin, "💾 ذخیره")


@smoke("۹.۱۲")
async def test_a_plan_bought_in_a_group_lands_on_the_panel_group_the_owner_picked(
    app: App,
) -> None:
    admin = app.admin()
    await open_store(admin)
    await make_group(admin, "پریمیوم")
    await set_panel_group(admin, "premium • #3")
    await new_plan(admin, "۵۰ گیگ", "۳۰ روز", "نامحدود", 200_000)
    await tap_starting(admin, "✅ تأیید و فعال")
    ali = app.customer("ali")
    await ali.start()
    labels = await shelf(ali, "پریمیوم")
    plan_button = next(b for b in labels if "۵۰ گیگ" in b)
    await tap_starting(ali, plan_button)
    order = await card_order_from_summary(ali)
    await pay_and_approve(app, ali, order)
    account = app.panel.only_user()
    assert account.group_ids == [3], "the panel group the owner ticked, nothing else"
    assert account.data_limit == 50 * 1024**3


@smoke("۹.۱۲الف")
async def test_changing_the_panel_group_of_a_group_with_services_asks_to_move_them(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await buy_active(app, store, ali)
    admin = app.admin()
    await _open_group(admin, "حجمی", "ویژه")
    await _repoint(admin, drop="tunnel • #2", pick="premium • #3")
    assert "1 سرویس در این گروه هست" in admin.last_text, admin.last_text
    assert "منتقلشون کنم" in admin.last_text
    assert "✅ منتقل کن" in admin.buttons and "نه، فقط سرویس‌های جدید" in admin.buttons
    assert app.panel.only_user().group_ids == [2], "nothing moved before the owner agrees"


@smoke("۹.۱۲الف")
async def test_a_group_without_services_just_saves_without_the_question(
    app: App, store: Store
) -> None:
    admin = app.admin()
    await _open_group(admin, "حجمی", "ویژه")
    await _repoint(admin, drop="tunnel • #2", pick="premium • #3")
    assert "منتقلشون کنم" not in admin.last_text
    assert "✅ ذخیره شد" in admin.last_text


@smoke("۹.۱۲ب")
async def test_moving_reports_the_counts_and_leaves_a_switched_service_alone(
    app: App, store: Store
) -> None:
    ali, sara, bob = app.customer("ali"), app.customer("sara"), app.customer("bob")
    stay = (await buy_active(app, store, ali, "special_50", "stay12")).username
    switched = (await buy_active(app, store, sara, "special_50", "gone12")).username
    moved_too = (await buy_active(app, store, bob, "special_50", "bob12")).username
    # Sara moves her service to another product (the real switch), so it leaves «ویژه».
    expire_in(app.panel.user(switched), days=10)  # an older service, not one bought a minute ago
    await _switch_to_economy(app, store, sara)
    assert app.panel.user(switched).group_ids == [3]
    writes_before = len(app.panel.requests_to("PUT /api/user/{username}"))
    # The owner points «ویژه» somewhere else and agrees to move what is on it.
    admin = app.admin()
    await _open_group(admin, "حجمی", "ویژه")
    await _repoint(admin, drop="tunnel • #2", pick="direct • #1")
    assert "2 سرویس در این گروه هست" in admin.last_text, admin.last_text
    await tap_starting(admin, "✅ منتقل کن")
    assert "منتقل‌شده: 2" in admin.last_text and "ناموفق: 0" in admin.last_text, admin.last_text
    assert app.panel.user(stay).group_ids == [1] and app.panel.user(moved_too).group_ids == [1]
    assert app.panel.user(switched).group_ids == [3], "the switched service was dragged back"
    assert len(app.panel.requests_to("PUT /api/user/{username}")) == writes_before + 2


@smoke("۹.۱۳")
async def test_deleting_a_plan_asks_twice_and_the_customer_stops_seeing_it(
    app: App, store: Store
) -> None:
    admin = app.admin()
    await _open_group(admin, "حجمی", "ویژه")
    await tap_starting(admin, "۵۰ گیگ")
    await tap_starting(admin, "🗑 حذف این پلن")
    assert "حذف پلن" in admin.last_text and "برداشته می‌شود" in admin.last_text
    await tap_starting(admin, "انصراف")  # the second question can be answered no
    assert "۵۰ گیگ / ۳۰ روز" in await live_plans(app, "ویژه")
    await tap_starting(admin, "🗑 حذف این پلن")
    await tap_starting(admin, "🗑 بله، حذف کن")
    assert "پلن «50 گیگ / 30 روز» حذف شد" in admin.last_text
    assert await live_plans(app, "ویژه") == ["۱۰۰ گیگ / ۹۰ روز"]
    ali = app.customer("ali")
    await ali.start()
    labels = await shelf(ali, "حجمی", "ویژه")
    assert not any("۵۰ گیگ" in b for b in labels), labels
    assert "۵۰ گیگ / ۳۰ روز" not in await live_plans(app, "ویژه")


async def _create_code(app: App, code_line: str) -> None:
    admin = app.admin()
    await open_store(admin)
    await tap_starting(admin, "🏷")
    await tap_starting(admin, "🏷 مدیریت")
    await tap_starting(admin, "➕")
    await admin.send(code_line)
    await skip_promo_rules(admin)


@smoke("۹.۱۴")
async def test_a_new_promo_code_takes_its_percent_off_the_purchase(app: App, store: Store) -> None:
    await _create_code(app, "SAVE15 15")
    ali = app.customer("ali")
    await ali.start()
    from tests.e2e.harness import browse_to_plan

    await browse_to_plan(ali, store, "special_50")
    await ali.send("ali12")
    await ali.send("save15")  # the code is not case sensitive
    price = store.plans["special_50"].price
    expected = price - price * 15 // 100
    assert expected == 127_500
    assert str(expected) in digits(ali.last_text), ali.last_text
    await ali.tap("ثبت سفارش")
    order = await app.latest_order(ali)
    assert order.price_toman == expected, order.price_toman


@smoke("۹.۱۵")
async def test_a_wrong_discount_entry_gets_an_exact_persian_message(app: App, store: Store) -> None:
    await _create_code(app, "SAVE15 15")
    admin = app.admin()
    await tap_starting(admin, "➕")
    await admin.send("SAVE15 abc")
    assert "invalid literal" not in admin.last_text and "ValueError" not in admin.last_text
    assert "درصد تخفیف نامعتبر است" in admin.last_text and "1 تا 100" in admin.last_text
    await admin.send("SAVE15 150")
    assert "invalid literal" not in admin.last_text
    assert "بین 0 و 100" in admin.last_text
    from tests.e2e.harness import browse_to_plan

    ali = app.customer("ali")
    await ali.start()
    await browse_to_plan(ali, store, "special_50")
    await ali.send("ali12")
    await ali.send("SAVE15 abc")
    assert "invalid literal" not in ali.last_text
    assert "این کد تخفیف معتبر نیست" in ali.last_text
    assert "ثبت سفارش" not in " ".join(ali.buttons), "a wrong code must not reach the summary"


@smoke("۹.۱۲ب")
async def test_a_same_size_switch_to_another_group_moves_the_panel_account(
    app: App, store: Store
) -> None:
    sara = app.customer("sara")
    name = (await buy_active(app, store, sara, "special_50", "gone12")).username
    assert app.panel.user(name).group_ids == [2]
    await _switch_to_economy(app, store, sara)
    assert app.panel.user(name).group_ids == [3]

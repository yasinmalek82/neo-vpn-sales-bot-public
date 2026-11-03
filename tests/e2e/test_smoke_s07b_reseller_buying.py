"""Section ۷ب, part 2: a reseller buys at the agreed price, for their own customers.

Rows: ۷ب.۵ and its letters, ۷ب.۶, ۷ب.۷, ۷ب.۹, ۷ب.۱۰. The reseller is made through the
admin's customer card; prices are typed into the price panel.
"""

from __future__ import annotations

import re

import pytest

from bot.keyboards.reply_menu import BUY
from bot.texts import texts
from database.models import OrderStatus
from tests.e2e._s07b_helpers import (
    RETAIL_CUSTOM_50GB_30D,
    add_custom_volume,
    buy_ready,
    deliver_by_card,
    make_shop,
    tap_starting,
)
from tests.e2e.harness import GB, App, Store, digits, plain, publish_store, smoke
from tests.e2e.harness.expectations import final_amount_line

GROUP = "حجمی/ویژه"
PLAN = "۵۰ گیگ / ۳۰ روز"
RETAIL = 150_000
AGREED = 100_000


@pytest.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


async def _open_special_shelf(shop) -> None:  # type: ignore[no-untyped-def]
    await shop.tap_reply(BUY)
    await shop.tap("حجمی")
    await shop.tap("ویژه")


@smoke("۷ب.۵", "۷ب.۵الف", "۷ب.۵ج", "۷ب.۵ج۲", "۷ب.۵د", "۷ب.۵ه", "۷ب.۵و")
async def test_the_agreed_price_and_the_two_name_steps_run_through_to_the_panel(
    app: App, store: Store
) -> None:
    shop, _ = await make_shop(app, prices=(("ویژه", PLAN, AGREED),))

    # ۷ب.۵: the shelf shows the agreed price on the button, not retail.
    await _open_special_shelf(shop)
    assert plain(texts.reseller_choose_plan) == shop.last_text
    plan_button = next(b for b in shop.buttons if b.startswith(PLAN))
    assert str(AGREED) in digits(plan_button) and str(RETAIL) not in digits(plan_button)
    await tap_starting(shop, PLAN)

    # ۷ب.۵ج: «who is this service for?», with both example lists.
    assert shop.last_text == plain(texts.reseller_label_name_prompt)
    for example in ("ali12", "mahdi", "ws1_ksdf", "محسن", "علی"):
        assert example in shop.last_text
    # ۷ب.۵ج۲: a Persian name is refused and the examples come back.
    mark = shop.mark()
    await shop.send("محسن")
    assert shop.texts_since(mark) == [plain(texts.reseller_label_name_invalid)]
    assert "ws1_ksdf" in shop.last_text
    # ۷ب.۵د: the name is cleaned, and the phone is asked separately with a skip button.
    await shop.send("Reza Mohammadi")
    assert shop.last_text == plain(texts.reseller_label_phone_prompt.format(name="reza_mohammadi"))
    assert shop.buttons == [texts.reseller_label_skip_phone]
    # ۷ب.۵ه: the confirmation page names the customer, not «نام سرویس».
    await shop.send("0912-345-6789")
    assert "مشتری: reza_mohammadi | 09123456789" in shop.last_text
    assert "نام سرویس" not in shop.last_text
    assert str(AGREED) in digits(final_amount_line(shop.last_text))
    await shop.tap("✅ ثبت سفارش")

    # ۷ب.۵الف: the amount taken is the amount on the button.
    order = await app.latest_order(shop)
    assert order.price_toman == AGREED
    assert str(AGREED) in digits(shop.last_text)
    await deliver_by_card(app, shop, order.id)
    assert (await app.latest_order(shop)).status == OrderStatus.ACTIVE

    # ۷ب.۵و: the panel username is the customer's name plus four characters, no phone digits.
    account = app.panel.only_user()
    assert re.fullmatch(r"reza_mohammadi_[a-z0-9]{4}", account.username), account.username
    assert "0912" not in account.username and "6789" not in account.username
    assert account.data_limit == 50 * GB


@smoke("۷ب.۵ز", "۷ب.۱۰")
async def test_an_ordinary_customer_still_gets_the_one_step_name_and_the_retail_price(
    app: App, store: Store
) -> None:
    await make_shop(app, prices=(("ویژه", PLAN, AGREED),))
    ali = app.customer("ali")
    await ali.start()
    await _open_special_shelf(ali)
    plan_button = next(b for b in ali.buttons if b.startswith(PLAN))
    assert str(RETAIL) in digits(plan_button) and str(AGREED) not in digits(plan_button)
    await tap_starting(ali, PLAN)
    assert ali.last_text == plain(texts.purchase_enter_service_name)  # one step, no phone
    await ali.send("ali12")
    await ali.tap("🎫 بدون کد تخفیف")
    assert str(RETAIL) in digits(final_amount_line(ali.last_text))
    await ali.tap("✅ ثبت سفارش")
    order = await app.latest_order(ali)
    assert order.price_toman == RETAIL
    await deliver_by_card(app, ali, order.id)
    assert re.fullmatch(r"ali12_[a-z0-9]{4}", app.panel.only_user().username)


@smoke("۷ب.۵ح", "۷ب.۵ط")
async def test_a_second_customer_on_the_same_plan_is_a_new_service_not_a_renewal(
    app: App, store: Store
) -> None:
    shop, _ = await make_shop(app, prices=(("ویژه", PLAN, AGREED),))
    first = await buy_ready(shop, app, GROUP, PLAN, "Reza Mohammadi", "09123456789")
    await deliver_by_card(app, shop, first)
    reza = app.panel.only_user()
    reza_expire, reza_limit = reza.expire, reza.data_limit

    # ۷ب.۵ح: the same plan again goes straight to «who is this for?».
    await _open_special_shelf(shop)
    await tap_starting(shop, PLAN)
    assert shop.last_text == plain(texts.reseller_label_name_prompt)
    assert "تمدید" not in shop.last_text
    await shop.send("Ali")
    await shop.tap(texts.reseller_label_skip_phone)
    await shop.tap("✅ ثبت سفارش")
    second = (await app.latest_order(shop)).id
    assert second != first
    await deliver_by_card(app, shop, second)

    # ۷ب.۵ط: the first customer's service is untouched; the second has its own username.
    assert sorted(u.username.rsplit("_", 1)[0] for u in app.panel.users.values()) == [
        "ali",
        "reza_mohammadi",
    ]
    assert app.panel.user(reza.username).expire == reza_expire
    assert app.panel.user(reza.username).data_limit == reza_limit
    assert app.panel.requests_to("PUT /api/user/{username}") == []
    assert len(app.panel.requests_to("POST /api/user")) == 2


@smoke("۷ب.۵ی")
async def test_renewing_from_a_customers_card_does_not_ask_the_name_again(
    app: App, store: Store
) -> None:
    shop, _ = await make_shop(app, prices=(("ویژه", PLAN, AGREED),))
    first = await buy_ready(shop, app, GROUP, PLAN, "Reza Mohammadi", "09123456789")
    await deliver_by_card(app, shop, first)
    account = app.panel.only_user()
    days_before = account.days_left() or 0

    await shop.tap_reply("👥 مشتری‌های من")
    await tap_starting(shop, "🟢 reza_mohammadi")
    await shop.tap("📅 تمدید این سرویس")
    # Straight to the summary at the agreed price: no name, no phone.
    assert shop.last_text.startswith("🧾")
    assert str(AGREED) in digits(shop.last_text)
    await shop.tap("✅ ثبت سفارش")
    renewal = await app.latest_order(shop)
    assert renewal.id != first and renewal.price_toman == AGREED
    await deliver_by_card(app, shop, renewal.id)

    assert len(app.panel.users) == 1
    assert (app.panel.user(account.username).days_left() or 0) > days_before + 25
    await shop.tap_reply("👥 مشتری‌های من")
    assert shop.buttons[0] == "🟢 reza_mohammadi | 09123456789", shop.buttons


@smoke("۷ب.۵ب")
async def test_a_plan_without_an_agreed_price_is_sold_at_retail(app: App, store: Store) -> None:
    shop, _ = await make_shop(app, prices=(("ویژه", PLAN, AGREED),))
    await shop.tap_reply(BUY)
    await shop.tap("حجمی")
    await shop.tap("اقتصادی")
    button = next(b for b in shop.buttons if b.startswith(PLAN))
    assert str(store.plans["economy_50"].price) in digits(button)
    await tap_starting(shop, PLAN)
    await shop.send("Omid")
    await shop.tap(texts.reseller_label_skip_phone)
    await shop.tap("✅ ثبت سفارش")
    assert (await app.latest_order(shop)).price_toman == store.plans["economy_50"].price


@smoke("۷ب.۶")
async def test_custom_volume_costs_gb_times_rate_plus_days_times_rate(
    app: App, store: Store
) -> None:
    await add_custom_volume(app, store)
    shop, _ = await make_shop(app, rates=(1200, 500))
    await _open_special_shelf(shop)
    await shop.tap("🧩 خرید سفارشی")
    await shop.tap("۵۰ گیگ")
    await shop.tap("۱ ماه")
    await shop.send("Reza")
    await shop.tap(texts.reseller_label_skip_phone)
    assert "75000" in digits(final_amount_line(shop.last_text))
    await shop.tap("✅ ثبت سفارش")
    order = await app.latest_order(shop)
    assert order.price_toman == 50 * 1200 + 30 * 500 == 75_000
    assert order.price_toman < RETAIL_CUSTOM_50GB_30D
    await deliver_by_card(app, shop, order.id)
    account = app.panel.only_user()
    assert account.data_limit == 50 * GB
    assert 29.9 < (account.days_left() or 0) <= 30


@smoke("۷ب.۷")
async def test_a_rate_that_crosses_retail_charges_retail_and_warns_the_owner(
    app: App, store: Store
) -> None:
    await add_custom_volume(app, store)
    group = await app.connect_report_group()
    shop, _ = await make_shop(app, rates=(1200, 500))
    await _open_special_shelf(shop)
    await shop.tap("🧩 خرید سفارشی")
    await shop.tap("۵۰ گیگ")
    await shop.tap("۱۲ ماه")
    await shop.send("Reza")
    await shop.tap(texts.reseller_label_skip_phone)
    await shop.tap("✅ ثبت سفارش")
    order = await app.latest_order(shop)
    shop_formula = 50 * 1200 + 365 * 500  # what the shop's own rates would charge
    assert shop_formula == 242_500
    retail = 225_000  # 50 GB at 6000, less 25% for a year (see CUSTOM_PRICING)
    assert order.price_toman == retail < shop_formula
    await app.run_job("report_flush")
    warnings = [t for t in await group.texts("errors") if "قیمت نمایندگی از خرده بالاتر زد" in t]
    assert len(warnings) == 1, warnings
    assert "225000" in digits(warnings[0]) and "242500" in digits(warnings[0])


@smoke("۷ب.۹")
async def test_a_ready_plan_priced_above_retail_charges_retail_and_warns(
    app: App, store: Store
) -> None:
    group = await app.connect_report_group()
    shop, _ = await make_shop(app, prices=(("ویژه", PLAN, 200_000),))
    order_id = await buy_ready(shop, app, GROUP, PLAN, "Reza", None)
    order = await app.latest_order(shop)
    assert order.id == order_id and order.price_toman == RETAIL
    await app.run_job("report_flush")
    warnings = [t for t in await group.texts("errors") if "قیمت نمایندگی از خرده بالاتر زد" in t]
    assert len(warnings) == 1, warnings
    assert "200000" in digits(warnings[0]) and "150000" in digits(warnings[0])

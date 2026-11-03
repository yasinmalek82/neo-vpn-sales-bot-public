"""Section 5: unlimited and custom purchases.

Rows ۵.۱ ۵.۲ ۵.۳ ۵.۴ ۵.۵ ۵.۶.
STALE: ۵.۱ names «♾️ پلن نامحدود» and «۲ کاربر همزمان»; the shelf is now the group «نامحدود»
with ready «۲ کاربره / ۱ ماه» plans, and «🧩 خرید سفارشی» is the builder with the same questions.
Both are tested. ۵.۴ and ۵.۵ are about the owner's «➕ پلن جدید» size prompt (the
only place a volume is typed); ۵.۶ names «⏱ با محدودیت زمانی / 🔓 بدون محدودیت زمانی»,
retired: the group's duration tabs («۳ ماهه», «بدون انقضا») replace it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from bot.keyboards.purchase import PurchaseCB
from bot.keyboards.reply_menu import ACCOUNT
from bot.texts import texts
from database.models import Order, OrderStatus
from tests.e2e._s01_helpers import (
    card_order_from_summary,
    open_shelf,
    open_volume_wizard,
    pay_and_approve,
)
from tests.e2e._s07b_helpers import (
    RETAIL_CUSTOM_50GB_30D,
    RETAIL_CUSTOM_50GB_365D,
    add_custom_volume,
    tap_starting,
)
from tests.e2e.harness import GB, App, Store, digits, plain, publish_store, smoke

UNLIMITED_2_READY = 400_000


@pytest.fixture
async def store(app: App) -> Store:
    s = await publish_store(app)
    await add_custom_volume(app, s)
    return s


@smoke("۵.۱", "۵.۲")
async def test_a_two_user_unlimited_plan_is_priced_and_gives_the_panel_two_devices(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await ali.start()
    await open_shelf(ali, "نامحدود")
    assert str(UNLIMITED_2_READY) in digits(" ".join(ali.buttons))
    await tap_starting(ali, "۲ کاربره")
    await ali.send("ali12")
    await ali.tap("بدون کد تخفیف")
    assert "کاربر همزمان: 2" in ali.last_text and "1 ماه" in ali.last_text
    assert str(UNLIMITED_2_READY) in digits(ali.last_text)
    await ali.tap("ثبت سفارش")
    order = await app.latest_order(ali)
    assert order.price_toman == UNLIMITED_2_READY
    await pay_and_approve(app, ali, order)
    account = app.panel.only_user()
    assert account.hwid_limit == 2, "the concurrent-device limit is the point of this bot"
    assert account.data_limit is None  # unlimited data
    assert 29.9 < (account.days_left() or 0) <= 30
    assert account.group_ids == store.groups["unlimited"].panel_groups


@smoke("۵.۱", "۵.۲")
async def test_the_unlimited_group_has_no_builder_and_a_forged_press_is_refused(
    app: App, store: Store
) -> None:
    """Unlimited is sold by users and months: no گیگ/ماه builder for anyone.

    It was a builder with its own price (`UNLIMITED_2_BUILDER`, a second user on
    the retail curve). The owner decided custom purchase makes no sense there
    (plans/2026-10-04, ۱.۱), so a stale or forged press is refused like any
    custom press nobody is offered.
    """
    ali = app.customer("ali")
    await ali.start()
    await open_shelf(ali, "نامحدود")
    assert not any(b.startswith("🧩") for b in ali.buttons), ali.buttons
    assert str(UNLIMITED_2_READY) in digits(" ".join(ali.buttons))  # the ready plans stay
    await ali.press(PurchaseCB.CUSTOM)
    assert ali.last_text == plain(texts.purchase_custom_not_offered), ali.last_text
    assert not await app.all(select(Order))


async def _custom_50gb(ali, *, duration: str) -> None:
    await open_shelf(ali, "حجمی", "ویژه")
    await tap_starting(ali, "🧩")
    await tap_starting(ali, "۵۰ گیگ")
    await tap_starting(ali, duration)


@smoke("۵.۳")
async def test_a_custom_50_gb_month_is_priced_by_the_tier_and_built_on_the_panel(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await ali.start()
    await _custom_50gb(ali, duration="۱ ماه")
    await ali.send("ali12")
    await ali.tap("بدون کد تخفیف")
    assert "50 گیگ" in ali.last_text and "1 ماه" in ali.last_text
    assert str(RETAIL_CUSTOM_50GB_30D) in digits(ali.last_text)  # 50 GB x 6 000
    await ali.tap("ثبت سفارش")
    order = await app.latest_order(ali)
    assert order.price_toman == RETAIL_CUSTOM_50GB_30D
    await pay_and_approve(app, ali, order)
    account = app.panel.only_user()
    assert account.data_limit == 50 * GB
    assert 29.9 < (account.days_left() or 0) <= 30
    assert account.hwid_limit == 0  # «نامحدود», never one device
    assert account.group_ids == store.groups["special"].panel_groups


@smoke("۵.۳")
async def test_a_custom_year_gets_the_long_service_discount(app: App, store: Store) -> None:
    ali = app.customer("ali")
    await ali.start()
    await _custom_50gb(ali, duration="۱۲ ماه")
    await ali.send("ali12")
    await ali.tap("بدون کد تخفیف")
    assert str(RETAIL_CUSTOM_50GB_365D) in digits(ali.last_text), ali.last_text
    await ali.tap("ثبت سفارش")
    order = await app.latest_order(ali)
    assert order.price_toman == RETAIL_CUSTOM_50GB_365D
    assert order.discount_toman == RETAIL_CUSTOM_50GB_30D - RETAIL_CUSTOM_50GB_365D


@smoke("۵.۳", "۵.۶")
@pytest.mark.parametrize(("tab", "days"), [("۳ ماه", 90), ("۱۲ ماه", 365)])
async def test_a_longer_custom_service_is_delivered_for_the_days_it_was_sold_for(
    app: App, store: Store, tab: str, days: int
) -> None:
    ali = app.customer("ali")
    await ali.start()
    await _custom_50gb(ali, duration=tab)
    order = await card_order_from_summary(ali)
    await pay_and_approve(app, ali, order)
    assert days - 1 < (app.panel.only_user().days_left() or 0) <= days


async def _volume_prompt(app: App) -> tuple:
    admin = app.admin()
    await open_volume_wizard(admin)
    return (admin,)


@smoke("۵.۴")
async def test_a_volume_over_the_cap_is_refused_at_the_volume_question(app: App) -> None:
    await publish_store(app)
    (admin,) = await _volume_prompt(app)
    await admin.send("99999")
    assert "۲ از ۴" not in admin.last_text, "the wizard went on to the duration step"
    assert "10000" in digits(admin.last_text) or "۱۰۰۰۰" in admin.last_text, admin.last_text


@smoke("۵.۵")
async def test_a_price_typed_at_the_volume_question_is_refused_there(app: App) -> None:
    await publish_store(app)
    (admin,) = await _volume_prompt(app)
    await admin.send("150000")
    assert "۲ از ۴" not in admin.last_text, "the wizard went on to the duration step"


@smoke("۵.۴")
async def test_an_out_of_range_duration_is_refused_at_the_duration_question(app: App) -> None:
    await publish_store(app)
    (admin,) = await _volume_prompt(app)
    await admin.send("60")
    await tap_starting(admin, "✏️ مقدار دیگر")
    await admin.send("99999")
    assert "۳ از ۴" not in admin.last_text, "the wizard went on to the device step"
    assert "3650" in digits(admin.last_text), admin.last_text
    await admin.send("45")  # still at the same question
    assert "3 از 4" in admin.last_text, admin.last_text


@smoke("۵.۴")
async def test_an_out_of_range_device_limit_is_refused_at_its_question(app: App) -> None:
    await publish_store(app)
    (admin,) = await _volume_prompt(app)
    await admin.send("60")
    await tap_starting(admin, "۳۰ روز")
    await tap_starting(admin, "✏️ مقدار دیگر")
    await admin.send("5000")
    assert "1000" in digits(admin.last_text), admin.last_text
    assert "قیمت" not in admin.last_text, "the wizard went on to the price step"


@smoke("۵.۴", "۵.۵")
async def test_a_giant_plan_is_never_created_and_the_shelf_is_untouched(
    app: App,
) -> None:
    """Refused at the question, nothing written, the shelf as it was."""
    from sqlalchemy import select

    from database.models.store import StorePackage

    store = await publish_store(app)
    before = len(await app.all(select(StorePackage)))
    admin = app.admin()
    await open_volume_wizard(admin)
    await admin.send("150000")  # a price typed as a volume
    assert admin.last_text == "حجم باید بین 1 و 10000 باشد."
    assert "۳۰ روز" not in " ".join(admin.buttons), "the wizard went on to the duration step"
    assert len(await app.all(select(StorePackage))) == before
    ali = app.customer("ali")
    await ali.start()
    await open_shelf(ali, "حجمی", "ویژه")
    assert not any("۱۵۰۰۰۰ گیگ" in b for b in ali.buttons)
    assert any(b.startswith(store.plans["special_50"].title) for b in ali.buttons)


async def _add_tabs(app: App, store: Store) -> None:
    """The owner adds a «۳ ماهه» tab and a «بدون انقضا» tab, and a plan for the latter."""
    from services.store_admin import (
        add_category_duration,
        commit_and_activate,
        create_draft_package,
    )

    async with app.session() as session:
        category = store.category_ids["special"]
        await add_category_duration(session, category, months=3)
        await add_category_duration(session, category, months=None)
        await create_draft_package(
            session,
            category_id=category,
            title_fa="۲۰ گیگ / بدون انقضا",
            fixed_price_toman=70_000,
            volume_gb=20,
        )
        await commit_and_activate(session, notes="تب مدت اضافه شد")


@smoke("۵.۶")
async def test_a_three_month_tab_buys_ninety_days_on_the_panel(app: App, store: Store) -> None:
    await _add_tabs(app, store)
    buyer = app.customer("buyer")
    await buyer.start()
    await open_shelf(buyer, "حجمی", "ویژه")
    await tap_starting(buyer, "۳ ماهه")
    await tap_starting(buyer, store.plans["special_100"].title)
    order = await card_order_from_summary(buyer, "buyer")
    assert order.delivery_duration_days == 90
    await pay_and_approve(app, buyer, order)
    assert 89.9 < (app.panel.only_user().days_left() or 0) <= 90


async def _to_no_expiry_summary(buyer, name: str) -> None:
    await open_shelf(buyer, "حجمی", "ویژه")
    await tap_starting(buyer, "بدون انقضا")
    await tap_starting(buyer, "۲۰ گیگ")
    await buyer.send(name)
    if buyer.last_text == plain(texts.purchase_renew_or_new):
        await buyer.tap("✨ سرویس جدید")
    await buyer.tap("بدون کد تخفیف")


async def _service_rows(buyer) -> list[str]:
    await buyer.tap_reply(ACCOUNT)
    return [b for b in buyer.buttons if b[:1] in "🟢🟡🔴⛔"]


@smoke("۵.۶")
async def test_a_no_expiry_tab_buys_a_service_with_no_expiry(app: App, store: Store) -> None:
    await _add_tabs(app, store)
    buyer = app.customer("buyer")
    await buyer.start()
    await open_shelf(buyer, "حجمی", "ویژه")
    await tap_starting(buyer, "بدون انقضا")
    await tap_starting(buyer, "۲۰ گیگ")
    order = await card_order_from_summary(buyer, "buyer")
    await pay_and_approve(app, buyer, order)
    assert app.panel.only_user().days_left() is None, "no expiry on the panel"


@smoke("۵.۶")
async def test_the_no_expiry_summary_says_no_expiry_and_the_sale_report_names_no_days(
    app: App, store: Store
) -> None:
    await _add_tabs(app, store)
    group = await app.connect_report_group()
    buyer = app.customer("buyer")
    await buyer.start()
    await _to_no_expiry_summary(buyer, "buyer")
    assert "مدت: بدون انقضا" in buyer.last_text, buyer.last_text
    assert "None" not in buyer.last_text and "ماه" not in buyer.last_text, buyer.last_text
    await buyer.tap("ثبت سفارش")
    order = await app.latest_order(buyer)
    await pay_and_approve(app, buyer, order)
    await app.run_job("report_flush")
    (sale,) = [t for t in await group.texts("sales") if "کد سفارش" in t]
    assert "حجم: 20 گیگ" in sale, sale
    assert "روز" not in sale, sale  # it said «مدت: ۰ روز»


@smoke("۵.۶")
async def test_a_wallet_paid_no_expiry_plan_is_delivered_at_once_with_no_expiry(
    app: App, store: Store
) -> None:
    await _add_tabs(app, store)
    buyer = app.customer("buyer")
    await buyer.start()
    await app.give_wallet(buyer, 100_000)
    await _to_no_expiry_summary(buyer, "buyer")
    await buyer.tap("ثبت سفارش")
    order = await app.latest_order(buyer)
    assert order.status == OrderStatus.ACTIVE
    assert order.price_toman == 70_000
    assert await app.wallet_balance(buyer) == 30_000
    account = app.panel.only_user()
    assert account.days_left() is None, "no expiry on the panel"
    assert account.data_limit == 20 * GB
    assert "زمان باقی‌مانده: بدون انقضا" in buyer.last_text, buyer.last_text


@smoke("۵.۶")
async def test_a_no_expiry_service_is_not_listed_as_expired(app: App, store: Store) -> None:
    await _add_tabs(app, store)
    buyer = app.customer("buyer")
    await buyer.start()
    await app.give_wallet(buyer, 100_000)
    await _to_no_expiry_summary(buyer, "buyer")
    await buyer.tap("ثبت سفارش")
    (row,) = await _service_rows(buyer)
    assert row.startswith("🟢"), row  # the order's expiry was stored as 1970
    # With the panel down the card falls back to the order: still no date in the past.
    app.panel.go_down()
    await buyer.tap(row)
    assert "منقضی" not in buyer.last_text, buyer.last_text
    assert "زمان باقی‌مانده: بدون انقضا" in buyer.last_text, buyer.last_text


@smoke("۵.۶")
async def test_renewing_a_no_expiry_service_adds_volume_and_keeps_it_without_expiry(
    app: App, store: Store
) -> None:
    await _add_tabs(app, store)
    buyer = app.customer("buyer")
    await buyer.start()
    await app.give_wallet(buyer, 140_000)
    await _to_no_expiry_summary(buyer, "buyer")
    await buyer.tap("ثبت سفارش")
    (row,) = await _service_rows(buyer)
    await buyer.tap(row)
    await tap_starting(buyer, "📅 تمدید این سرویس")
    await tap_starting(buyer, "بدون کد")
    assert "مدت: بدون انقضا" in buyer.last_text, buyer.last_text
    assert "70000" in digits(buyer.last_text)
    await buyer.tap("ثبت سفارش")
    assert await app.wallet_balance(buyer) == 0
    account = app.panel.only_user()  # the same account, no second one
    assert account.days_left() is None, "the renew gave a no-expiry service an expiry"
    assert account.data_limit == 40 * GB
    (row,) = await _service_rows(buyer)
    assert row.startswith("🟢"), row


@smoke("۵.۶")
async def test_buying_the_no_expiry_plan_again_offers_renewing_the_same_service(
    app: App, store: Store
) -> None:
    await _add_tabs(app, store)
    buyer = app.customer("buyer")
    await buyer.start()
    await app.give_wallet(buyer, 100_000)
    await _to_no_expiry_summary(buyer, "buyer")
    await buyer.tap("ثبت سفارش")
    await open_shelf(buyer, "حجمی", "ویژه")
    await tap_starting(buyer, "بدون انقضا")
    await tap_starting(buyer, "۲۰ گیگ")
    await buyer.send("again")
    assert buyer.last_text == plain(texts.purchase_renew_or_new)


async def _no_expiry_plan_also_priced_in_the_no_expiry_tab(app: App, store: Store) -> None:
    """The owner prices the «۵۰ گیگ / ۳۰ روز» plan in the «بدون انقضا» tab too (۱۲۰٬۰۰۰)."""
    from sqlalchemy import select

    from database.models.store import StorePackage, StorePackagePrice
    from services.store_admin import commit_and_activate, get_draft_category, get_or_create_draft

    await _add_tabs(app, store)
    async with app.session() as session:
        draft = await get_or_create_draft(session)
        special = await get_draft_category(session, store.category_ids["special"])  # ids moved
        package = (
            await session.execute(
                select(StorePackage).where(
                    StorePackage.store_version_id == draft.id,
                    StorePackage.category_id == special.id,
                    StorePackage.title_fa == store.plans["special_50"].title,
                )
            )
        ).scalar_one()
        session.add(
            StorePackagePrice(
                store_version_id=draft.id,
                package_slug=package.slug,
                duration_days=None,
                tier="economy",
                price_toman=120_000,
            )
        )
        await commit_and_activate(session, notes="قیمت بدون انقضا اضافه شد")


@smoke("۵.۶")
async def test_renewing_a_no_expiry_service_stays_on_the_no_expiry_tab(
    app: App, store: Store
) -> None:
    from tests.e2e._s08_helpers import my_service

    await _no_expiry_plan_also_priced_in_the_no_expiry_tab(app, store)
    buyer = app.customer("buyer")
    await buyer.start()
    await app.give_wallet(buyer, 500_000)
    await open_shelf(buyer, "حجمی", "ویژه")
    await tap_starting(buyer, "بدون انقضا")
    await tap_starting(buyer, "۵۰ گیگ")
    await buyer.send("buyer")
    await buyer.tap("بدون کد تخفیف")
    await buyer.tap("ثبت سفارش")
    assert (await app.latest_order(buyer)).price_toman == 120_000
    assert app.panel.only_user().days_left() is None

    await my_service(buyer)
    await tap_starting(buyer, "تمدید این سرویس")
    await tap_starting(buyer, "بدون کد")
    assert "مدت: بدون انقضا" in buyer.last_text, buyer.last_text
    assert "120000" in digits(buyer.last_text), "the no-expiry cell, not the plan's own 30-day one"
    await buyer.tap("ثبت سفارش")

    renewal = await app.latest_order(buyer)
    assert renewal.price_toman == 120_000 and renewal.delivery_duration_days is None
    account = app.panel.only_user()
    assert account.days_left() is None, "a no-expiry service must not gain an expiry on renew"
    assert account.data_limit == 100 * GB

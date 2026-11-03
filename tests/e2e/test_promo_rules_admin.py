"""Section ۱۷, rows ۱۷.۱۷ to ۱۷.۲۲: promo codes with usage rules, from the owner's screen to the till."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from bot.texts import texts
from database.models import Order, OrderStatus, PromoRedemption
from services.store_config import get_published_store_version
from tests.e2e._s07b_helpers import tap_starting
from tests.e2e._s09_helpers import open_store
from tests.e2e.harness import App, Store, browse_to_plan, digits, plain, publish_store, smoke
from tests.e2e.harness.expectations import report_plain

SKIP = "رد کردن"
YES = "✅ بله"
NO = "❌ نه"


@pytest.fixture(autouse=True)
async def store(app: App) -> Store:
    return await publish_store(app)


async def _open_promos(admin) -> None:  # type: ignore[no-untyped-def]
    await open_store(admin)
    await tap_starting(admin, "🏷")
    await tap_starting(admin, "🏷 مدیریت")


async def _start_wizard(admin, code_line: str) -> None:  # type: ignore[no-untyped-def]
    await _open_promos(admin)
    await tap_starting(admin, "➕")
    await admin.send(code_line)


async def _stored(app: App, code: str) -> dict[str, object]:
    async with app.session() as session:
        published = await get_published_store_version(session)
        assert published is not None
        entry = published.pricing_document["promo_codes"][code]
    assert isinstance(entry, dict), entry
    return entry


async def _use(app: App, store: Store, name: str, code: str):  # type: ignore[no-untyped-def]
    """A customer reaches the summary with the code and places the order."""
    customer = app.customer(name)
    await customer.start()
    await browse_to_plan(customer, store, "special_50")
    await customer.send(f"{name}12")
    await customer.send(code)
    return customer


@smoke("۱۷.۱۷")
async def test_the_wizard_asks_four_questions_in_order_and_first_purchase_only_stores_one_use(
    app: App, store: Store
) -> None:
    admin = app.admin()
    await _start_wizard(admin, "VIP 20")
    assert admin.last_text == plain(texts.admin_promo_ask_first_only)
    assert {YES, NO, SKIP} <= set(admin.buttons)

    await tap_starting(admin, YES)
    # «uses per user» is skipped: a first purchase can only be used once.
    assert admin.last_text == plain(texts.admin_promo_ask_total_limit), admin.last_text
    await admin.send("50")
    assert admin.last_text == report_plain(texts.admin_promo_ask_days)
    assert SKIP in admin.buttons
    before = datetime.now(UTC)
    await admin.send("30")

    entry = await _stored(app, "VIP")
    assert entry["first_purchase_only"] is True
    assert entry["per_user_limit"] == 1
    assert entry["total_limit"] == 50
    expires = datetime.fromisoformat(str(entry["expires_at"]))
    assert expires.tzinfo is not None
    assert abs(expires - (before + timedelta(days=30))) < timedelta(minutes=2)
    # The finished wizard lands on the list, with a way on.
    assert "VIP" in admin.last_text and admin.buttons
    shown = digits(admin.last_text)
    assert "مصرف: 0 از 50" in shown, shown


@smoke("۱۷.۱۷")
async def test_without_first_purchase_only_the_per_user_step_is_asked_and_zero_days_means_never(
    app: App,
) -> None:
    admin = app.admin()
    await _start_wizard(admin, "MANY 10")
    await tap_starting(admin, NO)
    assert admin.last_text == plain(texts.admin_promo_ask_user_limit)
    await admin.send("2")
    assert admin.last_text == plain(texts.admin_promo_ask_total_limit)
    await tap_starting(admin, SKIP)
    assert admin.last_text == report_plain(texts.admin_promo_ask_days)
    await admin.send("0")

    entry = await _stored(app, "MANY")
    assert entry.get("per_user_limit") == 2
    assert not entry.get("first_purchase_only")
    assert not entry.get("total_limit")
    assert not entry.get("expires_at")


@smoke("۱۷.۱۸")
async def test_skipping_every_step_keeps_the_code_unlimited(app: App, store: Store) -> None:
    admin = app.admin()
    await _start_wizard(admin, "OPEN 10")
    for _ in range(4):
        await tap_starting(admin, SKIP)
    entry = await _stored(app, "OPEN")
    for key in ("first_purchase_only", "per_user_limit", "total_limit", "expires_at"):
        assert not entry.get(key), (key, entry)
    assert "مصرف: 0 بار" in digits(admin.last_text), admin.last_text

    ali = await _use(app, store, "ali", "open")
    price = store.plans["special_50"].price
    assert str(price - price * 10 // 100) in digits(ali.last_text), ali.last_text


@smoke("۱۷.۱۹")
async def test_a_typed_answer_that_is_not_a_number_keeps_the_admin_on_the_step(app: App) -> None:
    admin = app.admin()
    await _start_wizard(admin, "NUM 10")
    await tap_starting(admin, NO)
    await admin.send("abc")
    assert admin.last_text.startswith(report_plain(texts.admin_promo_invalid_number))
    assert admin.buttons, "an admin message always carries a keyboard"
    await admin.send("-3")
    assert admin.last_text.startswith(report_plain(texts.admin_promo_invalid_number))
    await admin.send("2")
    assert admin.last_text == plain(texts.admin_promo_ask_total_limit)


@smoke("۱۷.۲۰")
async def test_the_list_shows_the_rules_and_the_live_usage_and_publishing_keeps_the_count(
    app: App, store: Store
) -> None:
    admin = app.admin()
    await _start_wizard(admin, "VIP 20")
    await tap_starting(admin, NO)
    await admin.send("1")
    await admin.send("5")
    await tap_starting(admin, SKIP)

    ali = await _use(app, store, "ali", "vip")
    await ali.tap("ثبت سفارش")
    order = await app.latest_order(ali)

    await _open_promos(admin)
    assert "مصرف: 1 از 5" in digits(admin.last_text), admin.last_text

    # Another edit publishes a new catalog version (new row ids): the count stays.
    await tap_starting(admin, "➕")
    await admin.send("NEXT 5")
    for _ in range(4):
        await tap_starting(admin, SKIP)
    assert "مصرف: 1 از 5" in digits(admin.last_text), admin.last_text
    assert "مصرف: 0" not in digits(admin.last_text).split("VIP")[1].split("NEXT")[0]

    # An order that dies gives its use back.
    async with app.session() as session:
        row = await session.get(Order, order.id)
        assert row is not None
        row.status = OrderStatus.EXPIRED
        await session.commit()
    await _open_promos(admin)
    assert "مصرف: 0 از 5" in digits(admin.last_text), admin.last_text


@smoke("۱۷.۲۱")
async def test_the_toggle_keeps_the_rules_and_a_switched_off_code_is_still_refused(
    app: App, store: Store
) -> None:
    admin = app.admin()
    await _start_wizard(admin, "VIP 20")
    await tap_starting(admin, YES)
    await admin.send("7")
    await admin.send("3")
    await tap_starting(admin, "✅ VIP")  # the code's own button is the on/off switch
    entry = await _stored(app, "VIP")
    assert entry["active"] is False
    assert entry["first_purchase_only"] is True and entry["per_user_limit"] == 1
    assert entry["total_limit"] == 7 and entry.get("expires_at")
    assert "مصرف: 0 از 7" in digits(admin.last_text), admin.last_text

    ali = await _use(app, store, "ali", "vip")
    assert texts.purchase_invalid_promo in ali.last_text


@smoke("۱۷.۲۲")
async def test_a_customer_gets_the_rules_own_sentence_at_the_summary(
    app: App, store: Store
) -> None:
    admin = app.admin()
    await _start_wizard(admin, "ONE 20")
    await tap_starting(admin, YES)
    await admin.send("1")
    await tap_starting(admin, SKIP)

    ali = await _use(app, store, "ali", "one")
    await ali.tap("ثبت سفارش")
    sara = await _use(app, store, "sara", "one")
    assert sara.last_text == plain(texts.purchase_promo_used_up)
    assert "ثبت سفارش" not in " ".join(sara.buttons), "a refused code must not reach the summary"


@smoke("۱۷.۲۳")
async def test_a_use_taken_while_the_summary_is_open_is_answered_at_the_till(
    app: App, store: Store
) -> None:
    admin = app.admin()
    await _start_wizard(admin, "ONE 20")
    await tap_starting(admin, NO)
    await admin.send("1")
    await admin.send("1")
    await tap_starting(admin, SKIP)

    ali = await _use(app, store, "ali", "one")  # the summary, with the code
    price = store.plans["special_50"].price
    assert str(price - price * 20 // 100) in digits(ali.last_text), ali.last_text
    sara = await _use(app, store, "sara", "one")  # takes the last use first
    await sara.tap("ثبت سفارش")

    await ali.tap("ثبت سفارش")
    assert texts.purchase_promo_used_up in [m.text for m in ali.messages][-3:], ali.last_text
    assert str(price) in digits(ali.last_text), ali.last_text  # the summary, without the code
    assert "ثبت سفارش" in " ".join(ali.buttons)
    async with app.session() as session:
        orders = await session.scalar(select(func.count()).select_from(Order))
        used = await session.scalar(select(func.count()).select_from(PromoRedemption))
    assert orders == 1 and used == 1, "only sara's order exists; ali's was never written"

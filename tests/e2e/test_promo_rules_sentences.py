"""Section ۱۷: the customer's own sentence for each promo rule, typed at the promo step.

The used-up sentence is driven in ``test_promo_rules_admin.py``. Here the other three:
expired, first purchase only, and the per-customer limit. Each code is made through the owner's
wizard; the customer types it at the promo step and must see exactly that sentence, no order
written, no use taken, and the plain purchase must still go through at the undiscounted price.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from bot.texts import texts
from database.models import Order, OrderStatus, PromoRedemption, User
from database.models.store import StoreVersion
from services.store_config import get_published_store_version
from tests.e2e._s07b_helpers import tap_starting
from tests.e2e.harness import Actor, App, Store, browse_to_plan, digits, plain, publish_store
from tests.e2e.test_promo_rules_admin import NO, SKIP, YES, _start_wizard

NO_CODE = "بدون کد تخفیف"


@pytest.fixture(autouse=True)
async def store(app: App) -> Store:
    return await publish_store(app)


async def _orders(app: App, customer: Actor) -> list[Order]:
    async with app.session() as session:
        rows = await session.execute(
            select(Order)
            .join(User, User.id == Order.user_id)
            .where(User.telegram_id == customer.user_id)
            .order_by(Order.id)
        )
        return list(rows.scalars().all())


async def _redemptions(app: App) -> int:
    async with app.session() as session:
        return int(await session.scalar(select(func.count()).select_from(PromoRedemption)) or 0)


async def _to_the_code_step(customer: Actor, store: Store, service: str) -> None:
    if not customer.messages:
        await customer.start()
    await browse_to_plan(customer, store, "special_50")
    await customer.send(service)


async def _expire(app: App, code: str) -> None:
    """The date has passed: move the stored expiry back, as a day going by would."""
    async with app.session() as session:
        published = await get_published_store_version(session)
        assert published is not None
        version = await session.get(StoreVersion, published.id)
        assert version is not None
        doc = copy.deepcopy(version.pricing_document)
        assert doc["promo_codes"][code].get("expires_at"), doc["promo_codes"][code]
        doc["promo_codes"][code]["expires_at"] = (
            datetime.now(UTC) - timedelta(hours=1)
        ).isoformat()
        version.pricing_document = doc
        await session.commit()


async def _become_active(app: App, order_id: int) -> None:
    async with app.session() as session:
        row = await session.get(Order, order_id)
        assert row is not None
        row.status = OrderStatus.ACTIVE
        await session.commit()


async def _refused_then_plain_purchase(
    app: App, customer: Actor, store: Store, sentence: str, orders_before: int
) -> None:
    """The refusal, then «بدون کد تخفیف»: the summary at the full price, and the order at it."""
    assert customer.last_text == plain(sentence), customer.last_text
    assert any(NO_CODE in b for b in customer.buttons), "the customer can go on without a code"
    assert len(await _orders(app, customer)) == orders_before, "a refused code writes no order"
    assert await _redemptions(app) == 0, "a refused code takes no use"

    await customer.tap(NO_CODE)
    price = store.plans["special_50"].price
    assert str(price) in digits(customer.last_text), customer.last_text
    assert "ثبت سفارش" in " ".join(customer.buttons)
    await customer.tap("ثبت سفارش")
    orders = await _orders(app, customer)
    assert len(orders) == orders_before + 1
    assert orders[-1].price_toman == price and orders[-1].discount_toman == 0
    assert await _redemptions(app) == 0, "the plain purchase uses no code"


async def test_an_expired_code_gets_the_expiry_sentence_and_the_customer_can_buy_without_it(
    app: App, store: Store
) -> None:
    admin = app.admin()
    await _start_wizard(admin, "OLDIE 20")
    await tap_starting(admin, NO)
    await tap_starting(admin, SKIP)
    await tap_starting(admin, SKIP)
    await admin.send("1")  # a real expiry, a day ahead
    await _expire(app, "OLDIE")

    ali = app.customer("ali")
    await _to_the_code_step(ali, store, "ali12")
    await ali.send("oldie")
    await _refused_then_plain_purchase(app, ali, store, texts.purchase_promo_expired, 0)


async def test_a_first_purchase_code_gets_its_sentence_from_a_customer_who_already_bought(
    app: App, store: Store
) -> None:
    admin = app.admin()
    await _start_wizard(admin, "FIRST 20")
    await tap_starting(admin, YES)
    await tap_starting(admin, SKIP)
    await tap_starting(admin, SKIP)

    ali = app.customer("ali")
    await _to_the_code_step(ali, store, "ali12")
    await ali.tap(NO_CODE)
    await ali.tap("ثبت سفارش")
    first = (await _orders(app, ali))[0]
    await _become_active(app, first.id)  # a paid service

    await _to_the_code_step(ali, store, "ali34")
    await ali.send("first")
    await _refused_then_plain_purchase(app, ali, store, texts.purchase_promo_first_only, 1)


async def test_a_customer_who_used_a_code_up_gets_the_per_customer_sentence(
    app: App, store: Store
) -> None:
    admin = app.admin()
    await _start_wizard(admin, "ONCE 20")
    await tap_starting(admin, NO)
    await admin.send("1")
    await tap_starting(admin, SKIP)
    await tap_starting(admin, SKIP)

    ali = app.customer("ali")
    await _to_the_code_step(ali, store, "ali12")
    await ali.send("once")
    price = store.plans["special_50"].price
    assert str(price - price * 20 // 100) in digits(ali.last_text), ali.last_text
    await ali.tap("ثبت سفارش")
    first = (await _orders(app, ali))[0]
    await _become_active(app, first.id)
    assert await _redemptions(app) == 1

    await _to_the_code_step(ali, store, "ali34")
    await ali.send("once")
    assert ali.last_text == plain(texts.purchase_promo_user_limit), ali.last_text
    assert any(NO_CODE in b for b in ali.buttons)
    assert len(await _orders(app, ali)) == 1, "a refused code writes no order"
    assert await _redemptions(app) == 1, "and takes no second use"

    await ali.tap(NO_CODE)
    assert str(price) in digits(ali.last_text), ali.last_text
    await ali.tap("ثبت سفارش")
    orders = await _orders(app, ali)
    assert len(orders) == 2
    assert orders[-1].price_toman == price and orders[-1].discount_toman == 0
    assert await _redemptions(app) == 1

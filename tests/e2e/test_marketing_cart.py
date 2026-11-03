"""Section ۱۷, rows ۱۷.۴ and ۱۷.۵: the cart reminder, end to end through the real job."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import select

from bot.keyboards.reply_menu import BUY
from bot.texts import texts
from database.models import Order, OrderStatus
from tests.e2e._s17_helpers import switch_on
from tests.e2e.harness import Actor, App, Store, plain, publish_store, smoke


@pytest_asyncio.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


async def _open_order(app: App, store: Store, ali: Actor, minutes_old: int) -> int:
    await ali.start()
    await ali.tap_reply(BUY)
    await ali.tap(store.groups["volume"].title)
    await ali.tap(store.groups["special"].title)
    await ali.tap(store.plans["special_50"].title)
    await ali.send("ali_cart")
    await ali.tap("بدون کد تخفیف")
    await ali.tap("ثبت سفارش")
    async with app.session() as session:
        order = (await session.scalars(select(Order))).one()
        assert order.status == OrderStatus.PENDING_PAYMENT
        order.created_at = datetime.now(UTC) - timedelta(minutes=minutes_old)
        await session.commit()
        return int(order.id)


@smoke("۱۷.۴")
async def test_the_reminder_reaches_the_customer_and_its_button_resumes_the_order(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await switch_on(app, "cart")
    order_id = await _open_order(app, store, ali, minutes_old=20)
    mark = ali.mark()

    assert await app.run_job("marketing_cart") == 1
    assert ali.texts_since(mark) == [plain(texts.notify_cart_reminder)]
    assert "ادامهٔ سفارش" in ali.buttons

    await ali.tap("ادامهٔ سفارش")
    assert plain(texts.purchase_choose_type) not in ali.last_text
    async with app.session() as session:
        orders = (await session.scalars(select(Order))).all()
    assert [int(o.id) for o in orders] == [order_id], "resumed, not a second order"

    assert await app.run_job("marketing_cart") == 0, "one reminder per order"


@smoke("۱۷.۵")
async def test_nothing_goes_out_while_the_campaign_is_off(app: App, store: Store) -> None:
    ali = app.customer("ali")
    await _open_order(app, store, ali, minutes_old=20)
    mark = ali.mark()
    assert await app.run_job("marketing_cart") == 0
    assert ali.texts_since(mark) == []

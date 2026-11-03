"""Live finding: the order summary kept «ثبت سفارش / شروع دوباره / انصراف»
after the order was confirmed and the service delivered."""

from __future__ import annotations

from sqlalchemy import func, select

from bot.keyboards.purchase import PurchaseCB
from database.models import Order
from tests.e2e.harness import App, browse_to_plan, publish_store


async def _to_summary(app: App, customer) -> object:  # noqa: ANN001
    store = await publish_store(app)
    await customer.start()
    await browse_to_plan(customer, store, "special_50")
    await customer.send("ali12")
    await customer.tap("بدون کد تخفیف")
    summary = customer.last_message
    assert any("ثبت سفارش" in label for label in summary.buttons), summary
    return summary


async def _orders(app: App) -> int:
    return int(await app.one(select(func.count(Order.id))))


async def test_card_order_summary_loses_its_buttons_once_confirmed(app: App) -> None:
    ali = app.customer("ali")
    summary = await _to_summary(app, ali)

    await ali.tap("ثبت سفارش")

    assert summary.buttons == []
    assert await _orders(app) == 1
    # A second tap from a client that still shows the old keyboard makes no second order.
    await ali.press(PurchaseCB.CONFIRM, message=summary)
    assert await _orders(app) == 1


async def test_wallet_paid_summary_loses_its_buttons_once_delivered(app: App) -> None:
    ali = app.customer("ali")
    await ali.start()
    await app.give_wallet(ali, 1_000_000)
    summary = await _to_summary(app, ali)

    await ali.tap("ثبت سفارش")

    assert len(app.panel.users) == 1, "the wallet paid and the service was built"
    assert summary.buttons == []
    await ali.press(PurchaseCB.CONFIRM, message=summary)
    assert await _orders(app) == 1
    assert len(app.panel.users) == 1

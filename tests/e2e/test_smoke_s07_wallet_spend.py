"""Section 7, second half: the customer sees and spends the wallet.

Rows (the second ۷.۴, ۷.۵ and ۷.۶ of the section, under «کیف پول»): the balance on the
menu button, a plan cheaper than the balance, a plan dearer than it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from bot.keyboards.reply_menu import ACCOUNT, BUY
from database.models import Order, OrderStatus, Payment
from tests.e2e._s07_helpers import credit_by_service, db_user, ledger, press_on_last
from tests.e2e.harness import GB, App, Store, digits, publish_store, smoke


@pytest.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


async def _to_summary(ali, store: Store, plan_key: str) -> None:
    plan = store.plans[plan_key]
    await ali.tap_reply(BUY)
    await ali.tap("حجمی")
    await ali.tap(store.groups[plan.group].title)
    await ali.tap(plan.title)
    await ali.send("ali12")
    await ali.tap("بدون کد تخفیف")


@smoke("۷.۴")
async def test_the_main_menu_wallet_button_carries_the_balance(app: App) -> None:
    ali = app.customer("ali")
    await ali.start()
    await credit_by_service(app, ali, 200_000)
    await ali.tap_reply(ACCOUNT)  # no services yet: the screen offers the way back to the menu
    await press_on_last(ali, "بازگشت به منو")
    wallet = [b for b in ali.buttons if "کیف پول" in b]
    assert len(wallet) == 1 and "۲۰۰٬۰۰۰" in wallet[0], ali.buttons


@smoke("۷.۵")
async def test_a_plan_cheaper_than_the_balance_activates_at_once_with_no_receipt(
    app: App, store: Store
) -> None:
    plan = store.plans["economy_50"]
    ali = app.customer("ali")
    await ali.start()
    await credit_by_service(app, ali, 200_000)
    await _to_summary(ali, store, "economy_50")
    assert "0" in digits(ali.last_text.split("مبلغ قابل پرداخت کارت:")[1].split("\n")[0])
    await ali.tap("ثبت سفارش")
    order = await app.latest_order(ali)
    assert order.status == OrderStatus.ACTIVE
    assert f"سرویس سفارش #{order.id} آماده‌ست." in ali.last_text, ali.last_text
    assert "رسید" not in ali.last_text
    payment = await app.one(select(Payment).where(Payment.order_id == order.id))
    assert payment.method == "wallet"
    assert (await db_user(app, ali)).wallet_balance_toman == 200_000 - plan.price
    account = app.panel.only_user()
    assert account.data_limit == plan.volume_gb * GB
    assert order.wallet_applied_toman == plan.price


@smoke("۷.۶")
async def test_a_dearer_plan_takes_the_balance_and_asks_the_card_for_the_rest(
    app: App, store: Store
) -> None:
    plan = store.plans["special_50"]
    ali = app.customer("ali")
    await ali.start()
    await credit_by_service(app, ali, 50_000)
    await _to_summary(ali, store, "special_50")
    summary = digits(ali.last_text)
    assert "50000" in summary.split("کسر از کیف پول:")[1].split("\n")[0]
    assert "100000" in summary.split("مبلغ قابل پرداخت کارت:")[1].split("\n")[0]
    await ali.tap("ثبت سفارش")
    order = await app.latest_order(ali)
    assert order.status == OrderStatus.PENDING_PAYMENT
    assert order.price_toman == plan.price and order.wallet_applied_toman == 50_000
    assert "100000" in digits(ali.last_text), "the card is asked for the remainder only"
    assert "150000" not in digits(ali.last_text)
    assert (await db_user(app, ali)).wallet_balance_toman == 0, "the balance was taken at once"
    debit = (await ledger(app, ali))[-1]
    assert (debit.entry_type.value, debit.amount_toman, debit.order_id) == (
        "debit",
        50_000,
        order.id,
    )

    # The receipt is for the remainder, and approving it activates the service.
    await ali.send_photo()
    admin = app.admin()
    await admin.send("/admin")
    await admin.tap("پرداخت‌ها")
    await admin.tap(f"#{order.id}")
    await admin.tap("✅ تایید")
    assert (await app.latest_order(ali)).status == OrderStatus.ACTIVE
    payment = await app.one(select(Payment).where(Payment.order_id == order.id))
    assert (payment.method, payment.amount_toman) == ("card_to_card", 100_000)
    assert len(await app.all(select(Order))) == 1

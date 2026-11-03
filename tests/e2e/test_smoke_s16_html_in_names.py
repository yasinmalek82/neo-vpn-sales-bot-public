"""A name or a typed reason that looks like HTML never breaks a message (ARCH-1.8).

Telegram refuses the whole message on a bare ``<``; the strict fake Telegram does the same, so
every screen below that interpolates the customer's name or the owner's typed reason fails the
scenario if it forgets to escape. The first name is the one the audit used.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from bot.keyboards.reply_menu import ACCOUNT, REFERRAL, SUPPORT, TRIAL, WALLET
from database.models import User
from tests.e2e._s07_helpers import press_on_last, users_list
from tests.e2e.harness import App, Store, order_by_card, publish_store, smoke

HOSTILE = "Ali <3 & <b>x"


@pytest.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


async def _hostile_customer(app: App):
    ali = app.customer("ali", first_name=HOSTILE)
    await ali.start()
    user = await app.one(select(User).where(User.telegram_id == ali.user_id))
    async with app.session() as session:
        row = await session.get(User, user.id)
        row.last_name = "<i>Z"
        await session.commit()
    return ali


@smoke("ARCH-1.8")
async def test_the_customer_screens_read_the_name_without_breaking(app: App, store: Store) -> None:
    ali = await _hostile_customer(app)
    await ali.send("/start")
    for label in (WALLET, REFERRAL, TRIAL, SUPPORT, ACCOUNT):
        await ali.tap_reply(label)


async def _open_hostile_card(admin) -> None:
    await users_list(admin)
    await press_on_last(admin, "Ali <3", startswith=True)


@smoke("ARCH-1.8")
async def test_the_owner_screens_read_the_name_without_breaking(app: App, store: Store) -> None:
    ali = await _hostile_customer(app)
    order = await order_by_card(ali, store)
    await ali.send_photo()
    admin = app.admin()
    await admin.send("/admin")
    await admin.tap("پرداخت‌ها")
    await admin.tap(f"#{order.id}")
    await _open_hostile_card(admin)
    await _open_hostile_card(admin)
    wanted = [b for row in admin.last_message.rows for b in row if not b.startswith(("🔎", "🏠"))]
    assert len(wanted) >= 7, wanted
    for label in wanted:
        await _open_hostile_card(admin)
        await press_on_last(admin, label)


@smoke("ARCH-1.8", "۴.۷")
async def test_a_typed_reject_reason_with_a_less_than_sign_reaches_the_customer(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await ali.start()
    order = await order_by_card(ali, store)
    await ali.send_photo()
    admin = app.admin()
    await admin.send("/admin")
    await admin.tap("پرداخت‌ها")
    await admin.tap(f"#{order.id}")
    await admin.tap("❌ رد")
    await admin.tap("✏️ دلیل سفارشی")
    mark = ali.mark()
    await admin.send("واریز <5 دقیقه پیش")
    assert "واریز <5 دقیقه پیش" in ali.texts_since(mark)[-1], "the customer never got the reason"
    assert "واریز <5 دقیقه پیش" in admin.last_text

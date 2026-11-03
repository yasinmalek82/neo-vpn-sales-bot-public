"""Section 12 (the rest): blocking a customer and the guard on blocking an admin.

Rows ۱۲.۵ and ۱۲.۶ (customer opens /admin, forged admin callback) are another worker's.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from bot.keyboards.reply_menu import BUY
from bot.texts import texts
from database.models import Order, User
from tests.e2e._s07_helpers import button_cb, db_user, open_customer_card
from tests.e2e.harness import (
    App,
    Store,
    browse_to_plan,
    order_by_card,
    plain,
    publish_store,
    smoke,
)
from tests.e2e.harness.settings import ADMIN_IDS, CARD_NUMBER


@pytest.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


async def _ban(admin, telegram_id: int) -> None:
    await admin.send("/admin")
    await admin.tap("👥 مشتری‌ها")
    await admin.tap("🚫 مسدود کردن")
    await admin.send(str(telegram_id))


async def _unban(admin, telegram_id: int) -> None:
    await admin.send("/admin")
    await admin.tap("👥 مشتری‌ها")
    await admin.tap("✅ رفع مسدودی")
    await admin.send(str(telegram_id))


@smoke("۱۲.۱")
async def test_the_owner_blocks_a_customer_by_telegram_id(app: App) -> None:
    ali = app.customer("ali")
    await ali.start()
    admin = app.admin()
    await _ban(admin, ali.user_id)
    assert admin.last_text == plain(texts.admin_ban_ok.format(telegram_id=ali.user_id))
    assert (await db_user(app, ali)).is_banned is True


@smoke("۱۲.۲")
async def test_a_blocked_customer_cannot_start_or_buy_and_is_told_so(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await ali.start()
    await browse_to_plan(ali, store, "special_50")
    await ali.send("ali12")
    await ali.tap("بدون کد تخفیف")
    summary = ali.last_message  # the order summary, «ثبت سفارش» still on screen
    confirm = button_cb(summary, "ثبت سفارش")
    admin = app.admin()
    await _ban(admin, ali.user_id)

    for action in (ali.start, lambda: ali.tap_reply(BUY)):
        mark = ali.mark()
        await action()
        reply = ali.texts_since(mark)
        assert len(reply) == 1 and reply[0].startswith(plain(texts.banned)), reply

    # The confirm button from before the ban is dead too: no order, no panel account.
    await ali.press(confirm, message=summary, expect_answer=False)
    assert await app.all(select(Order)) == []
    assert app.panel.users == {}


@smoke("۱۲.۳")
async def test_unblocking_brings_them_back_and_they_can_buy(app: App, store: Store) -> None:
    ali = app.customer("ali")
    await ali.start()
    admin = app.admin()
    await _ban(admin, ali.user_id)
    await _unban(admin, ali.user_id)
    assert admin.last_text == plain(texts.admin_unban_ok.format(telegram_id=ali.user_id))
    assert (await db_user(app, ali)).is_banned is False
    await ali.start()
    assert plain(texts.banned) not in ali.last_text
    order = await order_by_card(ali, store, "special_50")
    assert CARD_NUMBER in ali.last_text and order.id


@smoke("۱۲.۴")
async def test_blocking_an_admin_by_id_is_refused_and_the_admin_keeps_access(app: App) -> None:
    admin = app.admin()
    second = app.admin(1)
    await second.send("/admin")  # the second admin has used the bot, so the database knows them
    await _ban(admin, admin.user_id)
    assert admin.last_text.startswith(plain(texts.admin_cannot_ban_admin).splitlines()[0])
    other = ADMIN_IDS[1]
    await admin.send(str(other))
    assert admin.last_text.startswith(plain(texts.admin_cannot_ban_admin).splitlines()[0])
    users = await app.all(select(User).where(User.telegram_id.in_(ADMIN_IDS)))
    assert users and not any(u.is_banned for u in users)
    await admin.send("/admin")
    assert "پنل مدیریت" in admin.last_text


@smoke("۱۲.۴")
async def test_the_customer_card_refuses_to_block_an_admin_too(app: App) -> None:
    admin = app.admin()
    await open_customer_card(admin, "Admin900001")
    await admin.tap("🚫 مسدود کردن")
    assert admin.popup == texts.admin_cannot_ban_admin
    assert (await db_user(app, admin)).is_banned is False

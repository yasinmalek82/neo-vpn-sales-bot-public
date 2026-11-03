"""Section 15, part one: the late receipt and the owner's wallet charge from it.

Rows ۱۵.۱ to ۱۵.۴. A receipt photo that arrives after the order expired is real money with no
order: the customer is told, the admins get the photo once, and the owner pays it into the wallet.
"""

from __future__ import annotations

import pytest

from bot.texts import texts
from database.models import OrderStatus
from services.payments import record_late_receipt
from tests.e2e._s07_helpers import db_user, ledger
from tests.e2e._s07b_helpers import tap_starting
from tests.e2e.harness import App, Store, order_by_card, publish_store, smoke
from tests.e2e.harness.actors import Actor
from tests.e2e.harness.expectations import report_plain

AMOUNT = "۱۵۰٬۰۰۰"


@pytest.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


async def _expired_order(app: App, store: Store, ali: Actor):
    """The customer starts a card purchase, never pays, and the order times out."""
    await ali.start()
    order = await order_by_card(ali, store, "special_50")
    await app.age_order(order.id, minutes=31)
    await app.run_job("expire")
    assert (await app.latest_order(ali)).status == OrderStatus.EXPIRED
    return order


def _late_photos(admin: Actor) -> list:
    return [
        m for m in admin.messages if m.kind == "photo" and "رسید برای سفارش منقضی‌شده" in m.text
    ]


@smoke("۱۵.۱")
async def test_a_receipt_after_expiry_tells_the_customer_and_gives_both_admins_the_photo(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    order = await _expired_order(app, store, ali)
    admins = [app.admin(0), app.admin(1)]
    mark = ali.mark()
    sent = await ali.send_photo()
    assert ali.texts_since(mark) == [
        report_plain(texts.notify_late_receipt.format(order_id=str(order.id), amount=AMOUNT))
    ]
    assert (
        await app.latest_order(ali)
    ).status == OrderStatus.EXPIRED, "the order does not come back"
    assert app.panel.users == {}, "and nothing was delivered"
    for admin in admins:
        photos = _late_photos(admin)
        assert len(photos) == 1, (admin.name, admin.messages)
        assert photos[0].file_id == sent
        assert (
            f"سفارش: #{order.id}" in photos[0].text
            and report_plain(f"مبلغ کارت: {AMOUNT} تومان") in photos[0].text
        )
        assert f"🏦 شارژ کیف پول ({AMOUNT})" in photos[0].buttons


@smoke("۱۵.۲")
async def test_the_same_photo_again_gets_the_same_notice_and_no_second_admin_message(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    order = await _expired_order(app, store, ali)
    admins = [app.admin(0), app.admin(1)]
    await ali.send_photo()
    marks = {a.name: a.mark() for a in admins}
    mark = ali.mark()
    await ali.send_photo()
    assert ali.texts_since(mark) == [
        report_plain(texts.notify_late_receipt.format(order_id=str(order.id), amount=AMOUNT))
    ]
    for admin in admins:
        assert admin.messages_since(marks[admin.name]) == [], "no second message for the admin"
        assert len(_late_photos(admin)) == 1


@smoke("۱۵.۲")
async def test_the_late_receipt_is_queued_for_the_admins_once_per_order(
    app: App, store: Store
) -> None:
    """The «once» half, below the screen: the second call finds the first one's outbox row."""
    ali = app.customer("ali")
    order = await _expired_order(app, store, ali)
    user = await db_user(app, ali)
    async with app.session() as session:
        first = await record_late_receipt(session, order_id=int(order.id), user_id=int(user.id))
        await session.commit()
    async with app.session() as session:
        second = await record_late_receipt(session, order_id=int(order.id), user_id=int(user.id))
        await session.commit()
    assert (first.first, second.first) == (True, False)
    assert first.card_due_toman == second.card_due_toman == 150_000


@smoke("۱۵.۳")
async def test_the_wallet_button_opens_the_manual_confirm_with_the_card_amount_and_pays_it_in(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await _expired_order(app, store, ali)
    await ali.send_photo()
    admin = app.admin(0)
    photo = _late_photos(admin)[0]
    mark = ali.mark()
    button = next(
        b.callback_data for r in photo.markup.inline_keyboard for b in r if "شارژ" in b.text
    )
    await admin.press(button, message=photo)
    assert admin.last_text.startswith("⚠️ تأیید تغییر کیف پول"), admin.last_text
    assert report_plain(f"شارژ: {AMOUNT}") in admin.last_text and "ali" in admin.last_text
    assert await app.wallet_balance(ali) == 0, "nothing moved before the confirm"
    await tap_starting(admin, "تأیید و اعمال")
    assert await app.wallet_balance(ali) == 150_000
    assert ali.texts_since(mark) == [
        report_plain(f"🏦 کیف پولت {AMOUNT} تومان شارژ شد.\n💰 موجودی جدید: {AMOUNT} تومان")
    ]
    entries = await ledger(app, ali)
    assert [(e.entry_type.value, e.amount_toman) for e in entries] == [("credit", 150_000)]


@smoke("۱۵.۴")
async def test_pressing_the_wallet_button_again_pays_nothing_a_second_time(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await _expired_order(app, store, ali)
    await ali.send_photo()
    admin = app.admin(0)
    photo = _late_photos(admin)[0]
    button = next(
        b.callback_data for r in photo.markup.inline_keyboard for b in r if "شارژ" in b.text
    )
    await admin.press(button, message=photo)
    await tap_starting(admin, "تأیید و اعمال")
    await admin.press(button, message=photo)
    assert admin.popup == texts.admin_late_receipt_already
    assert await app.wallet_balance(ali) == 150_000
    assert len(await ledger(app, ali)) == 1


@smoke("۱۵.۴")
async def test_a_second_admin_cannot_pay_the_same_receipt_either(app: App, store: Store) -> None:
    ali = app.customer("ali")
    await _expired_order(app, store, ali)
    await ali.send_photo()
    first, second = app.admin(0), app.admin(1)
    button = next(
        b.callback_data
        for r in _late_photos(first)[0].markup.inline_keyboard
        for b in r
        if "شارژ" in b.text
    )
    await first.press(button, message=_late_photos(first)[0])
    await tap_starting(first, "تأیید و اعمال")
    await second.press(button, message=_late_photos(second)[0])
    assert second.popup == texts.admin_late_receipt_already
    assert await app.wallet_balance(ali) == 150_000

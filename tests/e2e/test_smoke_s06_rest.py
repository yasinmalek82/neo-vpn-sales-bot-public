"""Section 6: the bad paths, rows ۶.۴ to ۶.۱۰.

MANUAL (partly): ۶.۷ asks for a real restart of the process with Ctrl+C. A restart cannot
be faked; what it does to the bot is that the in-memory FSM is gone and every old button
is «stale». The scenario clears the dispatcher's storage, which is exactly that, and
checks that an old button is answered and nothing spins. That a real process comes back
up and polls again is left to the owner.
"""

from __future__ import annotations

import pytest

from bot.keyboards.reply_menu import BUY
from bot.texts import texts
from database.models import Order, OrderStatus
from tests.e2e._s01_helpers import open_shelf
from tests.e2e._s07b_helpers import tap_starting
from tests.e2e.harness import (
    App,
    Store,
    order_by_card,
    plain,
    publish_store,
    smoke,
)
from tests.e2e.harness.expectations import report_plain
from tests.e2e.harness.settings import CARD_NUMBER


@pytest.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


async def _orders(app: App) -> list[Order]:
    from sqlalchemy import select

    return await app.all(select(Order).order_by(Order.id))


@smoke("۶.۴")
async def test_buy_with_an_open_card_order_resumes_it_and_makes_no_second_order(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await ali.start()
    order = await order_by_card(ali, store)
    mark = ali.mark()
    await ali.tap_reply(BUY)
    shown = ali.texts_since(mark)
    assert CARD_NUMBER in shown[-1] and f"#{str(order.id)}" in shown[-1], shown
    assert plain(texts.purchase_choose_type) not in shown, "the shelf must not reopen"
    assert [o.id for o in await _orders(app)] == [order.id]


@smoke("۶.۴")
async def test_buy_with_an_order_in_review_says_it_is_in_the_queue(app: App, store: Store) -> None:
    ali = app.customer("ali")
    await ali.start()
    order = await order_by_card(ali, store)
    await ali.send_photo()
    mark = ali.mark()
    await ali.tap_reply(BUY)
    assert ali.texts_since(mark) == [
        report_plain(texts.purchase_open_in_review.format(order_id=str(order.id)))
    ]
    assert len(await _orders(app)) == 1


@smoke("۶.۵")
async def test_start_with_an_open_order_offers_to_continue_the_payment(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await ali.start()
    order = await order_by_card(ali, store)
    mark = ali.mark()
    await ali.start()
    shown = ali.texts_since(mark)
    assert report_plain(texts.purchase_resume_open.format(order_id=str(order.id))) in shown, shown
    assert CARD_NUMBER in shown[-1]
    assert len(await _orders(app)) == 1


@smoke("۶.۶")
async def test_cancel_in_the_middle_of_a_purchase_returns_to_the_menu_and_frees_the_flow(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await ali.start()
    await open_shelf(ali, "حجمی", "ویژه")
    await tap_starting(ali, store.plans["special_50"].title)
    assert ali.last_text.startswith(plain(texts.purchase_enter_service_name).split("\n")[0])
    mark = ali.mark()
    await ali.send("/cancel")
    assert ali.texts_since(mark) == [plain(texts.cancelled_done)]
    assert BUY in ali.reply_buttons, "the bottom keyboard is still there"
    # Not stuck in the name question: a word is now an unknown message, not a service name.
    mark = ali.mark()
    await ali.send("ali12")
    assert ali.texts_since(mark) == [plain(texts.unknown_message)]
    assert await _orders(app) == []
    # And with nothing to cancel it says so.
    mark = ali.mark()
    await ali.send("/cancel")
    assert ali.texts_since(mark) == [plain(texts.cancelled_nothing)]


@smoke("۶.۷")
async def test_old_buttons_are_answered_after_the_bot_forgot_everything(
    app: App, store: Store
) -> None:
    """A restart empties the FSM memory; every button on an old message is then stale."""
    ali = app.customer("ali")
    await ali.start()
    await open_shelf(ali, "حجمی", "ویژه")
    await tap_starting(ali, store.plans["special_50"].title)
    await ali.send("ali12")  # the promo question, with «بدون کد تخفیف» on it
    app.dp.storage.storage.clear()  # type: ignore[attr-defined]  # what a restart does
    mark = ali.mark()
    await ali.tap("بدون کد تخفیف")  # press() raises UnansweredCallback if the button spins
    assert ali.popup == texts.stale_button or plain(texts.stale_button) in ali.texts_since(mark)
    assert await _orders(app) == [], "a stale confirm must not make an order out of thin air"
    # The person can simply carry on from the menu.
    await ali.tap_reply(BUY)
    assert ali.last_text == plain(texts.purchase_choose_type)


@smoke("۶.۷")
async def test_a_forged_or_ancient_button_gets_an_answer_too(app: App, store: Store) -> None:
    ali = app.customer("ali")
    await ali.start()
    await ali.tap_reply(BUY)
    for data in ("buy:ready:99999", "buy:d:30", "totally:unknown", "adm:retry:1"):
        await ali.press(data)  # raises when the bot leaves the button spinning
    assert (await _orders(app)) == []


# --------------------------------------------------------------------------- 6.8 to 6.10


async def _approved_while_the_panel_key_is_wrong(app: App, store: Store):
    group = await app.connect_report_group()
    ali = app.customer("ali")
    await ali.start()
    order = await order_by_card(ali, store)
    await ali.send_photo()
    admin = app.admin()
    await admin.send("/admin")
    await tap_starting(admin, "پرداخت‌ها")
    await tap_starting(admin, f"#{order.id}")
    app.panel.reject_key()
    mark = ali.mark()
    await admin.tap("✅ تایید")
    return group, ali, admin, order, mark


@smoke("۶.۸")
async def test_an_approval_with_a_wrong_panel_key_tells_the_customer_in_persian_and_refunds_once(
    app: App, store: Store
) -> None:
    group, ali, admin, order, mark = await _approved_while_the_panel_key_is_wrong(app, store)
    told = ali.texts_since(mark)
    assert told == [
        report_plain(
            texts.payment_provision_failed_user.format(order_id=str(order.id), amount="150,000")
        )
    ], told
    assert not any(word in told[0] for word in ("Error", "unauthorized", "PasarGuard", "api_key"))
    assert (await app.latest_order(ali)).status == OrderStatus.PROVISION_FAILED
    assert app.panel.users == {}
    assert await app.wallet_balance(ali) == order.price_toman
    # The owner is told in the errors topic.
    await app.run_job("report_flush")
    real = [t for t in await group.texts("errors") if "تست مسیر" not in t]
    assert len(real) == 1 and "کد سفارش: 1" in real[0], real
    # A second tap on the old approve button must not pay the customer back again.
    await admin.tap("✅ تایید")
    assert await app.wallet_balance(ali) == order.price_toman, "credited more than once"
    await app.run_job("report_flush")
    assert len([t for t in await group.texts("errors") if "تست مسیر" not in t]) == 1


async def _retry(admin, order_id: int) -> None:
    await admin.press(f"adm:retry:{order_id}")


@smoke("۶.۹", "۶.۱۰")
async def test_retry_builds_the_service_once_and_takes_the_credit_back_once(
    app: App, store: Store
) -> None:
    group, ali, admin, order, _ = await _approved_while_the_panel_key_is_wrong(app, store)
    app.panel.reject_key(False)  # the key is right again
    await admin.tap("📋 جزئیات سفارش")
    assert "تلاش مجدد فعال‌سازی" in admin.buttons
    mark = ali.mark()
    await admin.tap("تلاش مجدد فعال‌سازی")  # ۶.۹
    assert admin.popup == texts.admin_retry_ok
    account = app.panel.only_user()
    assert (await app.latest_order(ali)).status == OrderStatus.ACTIVE
    assert await app.wallet_balance(ali) == 0, "the refunded credit must be taken back"
    arrived = ali.messages_since(mark)
    assert [m.kind for m in arrived] == ["photo"] and account.username in arrived[0].text

    # (The customer's card is sent again on a repeat tap: a harmless re-delivery, not asserted.)
    await _retry(admin, order.id)  # ۶.۱۰: the same button again
    assert len(app.panel.users) == 1
    assert len(app.panel.requests_to("POST /api/user")) == 1
    assert await app.wallet_balance(ali) == 0, "charged twice"


@smoke("۶.۹")
async def test_after_a_retry_the_owners_order_card_shows_the_new_state(
    app: App, store: Store
) -> None:
    group, ali, admin, order, _ = await _approved_while_the_panel_key_is_wrong(app, store)
    app.panel.reject_key(False)
    await admin.tap("📋 جزئیات سفارش")
    await admin.tap("تلاش مجدد فعال‌سازی")
    card = [m for m in admin.messages if m.text.startswith("🧾 جزئیات سفارش")][-1]
    assert "وضعیت: فعال\n" in card.text + "\n", card.text
    assert "تلاش مجدد فعال‌سازی" not in card.buttons

"""Section 6: the bad paths. Only the rows that prove the harness are here.

Rows covered: ۶.۱ ۶.۲ ۶.۳. The rest of the section (۶.۴ to ۶.۱۰) is package E1's.
۶.۷ restarts the real process and ۶.۸ changes the panel key, so those two need
care there (a restart cannot be faked; the key is ``panel.reject_key()``).
"""

from __future__ import annotations

from typing import Any

from bot.texts import texts
from database.models import OrderStatus
from tests.e2e.harness import (
    Actor,
    App,
    Store,
    order_by_card,
    pay_by_card,
    publish_store,
    smoke,
)
from tests.e2e.harness.expectations import report_plain


async def _open_review(app: App, store: Store) -> tuple[Actor, Actor, Any]:
    ali = app.customer("ali")
    await ali.start()
    order = await pay_by_card(ali, store)
    admin = app.admin()
    await admin.send("/admin")
    await admin.tap("پرداخت‌ها")
    await admin.tap(f"#{order.id}")
    return ali, admin, order


@smoke("۶.۱")
async def test_a_rejected_receipt_tells_the_customer_why_and_builds_nothing(app: App) -> None:
    store = await publish_store(app)
    ali, admin, order = await _open_review(app, store)
    mark = ali.mark()
    await admin.tap("❌ رد")
    reason = texts.admin_reject_reasons["unreadable"]
    await admin.tap("فیش ناخوانا")
    told = ali.texts_since(mark)[0]
    assert told.startswith(
        report_plain(
            texts.payment_rejected_user_reason.format(order_id=str(order.id), reason=reason)
        )
    )
    assert app.settings.support_handle in told  # a rejected customer's only recourse
    assert (await app.latest_order(ali)).status == OrderStatus.REJECTED
    assert app.panel.users == {}
    assert app.panel.requests_to("POST /api/user") == []


@smoke("۶.۱")
async def test_a_typed_rejection_reason_reaches_the_customer(app: App) -> None:
    store = await publish_store(app)
    ali, admin, order = await _open_review(app, store)
    mark = ali.mark()
    await admin.tap("❌ رد")
    await admin.tap("✏️ دلیل سفارشی")
    await admin.send("مبلغ با فیش نمی‌خونه")
    assert "مبلغ با فیش نمی‌خونه" in ali.texts_since(mark)[0]
    assert (await app.latest_order(ali)).status == OrderStatus.REJECTED


@smoke("۶.۲")
async def test_aborting_the_rejection_leaves_the_order_in_the_queue(app: App) -> None:
    store = await publish_store(app)
    ali, admin, order = await _open_review(app, store)
    mark = ali.mark()
    await admin.tap("❌ رد")
    await admin.tap("لغو رد")
    assert admin.popup == texts.admin_reject_aborted
    assert ali.messages_since(mark) == []  # the customer heard nothing
    assert (await app.latest_order(ali)).status == OrderStatus.PAYMENT_REVIEW
    await admin.send("/admin")
    await admin.tap("پرداخت‌ها")
    assert any(f"#{order.id}" in b for b in admin.buttons)


@smoke("۶.۳")
async def test_an_unpaid_order_expires_after_thirty_minutes_and_the_customer_is_told(
    app: App,
) -> None:
    store = await publish_store(app)
    ali = app.customer("ali")
    await ali.start()
    order = await order_by_card(ali, store)

    await app.age_order(order.id, minutes=29)
    await app.run_job("expire")
    assert (await app.latest_order(ali)).status == OrderStatus.PENDING_PAYMENT  # not yet

    await app.age_order(order.id, minutes=6)  # 35 minutes old now
    mark = ali.mark()
    await app.run_job("expire")
    assert (await app.latest_order(ali)).status == OrderStatus.EXPIRED
    (told,) = ali.texts_since(mark)
    assert told.startswith(report_plain(texts.payment_order_expired.format(order_id=str(order.id))))

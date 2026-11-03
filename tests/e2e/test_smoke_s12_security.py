"""Section 12: a customer must not reach the admin side.

Rows covered: ۱۲.۵ ۱۲.۶. The ban rows (۱۲.۱ to ۱۲.۴) belong to the section's own package.
"""

from __future__ import annotations

import pytest

from bot.keyboards.admin import AdminCB
from bot.keyboards.admin_payment import PayCB
from bot.keyboards.store_admin import StoreCB
from bot.texts import texts
from database.models import OrderStatus
from tests.e2e.harness import App, Store, plain, publish_store, smoke
from tests.e2e.harness.flows import pay_by_card


@smoke("۱۲.۵")
async def test_a_customer_typing_admin_is_refused(app: App) -> None:
    ali = app.customer("ali")
    await ali.start()
    mark = ali.mark()
    await ali.send("/admin")
    assert ali.texts_since(mark) == [plain(texts.admin_only)]


@smoke("۱۲.۵")
async def test_a_customer_has_no_admin_button_on_the_bottom_keyboard(app: App) -> None:
    ali = app.customer("ali")
    await ali.start()
    assert not [b for b in ali.reply_buttons if "مدیریت" in b]


_FORGED = [
    pytest.param(f"{PayCB.APPROVE_PREFIX}1", id="approve-payment"),
    pytest.param(f"{PayCB.REJECT_PREFIX}1", id="reject-payment"),
    pytest.param(AdminCB.HOME, id="admin-home"),
    pytest.param(AdminCB.PENDING, id="pending-queue"),
    pytest.param(AdminCB.STATS, id="stats"),
    pytest.param(AdminCB.PANEL, id="panel-settings"),
    pytest.param(f"{AdminCB.SHOW_RECEIPT}1", id="show-receipt"),
    pytest.param(StoreCB.HUB, id="store-hub"),
]


@smoke("۱۲.۶")
@pytest.mark.parametrize("data", _FORGED)
async def test_a_forged_admin_callback_changes_nothing_and_shows_nothing(
    app: App, data: str
) -> None:
    store: Store = await publish_store(app)
    ali = app.customer("ali")
    await ali.start()
    order = await pay_by_card(ali, store)  # a real order waiting for review: the tempting target
    mark = ali.mark()
    await ali.press(data)
    assert ali.popup == texts.admin_action_denied
    assert not ali.photos[1:], "a forged callback must not send the customer a receipt"
    shown = " ".join(ali.texts_since(mark))
    assert "پنل مدیریت" not in shown and "پرداخت‌های در انتظار" not in shown, shown
    assert (await app.latest_order(ali)).status == OrderStatus.PAYMENT_REVIEW
    assert (await app.latest_order(ali)).id == order.id
    assert app.panel.users == {}

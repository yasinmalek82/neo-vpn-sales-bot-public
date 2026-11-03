"""Live finding: raw English in the admin's order screens.

List buttons «#25 | ACTIVE», detail «پلن: volume-5GB-0d-p30000-h2-g3» and
«پرداخت: verified», header «صفحه 1».
"""

from __future__ import annotations

import re

import pytest

from bot.handlers import admin as ha
from bot.keyboards.admin import orders_list_keyboard
from database.models import OrderStatus, Plan
from database.models.payment import PaymentStatus
from tests.e2e._s07b_helpers import tap_starting
from tests.e2e._s08_helpers import buy_active
from tests.e2e.harness import App, publish_store
from tests.e2e.harness.expectations import report_plain

LATIN_WORD = re.compile(r"[A-Za-z]{3,}")


async def _orders_list(app: App):  # noqa: ANN202
    store = await publish_store(app)
    ali = app.customer("ali", first_name="علی")
    account = await buy_active(app, store, ali)
    admin = app.admin()
    await admin.send("/admin")
    await tap_starting(admin, "💰 فروش و سفارش")
    await tap_starting(admin, "📦 سفارش")
    await tap_starting(admin, "همه")
    return store, admin, account


async def test_the_orders_list_is_persian_on_its_buttons_and_header(app: App) -> None:
    _store, admin, _account = await _orders_list(app)

    (button,) = [b for b in admin.buttons if b.startswith("#1")]
    assert "فعال" in button and not LATIN_WORD.search(button), button
    header = admin.last_text.splitlines()[0]
    assert "صفحه 1" in header and "(1)" in header, header


async def test_the_order_detail_names_the_plan_and_the_payment_in_persian(app: App) -> None:
    store, admin, account = await _orders_list(app)

    await tap_starting(admin, "#1")

    detail = admin.last_text
    plan = store.plans["special_50"]
    (plan_line,) = [ln for ln in detail.splitlines() if ln.startswith("پلن:")]
    assert (
        report_plain(store.groups[plan.group].title) in plan_line
        and report_plain(plan.title) in plan_line
    ), plan_line
    (pay_line,) = [ln for ln in detail.splitlines() if ln.startswith("پرداخت:")]
    assert "تأیید" in pay_line and not LATIN_WORD.search(pay_line), pay_line
    from sqlalchemy import select

    for internal in await app.all(select(Plan.name)):
        assert str(internal) not in detail, detail
    assert account.username in detail  # the panel username is the one Latin thing left


@pytest.mark.parametrize("status", list(OrderStatus))
def test_every_order_status_has_a_persian_button(status: OrderStatus) -> None:
    from types import SimpleNamespace

    markup = orders_list_keyboard(
        [SimpleNamespace(id=7, status=status)], status_key="ALL", offset=0, limit=10, total=1
    )
    label = markup.inline_keyboard[0][0].text
    assert not LATIN_WORD.search(label), label


@pytest.mark.parametrize("status", list(PaymentStatus))
def test_every_payment_status_has_a_persian_label(status: PaymentStatus) -> None:
    label = ha.payment_status_fa(status)
    assert label and not LATIN_WORD.search(label), label

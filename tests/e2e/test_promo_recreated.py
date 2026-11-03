"""Section ۱۷: a code the owner deletes and makes again counts its uses from zero.

The owner's own path: the wizard, the 🗑 and its confirm, the wizard again; the customers' path:
the code typed at the promo step and the order placed. The list shows the new code's usage.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from bot.texts import texts
from database.models import PromoRedemption
from tests.e2e._s07b_helpers import tap_starting
from tests.e2e.harness import Actor, App, Store, browse_to_plan, digits, plain, publish_store, smoke
from tests.e2e.test_promo_rules_admin import NO, SKIP, _open_promos, _start_wizard


@pytest.fixture(autouse=True)
async def store(app: App) -> Store:
    return await publish_store(app)


async def _redemptions(app: App) -> int:
    async with app.session() as session:
        return int(await session.scalar(select(func.count()).select_from(PromoRedemption)) or 0)


async def _to_the_code_step(customer: Actor, store: Store, service: str) -> None:
    if not customer.messages:
        await customer.start()
    await browse_to_plan(customer, store, "special_50")
    await customer.send(service)


@smoke("۱۷.۲۵")
async def test_a_code_the_owner_deleted_and_made_again_starts_its_count_from_zero(
    app: App, store: Store
) -> None:
    admin = app.admin()
    await _start_wizard(admin, "ONE 20")
    await tap_starting(admin, NO)
    await tap_starting(admin, SKIP)
    await admin.send("1")
    await tap_starting(admin, SKIP)

    ali = app.customer("ali")
    await _to_the_code_step(ali, store, "ali12")
    await ali.send("one")
    await ali.tap("ثبت سفارش")
    assert await _redemptions(app) == 1
    sara = app.customer("sara")
    await _to_the_code_step(sara, store, "sara12")
    await sara.send("one")
    assert sara.last_text == plain(texts.purchase_promo_used_up), sara.last_text

    # The owner deletes the code and makes it again, with the same single use.
    await _open_promos(admin)
    await tap_starting(admin, "🗑")
    await tap_starting(admin, "🗑 بله")
    assert "کدهای تخفیف (0)" in digits(admin.last_text), admin.last_text
    await tap_starting(admin, "➕")
    await admin.send("ONE 20")
    await tap_starting(admin, NO)
    await tap_starting(admin, SKIP)
    await admin.send("1")
    await tap_starting(admin, SKIP)
    assert "مصرف: 0 از 1" in digits(admin.last_text), admin.last_text

    price = store.plans["special_50"].price
    await _to_the_code_step(sara, store, "sara34")
    await sara.send("one")
    assert str(price - price * 20 // 100) in digits(sara.last_text), sara.last_text
    await sara.tap("ثبت سفارش")
    assert await _redemptions(app) == 2, "ali's row of the deleted code, and sara's of the new one"

    # The new code is used up by sara: a third customer is refused as usual.
    omid = app.customer("omid")
    await _to_the_code_step(omid, store, "omid12")
    await omid.send("one")
    assert omid.last_text == plain(texts.purchase_promo_used_up), omid.last_text

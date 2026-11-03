"""A renewal asks for a promo code like any other purchase (owner, 2026-10-03).

«📅 تمدید این سرویس» used to go straight to the summary, so a retail customer
with a code could not use it on a renewal. A reseller is still not asked
(checkout drops a code for them). What is asserted is what the person sees and
what the stored order carries; the renewal must still land on the same panel user.
"""

from __future__ import annotations

import pytest

from bot.texts import texts
from database.models import OrderStatus
from tests.e2e._s07b_helpers import (
    approve_order,
    buy_ready,
    deliver_by_card,
    make_shop,
    tap_starting,
)
from tests.e2e._s08_helpers import buy_active, expire_in, my_service
from tests.e2e._s09_helpers import open_store, skip_promo_rules
from tests.e2e.harness import App, Store, digits, plain, publish_store
from tests.e2e.harness.expectations import final_amount_line

RETAIL = 150_000
AGREED = 100_000
PROMO_QUESTION = plain(texts.purchase_enter_promo)
SKIP_PROMO = "🎫 بدون کد تخفیف"


def _renewal_line(name: str) -> str:
    return plain(texts.purchase_summary_renew.format(name=name))


@pytest.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


async def _create_code(app: App, code_line: str) -> None:
    admin = app.admin()
    await open_store(admin)
    await tap_starting(admin, "🏷")
    await tap_starting(admin, "🏷 مدیریت")
    await tap_starting(admin, "➕")
    await admin.send(code_line)
    await skip_promo_rules(admin)


def _is_summary(actor) -> bool:  # type: ignore[no-untyped-def]
    return actor.last_text.startswith("🧾") and any("ثبت سفارش" in b for b in actor.buttons)


async def _to_the_prompt(app: App, store: Store, ali):  # type: ignore[no-untyped-def]
    account = await buy_active(app, store, ali)
    expire_in(account, days=10)
    await my_service(ali)
    await tap_starting(ali, "📅 تمدید این سرویس")
    return account


async def test_a_retail_renew_shows_the_promo_prompt(app: App, store: Store) -> None:
    ali = app.customer("ali")
    await _to_the_prompt(app, store, ali)
    assert ali.last_text == PROMO_QUESTION
    assert SKIP_PROMO in ali.buttons
    assert not _is_summary(ali)


async def test_renewing_without_a_code_reaches_the_summary_at_full_price(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    account = await _to_the_prompt(app, store, ali)
    await ali.tap(SKIP_PROMO)
    assert _is_summary(ali)
    assert _renewal_line(account.username) in ali.last_text.splitlines(), ali.last_text
    assert str(RETAIL) in digits(final_amount_line(ali.last_text)), ali.last_text
    assert "🔁 پلن دیگه روی همین سرویس" in ali.buttons
    await ali.tap("ثبت سفارش")
    assert (await app.latest_order(ali)).price_toman == RETAIL


async def test_a_valid_code_lowers_the_renew_price_and_the_renewal_lands_on_the_same_user(
    app: App, store: Store
) -> None:
    await _create_code(app, "SAVE15 15")
    ali = app.customer("ali")
    account = await _to_the_prompt(app, store, ali)
    username, token = account.username, account.token
    before = account.expire
    await ali.send("save15")

    expected = RETAIL - RETAIL * 15 // 100
    assert expected == 127_500
    assert _is_summary(ali)
    assert _renewal_line(username) in ali.last_text.splitlines(), ali.last_text
    assert str(expected) in digits(final_amount_line(ali.last_text)), ali.last_text
    assert "🔁 پلن دیگه روی همین سرویس" in ali.buttons

    await ali.tap("ثبت سفارش")
    order = await app.latest_order(ali)
    assert order.price_toman == expected
    assert order.renew_account_id is not None
    await ali.send_photo()
    await approve_order(app, order.id)
    assert (await app.latest_order(ali)).status == OrderStatus.ACTIVE
    assert list(app.panel.users) == [username], "no second account"
    assert app.panel.user(username).token == token
    assert app.panel.user(username).expire > before, "the expiry moved"


async def test_a_wrong_code_on_a_renew_is_refused_like_on_a_new_purchase(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await _to_the_prompt(app, store, ali)
    await ali.send("NOSUCHCODE")
    assert ali.last_text == plain(texts.purchase_invalid_promo)
    assert SKIP_PROMO in ali.buttons, "still on the prompt"
    await ali.tap(SKIP_PROMO)
    assert _is_summary(ali) and str(RETAIL) in digits(final_amount_line(ali.last_text))


async def test_a_reseller_renew_shows_no_prompt(app: App, store: Store) -> None:
    shop, _ = await make_shop(app, prices=(("ویژه", "۵۰ گیگ / ۳۰ روز", AGREED),))
    first = await buy_ready(
        shop, app, "حجمی/ویژه", "۵۰ گیگ / ۳۰ روز", "Reza Mohammadi", "09123456789"
    )
    await deliver_by_card(app, shop, first)
    await shop.tap_reply("👥 مشتری‌های من")
    await tap_starting(shop, "🟢 reza_mohammadi")
    mark = shop.mark()
    await shop.tap("📅 تمدید این سرویس")
    assert PROMO_QUESTION not in shop.texts_since(mark)
    assert _is_summary(shop) and str(AGREED) in digits(final_amount_line(shop.last_text))

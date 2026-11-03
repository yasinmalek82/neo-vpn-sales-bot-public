"""Owner decisions on the purchase flow (2026-10-03).

1. A reseller is not asked for a promo code: checkout drops it for them anyway, so
   the question promised something the bot never kept.
2. The summary of a renewal says it is a renewal, and of which service, by the
   name «سرویس‌های من» shows for it.

What is asserted is what the person sees and what the order stores.
"""

from __future__ import annotations

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from sqlalchemy import select

from bot.keyboards.purchase import PurchaseCB
from bot.keyboards.reply_menu import ACCOUNT, BUY
from bot.states import PurchaseStates
from bot.texts import texts
from database.models import PasarGuardAccount
from tests.e2e._s07b_helpers import (
    buy_ready,
    deliver_by_card,
    make_shop,
    tap_starting,
)
from tests.e2e._s08_helpers import buy_active, my_service, retire_plan
from tests.e2e._s09_helpers import open_store, skip_promo_rules
from tests.e2e.harness import App, Store, browse_to_plan, digits, plain, publish_store
from tests.e2e.harness.actors import Actor
from tests.e2e.harness.expectations import final_amount_line

GROUP = "حجمی/ویژه"
PLAN = "۵۰ گیگ / ۳۰ روز"
RETAIL = 150_000
AGREED = 100_000
PROMO_QUESTION = plain(texts.purchase_enter_promo)
SKIP_PROMO = "بدون کد تخفیف"
RENEWAL = "📅 تمدید سرویس:"


@pytest.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


def _shown_promo_question(actor: Actor, mark: int) -> bool:
    return PROMO_QUESTION in actor.texts_since(mark)


def _is_summary(actor: Actor) -> bool:
    return actor.last_text.startswith("🧾") and any("ثبت سفارش" in b for b in actor.buttons)


async def _shop_walks_to_the_phone_step(shop: Actor, name: str = "Reza Mohammadi") -> None:
    """«خرید اشتراک» → ویژه → the agreed-price plan → the customer's name."""
    await shop.tap_reply(BUY)
    await shop.tap("حجمی")
    await shop.tap("ویژه")
    await tap_starting(shop, PLAN)
    await shop.send(name)


async def _service_row_on_my_services(actor: Actor) -> tuple[str, str]:
    """«سرویس‌های من»: the first service's button, and its name without the status dot."""
    await actor.tap_reply(ACCOUNT)
    row = next(b for b in actor.buttons if b[:1] in "🟢🟡🔴⛔⚪")
    return row, row.split(" ", 1)[1]


async def _force_fsm(app: App, actor: Actor, state: object, **data: object) -> None:
    """Put someone's conversation in a state the buttons no longer lead to."""
    storage = app.dp.fsm.storage
    key = StorageKey(bot_id=app.bot.id, chat_id=actor.user_id, user_id=actor.user_id)
    context = FSMContext(storage=storage, key=key)
    await context.set_state(state)  # type: ignore[arg-type]
    await context.update_data(**data)


async def _create_code(app: App, code_line: str) -> None:
    admin = app.admin()
    await open_store(admin)
    await tap_starting(admin, "🏷")
    await tap_starting(admin, "🏷 مدیریت")
    await tap_starting(admin, "➕")
    await admin.send(code_line)
    await skip_promo_rules(admin)


# --------------------------------------------------------------- change 1: promo


async def test_a_reseller_buying_a_plan_is_never_asked_for_a_promo_code(
    app: App, store: Store
) -> None:
    shop, _ = await make_shop(app, prices=(("ویژه", PLAN, AGREED),))
    mark = shop.mark()
    await _shop_walks_to_the_phone_step(shop)
    await shop.tap(texts.reseller_label_skip_phone)

    assert not _shown_promo_question(shop, mark), shop.texts_since(mark)
    assert _is_summary(shop), (shop.last_text, shop.buttons)
    assert SKIP_PROMO not in " ".join(shop.buttons)
    assert "مشتری: reza_mohammadi" in shop.last_text
    assert str(AGREED) in digits(final_amount_line(shop.last_text))

    await shop.tap("✅ ثبت سفارش")
    order = await app.latest_order(shop)
    assert order.price_toman == AGREED, "the reseller's agreed price, not retail"
    assert order.price_toman != RETAIL


async def test_a_retail_customer_is_still_asked_and_a_valid_code_still_lowers_the_price(
    app: App, store: Store
) -> None:
    await _create_code(app, "SAVE15 15")
    ali = app.customer("ali")
    await ali.start()
    await browse_to_plan(ali, store, "special_50")
    mark = ali.mark()
    await ali.send("ali12")

    assert ali.last_text == PROMO_QUESTION
    assert SKIP_PROMO in " ".join(ali.buttons)
    await ali.send("save15")
    expected = RETAIL - RETAIL * 15 // 100
    assert expected == 127_500
    assert _is_summary(ali)
    assert str(expected) in digits(final_amount_line(ali.last_text)), ali.last_text
    await ali.tap("ثبت سفارش")
    assert (await app.latest_order(ali)).price_toman == expected
    assert PROMO_QUESTION in ali.texts_since(mark)


async def test_a_retail_customer_who_skips_the_code_pays_retail(app: App, store: Store) -> None:
    ali = app.customer("ali")
    await ali.start()
    await browse_to_plan(ali, store, "special_50")
    await ali.send("ali12")
    assert ali.last_text == PROMO_QUESTION
    await ali.tap(SKIP_PROMO)
    assert _is_summary(ali)
    await ali.tap("ثبت سفارش")
    assert (await app.latest_order(ali)).price_toman == RETAIL


async def test_a_reseller_renewing_from_a_customers_card_is_not_asked_for_a_code(
    app: App, store: Store
) -> None:
    """The quick renew: card → «📅 تمدید این سرویس» → the summary at the agreed price."""
    shop, _ = await make_shop(app, prices=(("ویژه", PLAN, AGREED),))
    first = await buy_ready(shop, app, GROUP, PLAN, "Reza Mohammadi", "09123456789")
    await deliver_by_card(app, shop, first)

    await shop.tap_reply("👥 مشتری‌های من")
    await tap_starting(shop, "🟢 reza_mohammadi")
    mark = shop.mark()
    await shop.tap("📅 تمدید این سرویس")

    assert not _shown_promo_question(shop, mark), shop.texts_since(mark)
    assert _is_summary(shop) and str(AGREED) in digits(shop.last_text)
    await shop.tap("✅ ثبت سفارش")
    assert (await app.latest_order(shop)).price_toman == AGREED


async def test_a_reseller_switching_a_customers_plan_is_not_asked_for_a_code(
    app: App, store: Store
) -> None:
    """The customer's plan is off sale: renew → the store on the same link → pick a plan."""
    shop, _ = await make_shop(app, prices=(("ویژه", PLAN, AGREED),))
    first = await buy_ready(shop, app, GROUP, PLAN, "Reza Mohammadi", "09123456789")
    await deliver_by_card(app, shop, first)
    await retire_plan(app, store, "special_50")

    await shop.tap_reply("👥 مشتری‌های من")
    await tap_starting(shop, "🟢 reza_mohammadi")
    await shop.tap("📅 تمدید این سرویس")
    assert shop.last_text == plain(texts.purchase_switch_plan_gone)
    mark = shop.mark()
    await tap_starting(shop, store.groups["volume"].title)
    await tap_starting(shop, store.groups["economy"].title)
    await tap_starting(shop, store.plans["economy_50"].title)

    assert not _shown_promo_question(shop, mark), shop.texts_since(mark)
    assert _is_summary(shop), (shop.last_text, shop.buttons)
    assert plain(texts.purchase_switch_note) in shop.last_text
    assert str(store.plans["economy_50"].price) in digits(shop.last_text)
    await shop.tap("✅ ثبت سفارش")
    order = await app.latest_order(shop)
    assert order.price_toman == store.plans["economy_50"].price


async def _shop_with_a_customer_and_a_second_sale_in_the_air(
    app: App, store: Store
) -> tuple[Actor, int]:
    """A shop whose customer Reza owns a service, and who is now buying for Ali.

    Returns the shop and Reza's account id. The conversation stops where the bot
    leaves it after Ali's name and phone.
    """
    shop, _ = await make_shop(app, prices=(("ویژه", PLAN, AGREED),))
    first = await buy_ready(shop, app, GROUP, PLAN, "Reza Mohammadi", "09123456789")
    await deliver_by_card(app, shop, first)
    reza = await app.one(select(PasarGuardAccount))
    await _shop_walks_to_the_phone_step(shop, "Ali")
    await shop.tap(texts.reseller_label_skip_phone)
    return shop, int(reza.id)


async def test_a_reseller_answering_renew_same_is_not_asked_for_a_code(
    app: App, store: Store
) -> None:
    """A shop is never offered «تمدید یا جدید» (it matches plan shape, not a person), but a
    stale or forged button still reaches the handler: it must not ask either."""
    shop, reza_id = await _shop_with_a_customer_and_a_second_sale_in_the_air(app, store)
    await _force_fsm(
        app, shop, PurchaseStates.choosing_renew_mode, matched_renew_account_id=reza_id
    )
    mark = shop.mark()
    await shop.press(PurchaseCB.RENEW_SAME)

    assert not _shown_promo_question(shop, mark), shop.texts_since(mark)
    assert _is_summary(shop), (shop.last_text, shop.buttons)
    assert SKIP_PROMO not in " ".join(shop.buttons)
    assert f"{RENEWAL} reza_mohammadi | 09123456789" in shop.last_text
    assert str(AGREED) in digits(final_amount_line(shop.last_text))


async def test_a_reseller_answering_renew_new_is_not_asked_for_a_code(
    app: App, store: Store
) -> None:
    shop, reza_id = await _shop_with_a_customer_and_a_second_sale_in_the_air(app, store)
    await _force_fsm(
        app, shop, PurchaseStates.choosing_renew_mode, matched_renew_account_id=reza_id
    )
    mark = shop.mark()
    await shop.press(PurchaseCB.RENEW_NEW)

    assert not _shown_promo_question(shop, mark), shop.texts_since(mark)
    assert _is_summary(shop), (shop.last_text, shop.buttons)
    assert RENEWAL not in shop.last_text
    assert "مشتری: ali" in shop.last_text
    await shop.tap("✅ ثبت سفارش")
    order = await app.latest_order(shop)
    assert order.price_toman == AGREED


# ------------------------------------------------------------ change 2: renewal


async def test_renewing_from_my_services_says_it_is_a_renewal_and_of_which_service(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    account = await buy_active(app, store, ali)
    row, label = await _service_row_on_my_services(ali)
    assert label == account.username
    await ali.tap(row)
    await tap_starting(ali, "تمدید این سرویس")
    assert ali.last_text == PROMO_QUESTION, "a retail customer is asked, renewing too"
    await ali.tap(SKIP_PROMO)

    assert _is_summary(ali)
    assert f"{RENEWAL} {label}" in ali.last_text.splitlines(), ali.last_text
    assert str(RETAIL) in digits(ali.last_text)


async def test_buying_the_same_plan_again_and_choosing_renew_names_that_service(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    account = await buy_active(app, store, ali)
    await browse_to_plan(ali, store, "special_50")
    await ali.send("bobo")
    assert ali.last_text == plain(texts.purchase_renew_or_new)
    await ali.tap("📅 تمدید همان سرویس")
    assert ali.last_text == PROMO_QUESTION, "a retail customer is still asked"
    await ali.tap(SKIP_PROMO)

    assert _is_summary(ali)
    assert f"{RENEWAL} {account.username}" in ali.last_text.splitlines(), ali.last_text
    await ali.tap("ثبت سفارش")
    renewal = await app.latest_order(ali)
    assert renewal.price_toman == RETAIL


async def test_a_reseller_renewing_a_customer_keeps_the_label_and_names_the_service(
    app: App, store: Store
) -> None:
    shop, _ = await make_shop(app, prices=(("ویژه", PLAN, AGREED),))
    first = await buy_ready(shop, app, GROUP, PLAN, "Reza Mohammadi", "09123456789")
    await deliver_by_card(app, shop, first)

    await shop.tap_reply("👥 مشتری‌های من")
    shown = shop.buttons[0].split(" ", 1)[1]
    assert shown == "reza_mohammadi | 09123456789"
    await tap_starting(shop, "🟢 reza_mohammadi")
    await shop.tap("📅 تمدید این سرویس")

    assert _is_summary(shop)
    assert f"{RENEWAL} {shown}" in shop.last_text.splitlines(), shop.last_text
    assert str(AGREED) in digits(final_amount_line(shop.last_text))


async def test_a_first_purchase_summary_has_no_renewal_line(app: App, store: Store) -> None:
    ali = app.customer("ali")
    await ali.start()
    await browse_to_plan(ali, store, "special_50")
    await ali.send("ali12")
    await ali.tap(SKIP_PROMO)

    assert _is_summary(ali)
    assert "نام سرویس: ali12" in ali.last_text
    assert RENEWAL not in ali.last_text and "تمدید" not in ali.last_text


async def test_choosing_a_new_service_next_to_an_existing_one_has_no_renewal_line(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await buy_active(app, store, ali)
    await browse_to_plan(ali, store, "special_50")
    await ali.send("second")
    await ali.tap("✨ سرویس جدید")
    await ali.tap(SKIP_PROMO)

    assert _is_summary(ali)
    assert "نام سرویس: second" in ali.last_text
    assert RENEWAL not in ali.last_text
    await ali.tap("ثبت سفارش")
    order = await app.latest_order(ali)
    assert order.price_toman == RETAIL


async def test_a_plan_switch_keeps_its_note_and_is_not_called_a_renewal(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await buy_active(app, store, ali, "special_50")
    await retire_plan(app, store, "special_50")
    await my_service(ali)
    await tap_starting(ali, "تمدید این سرویس")
    assert ali.last_text == plain(texts.purchase_switch_plan_gone)
    await tap_starting(ali, store.groups["volume"].title)
    await tap_starting(ali, store.groups["economy"].title)
    await tap_starting(ali, store.plans["economy_50"].title)
    await tap_starting(ali, "بدون کد")

    assert _is_summary(ali)
    assert plain(texts.purchase_switch_note) in ali.last_text
    assert RENEWAL not in ali.last_text

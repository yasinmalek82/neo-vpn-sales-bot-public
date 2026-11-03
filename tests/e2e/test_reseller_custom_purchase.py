"""Custom purchase for resellers, with a typed volume (plans/2026-10-03-reseller-custom-purchase.md).

Rows ۵.۷ to ۵.۱۰, ۷ب.۶الف to ۷ب.۶د and ۹.۲۲. The group's custom setting has three states
(off, resellers only, everyone); a reseller needs a per-gigabyte rate to be offered the
builder; the volume can be typed (a whole number, 1 to 1000).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from bot.keyboards.purchase import PurchaseCB
from bot.texts import texts
from database.models import Order, OrderStatus
from services.store_admin import (
    commit_and_activate,
    create_draft_category,
    update_draft_category,
)
from tests.e2e._s01_helpers import open_shelf
from tests.e2e._s07b_helpers import (
    add_custom_volume,
    make_shop,
    open_card,
    open_prices,
    set_rates,
    tap_starting,
)
from tests.e2e._s09_helpers import live_catalog, open_store
from tests.e2e.harness import GB, Actor, App, Store, digits, plain, publish_store, smoke
from tests.e2e.harness.expectations import report_plain

RATE_GB, RATE_DAY = 1200, 500
OFF, RESELLERS, EVERYONE = "off", "resellers", "everyone"
VOLUME_QUESTION = report_plain(
    texts.purchase_choose_volume
    + "\n"
    + texts.purchase_volume_type_hint.format(low="۱", high="۱۰۰۰")
)


@pytest.fixture
async def store(app: App) -> Store:
    s = await publish_store(app)
    await add_custom_volume(app, s)
    return s


async def _set_custom(app: App, store: Store, mode: str, group: str = "special") -> None:
    """The owner's setting, through the same write the group's button makes."""
    async with app.session() as session:
        await update_draft_category(session, store.category_ids[group], custom_purchase_mode=mode)
        await commit_and_activate(session, notes="خرید سفارشی عوض شد")


def _sees_custom(actor: Actor) -> bool:
    return any(b.startswith("🧩") for b in actor.buttons)


async def _special_shelf_shows_custom(actor: Actor) -> bool:
    await open_shelf(actor, "حجمی", "ویژه")
    return _sees_custom(actor)


async def _to_volume_question(actor: Actor) -> None:
    await open_shelf(actor, "حجمی", "ویژه")
    await tap_starting(actor, "🧩")
    assert actor.last_text == VOLUME_QUESTION, actor.last_text


async def _orders(app: App) -> list[Order]:
    return await app.all(select(Order))


async def _buyers(app: App) -> dict[str, Actor]:
    """A retail customer and three shops: a per-GB rate, no rate, a per-day rate only."""
    ali = app.customer("ali")
    await ali.start()
    with_rate, _ = await make_shop(app, "rate", rates=(RATE_GB, RATE_DAY))
    no_rate, _ = await make_shop(app, "norate")
    day_only, _ = await make_shop(app, "dayonly", rates=(0, RATE_DAY))
    return {"retail": ali, "rate": with_rate, "none": no_rate, "day": day_only}


SEES = {
    OFF: {"retail": False, "rate": False, "none": False, "day": False},
    RESELLERS: {"retail": False, "rate": True, "none": False, "day": False},
    EVERYONE: {"retail": True, "rate": True, "none": False, "day": False},
}


@smoke("۵.۷")
@pytest.mark.parametrize("mode", [OFF, RESELLERS, EVERYONE])
async def test_the_shelf_offers_custom_by_the_groups_state_and_the_buyers_rate(
    app: App, store: Store, mode: str
) -> None:
    buyers = await _buyers(app)
    await _set_custom(app, store, mode)
    seen = {name: await _special_shelf_shows_custom(actor) for name, actor in buyers.items()}
    assert seen == SEES[mode], f"state {mode}: who saw «🧩 خرید سفارشی» -> {seen}"


@smoke("۵.۷")
async def test_a_group_with_no_ready_plans_is_entered_by_whoever_is_offered_custom(
    app: App, store: Store
) -> None:
    """The direct entry asks the same question: custom-only, for resellers only."""
    async with app.session() as session:
        cat = await create_draft_category(session, title_fa="سفارشی‌ها", kind="volume")
        await update_draft_category(
            session, cat.id, panel_group_ids="11", custom_purchase_mode=RESELLERS
        )
        await commit_and_activate(session, notes="گروه فقط سفارشی")
    buyers = await _buyers(app)
    listed = {}
    for name, actor in buyers.items():
        await actor.tap_reply("🛒 خرید اشتراک")
        listed[name] = any(b.startswith("سفارشی‌ها") for b in actor.buttons)
    assert listed == {"retail": False, "rate": True, "none": False, "day": False}, listed

    shop = buyers["rate"]
    await shop.tap_reply("🛒 خرید اشتراک")
    await tap_starting(shop, "سفارشی‌ها")
    assert shop.last_text == VOLUME_QUESTION, shop.last_text

    # A forged tap on that group by someone who is not offered it opens nothing.
    cid = next(c.id for c in (await live_catalog(app)).categories if c.title_fa == "سفارشی‌ها")
    for name in ("retail", "none"):
        actor = buyers[name]
        await actor.tap_reply("🛒 خرید اشتراک")
        mark = actor.mark()
        await actor.press(f"{PurchaseCB.CAT_PREFIX}{cid}")
        assert VOLUME_QUESTION not in actor.texts_since(mark), name
    assert await _orders(app) == []


def _custom_button(admin: Actor) -> str:
    labels = [b for b in admin.buttons if "خرید سفارشی" in b and not b.startswith("👥")]
    assert len(labels) == 1, admin.buttons
    return labels[0]


@smoke("۹.۲۲")
async def test_the_group_button_cycles_off_resellers_everyone_and_the_shelf_follows(
    app: App, store: Store
) -> None:
    from tests.e2e._s09_helpers import drafts

    shop, _ = await make_shop(app, "rate", rates=(RATE_GB, RATE_DAY))
    ali = app.customer("ali")
    await ali.start()
    admin = app.admin()
    await open_store(admin)
    await tap_starting(admin, "حجمی")
    await tap_starting(admin, "ویژه")
    assert "خرید سفارشی: همه" in admin.last_text, "the dashboard says who sees custom"
    await tap_starting(admin, "🛒 فروش")
    assert _custom_button(admin) == "✅ خرید سفارشی: همه"
    assert any(b.startswith("👥 کاربر همزمان خرید سفارشی") for b in admin.buttons)

    steps = [
        (OFF, "❌ خرید سفارشی", "خرید سفارشی: خاموش", False, False),
        (RESELLERS, "🏪 خرید سفارشی: فقط نماینده‌ها", "خرید سفارشی: فقط نماینده‌ها", False, True),
        (EVERYONE, "✅ خرید سفارشی: همه", "خرید سفارشی: همه", True, True),
    ]
    for mode, label, dash_line, retail_sees, shop_sees in steps:
        await admin.tap(_custom_button(admin))
        assert _custom_button(admin) == label, (mode, admin.buttons)
        assert plain(texts.admin_store_saved) in admin.last_text
        assert any(b.startswith("👥 کاربر همزمان خرید سفارشی") for b in admin.buttons) == (
            mode != OFF
        ), "the custom device limit shows only while custom is not off"
        assert await _special_shelf_shows_custom(ali) is retail_sees, mode
        assert await _special_shelf_shows_custom(shop) is shop_sees, mode
        await tap_starting(admin, "⬅️ گروه")
        assert dash_line in admin.last_text, admin.last_text
        await tap_starting(admin, "🛒 فروش")
    assert await drafts(app) == 0, "the setting went live at once, no draft left behind"


async def _refused_with_no_builder(app: App, actor: Actor, mark: int) -> None:
    said = actor.texts_since(mark)
    assert plain(texts.purchase_custom_not_offered) in said, said
    assert VOLUME_QUESTION not in said, said
    assert plain(texts.purchase_choose_duration_months) not in said, said
    assert await _orders(app) == []


@smoke("۵.۸")
async def test_a_forged_custom_press_by_a_retail_customer_in_a_resellers_only_group_is_refused(
    app: App, store: Store
) -> None:
    await _set_custom(app, store, RESELLERS)
    ali = app.customer("ali")
    await ali.start()
    await open_shelf(ali, "حجمی", "ویژه")
    assert not _sees_custom(ali)
    mark = ali.mark()
    await ali.press(PurchaseCB.CUSTOM)
    await _refused_with_no_builder(app, ali, mark)


@smoke("۵.۸")
async def test_a_forged_custom_press_by_a_shop_with_no_per_gb_rate_is_refused(
    app: App, store: Store
) -> None:
    shop, _ = await make_shop(app, "norate")
    await open_shelf(shop, "حجمی", "ویژه")
    assert not _sees_custom(shop)
    mark = shop.mark()
    await shop.press(PurchaseCB.CUSTOM)
    await _refused_with_no_builder(app, shop, mark)


@smoke("۵.۸")
async def test_a_builder_left_open_when_the_owner_narrows_the_setting_stops_at_the_next_tap(
    app: App, store: Store
) -> None:
    """Retail opened the builder while custom was for everyone; then it became resellers-only."""
    ali = app.customer("ali")
    await ali.start()
    await _to_volume_question(ali)
    await _set_custom(app, store, RESELLERS)
    mark = ali.mark()
    await ali.press(f"{PurchaseCB.VOL_PREFIX}50")
    await _refused_with_no_builder(app, ali, mark)

    # Typed volume, duration and unlimited-builder presses are no different.
    await _set_custom(app, store, EVERYONE)
    await _to_volume_question(ali)
    await _set_custom(app, store, RESELLERS)
    mark = ali.mark()
    await ali.send("37")
    await _refused_with_no_builder(app, ali, mark)


@smoke("۵.۸")
async def test_a_stale_duration_press_after_the_setting_is_narrowed_is_refused(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await ali.start()
    await _to_volume_question(ali)
    await ali.send("37")
    assert ali.last_text == plain(texts.purchase_choose_duration_months)
    await _set_custom(app, store, OFF)
    mark = ali.mark()
    await ali.press(f"{PurchaseCB.DAYS_PREFIX}30")
    await _refused_with_no_builder(app, ali, mark)


# --------------------------------------------------------------------------- #
# The typed volume
# --------------------------------------------------------------------------- #


async def _typed_summary(shop: Actor, typed: str, *, label: str = "Reza") -> None:
    """Shelf -> builder -> typed volume -> «۱ ماه» -> the customer -> the summary."""
    await _to_volume_question(shop)
    await shop.send(typed)
    await tap_starting(shop, "۱ ماه")
    await shop.send(label)
    await shop.tap(texts.reseller_label_skip_phone)


@smoke("۵.۹")
async def test_the_volume_question_shows_the_quick_buttons_and_asks_for_a_number(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await ali.start()
    await _to_volume_question(ali)
    assert ali.buttons[:3] == ["۳۰ گیگ", "۵۰ گیگ", "۱۰۰ گیگ"]
    assert "عدد" in ali.last_text and "1000" in ali.last_text, ali.last_text


@smoke("۵.۹")
@pytest.mark.parametrize(
    "typed",
    ["0", "۰", "-5", "-۵", "12.5", "۱۲٫۵", "abc", "بیست", "37 گیگ", "1001", "۱۰۰۱", "99999"],
)
async def test_a_volume_that_is_not_a_whole_number_from_1_to_1000_is_refused_at_the_question(
    app: App, store: Store, typed: str
) -> None:
    ali = app.customer("ali")
    await ali.start()
    await _to_volume_question(ali)
    mark = ali.mark()
    await ali.send(typed)
    said = ali.texts_since(mark)
    assert len(said) == 1, said
    assert "1" in digits(said[0]) and "1000" in digits(said[0]), "the refusal states the range"
    assert "Traceback" not in said[0] and "invalid literal" not in said[0]
    assert plain(texts.purchase_choose_duration_months) not in said
    assert "۵۰ گیگ" in ali.buttons, "still on the question, the quick buttons are there"
    # Still on the same question: a right answer now goes on.
    await ali.send("37")
    assert ali.last_text == plain(texts.purchase_choose_duration_months)
    assert await _orders(app) == []


@smoke("۵.۹")
@pytest.mark.parametrize(("typed", "gb"), [("1", 1), ("۱۰۰۰", 1000), ("37", 37), ("۳۷", 37)])
async def test_a_whole_number_inside_the_limits_goes_on_to_the_duration(
    app: App, store: Store, typed: str, gb: int
) -> None:
    ali = app.customer("ali")
    await ali.start()
    await _to_volume_question(ali)
    await ali.send(typed)
    assert ali.last_text == plain(texts.purchase_choose_duration_months)
    await tap_starting(ali, "۱ ماه")
    await ali.send("ali12")
    await ali.tap("بدون کد تخفیف")
    assert f"📦 حجم: {gb}" in ali.last_text


@smoke("۵.۱۰")
async def test_a_retail_customer_pays_the_retail_price_for_a_typed_volume(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await ali.start()
    await app.give_wallet(ali, 300_000)
    await _to_volume_question(ali)
    await ali.send("37")
    await tap_starting(ali, "۱ ماه")
    await ali.send("ali12")
    await ali.tap("بدون کد تخفیف")
    assert "37 گیگ" in ali.last_text
    assert "222000" in digits(ali.last_text), ali.last_text  # 37 GB x 6 000, the same function
    await ali.tap("ثبت سفارش")
    order = await app.latest_order(ali)
    assert order.price_toman == 37 * 6000
    assert order.delivery_data_limit_gb == 37
    assert app.panel.only_user().data_limit == 37 * GB


@smoke("۷ب.۶الف")
async def test_a_shops_typed_volume_is_priced_at_its_rate_and_delivered_with_that_limit(
    app: App, store: Store
) -> None:
    shop, _ = await make_shop(app, "rez", rates=(RATE_GB, RATE_DAY), wallet=100_000)
    await _typed_summary(shop, "۳۷")
    expected = 37 * RATE_GB + 30 * RATE_DAY
    assert expected == 59_400
    assert "📦 حجم: 37 گیگ" in shop.last_text, shop.last_text
    assert "59400" in digits(shop.last_text.splitlines()[-1]) or "59400" in digits(shop.last_text)
    await shop.tap("✅ ثبت سفارش")
    order = await app.latest_order(shop)
    assert order.status == OrderStatus.ACTIVE, "paid from the wallet, delivered at once"
    assert order.price_toman == expected
    assert order.delivery_data_limit_gb == 37
    assert await app.wallet_balance(shop) == 100_000 - expected
    assert app.panel.only_user().data_limit == 37 * GB, "the panel got exactly the typed volume"


@smoke("۷ب.۶ب")
async def test_two_shops_with_different_rates_pay_different_prices_for_the_same_volume(
    app: App, store: Store
) -> None:
    cheap, _ = await make_shop(app, "cheap", rates=(1000, RATE_DAY))
    dear, _ = await make_shop(app, "dear", rates=(1500, RATE_DAY))
    prices = {}
    for name, shop in (("cheap", cheap), ("dear", dear)):
        await _typed_summary(shop, "37", label=name)
        await shop.tap("✅ ثبت سفارش")
        prices[name] = (await app.latest_order(shop)).price_toman
    assert prices == {"cheap": 37 * 1000 + 30 * RATE_DAY, "dear": 37 * 1500 + 30 * RATE_DAY}


@smoke("۷ب.۶ج")
async def test_a_shop_with_no_per_gb_rate_has_no_custom_even_when_it_is_for_everyone(
    app: App, store: Store
) -> None:
    await _set_custom(app, store, EVERYONE)
    day_only, _ = await make_shop(app, "dayonly", rates=(0, RATE_DAY))
    assert not await _special_shelf_shows_custom(day_only)
    # The unlimited group is the same: no rate, no builder.
    await open_shelf(day_only, "نامحدود")
    assert not _sees_custom(day_only)


@smoke("۷ب.۶ج")
async def test_no_one_sees_custom_in_the_unlimited_group_whatever_it_is_set_to(
    app: App, store: Store
) -> None:
    """Resellers with a rate included: a per-GB rate has nothing to price there."""
    shop, _ = await make_shop(app, "rate", rates=(RATE_GB, RATE_DAY))
    ali = app.customer("ali")
    await ali.start()
    unlimited = store.category_ids["unlimited"]
    for mode in (EVERYONE, RESELLERS, OFF):
        async with app.session() as session:
            await update_draft_category(session, unlimited, custom_purchase_mode=mode)
            await commit_and_activate(session, notes="خرید سفارشی عوض شد")
        for buyer in (shop, ali):
            await open_shelf(buyer, "نامحدود")
            assert not _sees_custom(buyer), (mode, buyer.buttons)
            await buyer.press(PurchaseCB.CUSTOM)
            assert buyer.last_text == plain(texts.purchase_custom_not_offered), buyer.last_text


@smoke("۷ب.۶د")
async def test_the_owners_card_says_the_per_gb_rate_is_missing_until_he_sets_one(
    app: App, store: Store
) -> None:
    shop, admin = await make_shop(app, "rez")
    await open_card(admin, "rez")
    await open_prices(admin)
    assert "نرخ هر گیگ تعریف نشده" in admin.last_text, admin.last_text
    assert "خرید سفارشی برای این نماینده نشان داده نمی‌شود" in admin.last_text

    # A per-day rate alone does not make custom offerable: the line stays.
    await set_rates(admin, 0, RATE_DAY)
    assert "نرخ هر گیگ تعریف نشده" in admin.last_text, admin.last_text

    await set_rates(admin, RATE_GB, RATE_DAY)
    assert "نرخ هر گیگ تعریف نشده" not in admin.last_text, admin.last_text
    assert await _special_shelf_shows_custom(shop)

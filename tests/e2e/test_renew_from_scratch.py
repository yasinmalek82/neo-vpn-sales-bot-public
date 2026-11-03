"""«تمدید از نو» (plans/2026-10-04, ۱.۲), the group's «تمدید» switch (د۱), the trial's button.

Rows ۸.۳۲ to ۸.۴۴ (۷ب.۲۹ for the reseller's card; ۸.۱۱ is the switch fallback). A reseller taps
«📅 تمدید این سرویس» on a volume service no ready plan matches, sees one screen
(«همان ۳۷ گیگ • ۱ ماه — … تومان»), and the service is SET ANEW on the panel: expiry
today + days, usage zero, the bought volume; the panel groups and the device limit stay.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from bot.keyboards.account import AccountCB
from bot.keyboards.purchase import PurchaseCB
from bot.keyboards.reply_menu import ACCOUNT
from bot.texts import texts
from database.models import Order, OrderStatus, PasarGuardAccount, User
from database.models.order import RENEW_MODE_RESET
from services.store_admin import commit_and_activate, update_draft_category
from tests.e2e._s01_helpers import open_shelf
from tests.e2e._s07b_helpers import (
    add_custom_volume,
    approve_order,
    make_shop,
    tap_starting,
)
from tests.e2e._s08_helpers import buy_active, expire_in, my_service  # noqa: F401
from tests.e2e._s09_helpers import open_store, skip_promo_rules
from tests.e2e.harness import GB, Actor, App, Store, digits, plain, publish_store, smoke
from tests.e2e.harness.expectations import report_plain
from tests.e2e.harness.panel import PanelUser

RATE = 4500  # the old bot's reseller rate: toman per GB, nothing per day
PROMO_QUESTION = plain(texts.purchase_enter_promo)
RETAIL_PER_GB = 6000  # the pricing document's custom volume, up to 50 GB
RENEW = "📅 تمدید این سرویس"
SAME = "✅ همین"


@pytest.fixture
async def store(app: App) -> Store:
    s = await publish_store(app)
    await add_custom_volume(app, s)  # the pricing document gets its custom volume plan
    return s


# --------------------------------------------------------------------------- helpers


async def a_shop(
    app: App, *, rate: int = RATE, wallet: int = 2_000_000, name: str = "rez"
) -> Actor:
    shop, _ = await make_shop(app, name, rates=(rate, 0), wallet=wallet)
    return shop


async def buy_custom(app: App, shop: Actor, gb: int = 37, label: str = "Reza") -> PanelUser:
    """The shop buys a custom service by typing the volume (wallet-paid, delivered at once)."""
    await open_shelf(shop, "حجمی", "ویژه")
    await tap_starting(shop, "🧩")
    await shop.send(str(gb))
    await tap_starting(shop, "۱ ماه")
    await shop.send(label)
    await shop.tap(texts.reseller_label_skip_phone)
    await shop.tap("✅ ثبت سفارش")
    order = await app.latest_order(shop)
    assert order.status == OrderStatus.ACTIVE, order.status
    return max(app.panel.users.values(), key=lambda u: u.id)


async def import_service(
    app: App,
    owner: Actor,
    *,
    username: str = "mirza_u1",
    gb: int | None = 50,
    groups: tuple[int, ...] = (2, 6, 8),
    days: float = 10,
    used_gb: int = 20,
    hwid: int | None = 3,
) -> PanelUser:
    """A service that came from the old bot: a panel user and an account row, NO order."""
    app.panel.add_group(6, "g6")
    app.panel.add_group(8, "g8")
    user = PanelUser(
        id=7000 + len(app.panel.users),
        username=username,
        token=f"tok-{username}",
        created_at=datetime.now(UTC) - timedelta(days=20),
        used_traffic=used_gb * GB,
        expire=datetime.now(UTC) + timedelta(days=days, hours=12),
        data_limit=None if gb is None else gb * GB,
        group_ids=list(groups),
        hwid_limit=hwid,
    )
    app.panel.users[username] = user
    async with app.session() as session:
        db_user = await session.scalar(select(User).where(User.telegram_id == owner.user_id))
        session.add(
            PasarGuardAccount(
                user_id=db_user.id,
                panel_user_id=user.id,
                panel_username=username,
                display_name=username,  # the import names it, or «سرویس‌های من» hides it
                subscription_url=f"{app.panel.base_url}/sub/{user.token}",
            )
        )
        await session.commit()
    return user


async def open_service_of(actor: Actor) -> None:
    """Customer: «سرویس‌های من» → the service. Reseller: «مشتری‌های من» → the first customer."""
    user = await app_user(actor)
    if user.is_reseller:
        await actor.tap_reply("👥 مشتری‌های من")
        await actor.tap(_first_service_row(actor))
    else:
        await actor.tap_reply(ACCOUNT)
        await actor.tap(_first_service_row(actor))


def _first_service_row(actor: Actor) -> str:
    return next(b for b in actor.buttons if b[:1] in "🟢🟡🔴⛔⚪")


async def app_user(actor: Actor) -> User:
    return await actor.app.one(select(User).where(User.telegram_id == actor.user_id))


async def to_renew_screen(actor: Actor) -> None:
    await open_service_of(actor)
    await tap_starting(actor, RENEW)


async def to_summary(actor: Actor) -> None:
    await to_renew_screen(actor)
    await actor.tap(SAME)
    if actor.last_text == PROMO_QUESTION:
        await actor.tap("🎫 بدون کد تخفیف")


async def renews(app: App, shop: Actor) -> list[Order]:
    return [o for o in await app.all(select(Order)) if o.renew_mode == RENEW_MODE_RESET]


def kept(user: PanelUser) -> tuple[list[int], int | None]:
    return list(user.group_ids), user.hwid_limit


def screen_has(actor: Actor, gb: int, price: int, *, same: bool = True) -> bool:
    text = digits(actor.last_text)
    return (
        f"{gb}" in text
        and str(price) in text
        and (plain(texts.purchase_renew_custom_same) in actor.last_text) is same
    )


# --------------------------------------------------------------------------- the reseller


@smoke("۸.۳۲", "۷ب.۲۹")
async def test_a_shop_renews_a_custom_service_from_scratch_with_one_tap(
    app: App, store: Store
) -> None:
    shop = await a_shop(app)
    live = await buy_custom(app, shop, 37)
    live.group_ids = [2, 6, 8]  # the owner's extra panel groups on this service
    app.panel.add_group(6, "g6")
    app.panel.add_group(8, "g8")
    live.used_traffic = 30 * GB
    expire_in(live, days=5)
    groups_before, hwid_before = kept(live)
    mark = shop.mark()
    await to_renew_screen(shop)
    assert screen_has(shop, 37, 37 * RATE), shop.last_text
    assert plain(texts.purchase_renew_custom_same) in shop.last_text
    assert SAME in shop.buttons

    await shop.tap(SAME)
    # No group question, no shelf, no name, no promo for a reseller: the summary.
    assert PROMO_QUESTION not in shop.texts_since(mark)
    assert plain(texts.purchase_renew_reset_note) in shop.last_text, shop.last_text
    assert plain(texts.purchase_summary_renew.format(name="")).strip() in shop.last_text
    assert str(37 * RATE) in digits(shop.last_text)

    balance = await app.wallet_balance(shop)
    await shop.tap("✅ ثبت سفارش")
    order = await app.latest_order(shop)
    assert order.status == OrderStatus.ACTIVE
    assert order.price_toman == 37 * RATE
    assert order.renew_mode == RENEW_MODE_RESET and not order.renew_resets
    assert order.renew_account_id is not None and order.store_package_slug is None
    assert order.custom_purchase and order.store_category_slug
    assert (order.delivery_data_limit_gb, order.delivery_duration_days) == (37, 30)
    assert await app.wallet_balance(shop) == balance - 37 * RATE

    panel = app.panel.user(live.username)
    assert 29.9 < panel.days_left() <= 30.01, "today + 30 days, the leftover is gone"
    assert panel.used_traffic == 0 and panel.data_limit == 37 * GB
    assert kept(panel) == (groups_before, hwid_before), "groups and device limit stay"
    assert len(app.panel.users) == 1


@smoke("۸.۳۲")
async def test_the_write_never_carries_group_ids(app: App, store: Store) -> None:
    shop = await a_shop(app)
    live = await buy_custom(app, shop)
    expire_in(live, days=5)  # not an identical service bought a minute ago
    live.used_traffic = 4 * GB
    await to_summary(shop)
    await shop.tap("✅ ثبت سفارش")
    puts = list(app.panel.requests_to("PUT /api/user/{username}"))
    assert puts and all("group_ids" not in (r.body or {}) for r in puts), [r.body for r in puts]
    assert len(app.panel.requests_to("POST /api/user/{username}/reset")) == 1
    assert app.panel.user(live.username).used_traffic == 0


@smoke("۸.۳۳")
async def test_a_typed_volume_and_a_duration_tap_reprice_the_same_screen(
    app: App, store: Store
) -> None:
    shop = await a_shop(app)
    live = await buy_custom(app, shop, 37)
    await to_renew_screen(shop)
    await shop.send("۵۰")  # Persian digits
    assert screen_has(shop, 50, 50 * RATE, same=False), shop.last_text
    await shop.tap("۳ ماهه")
    assert "3 ماه" in shop.last_text and "✅" in " ".join(shop.buttons)
    # a reseller's formula is rate x GB + day rate x days (day rate 0): price unchanged by the tab
    assert str(50 * RATE) in digits(shop.last_text)
    await shop.tap(SAME)
    await shop.tap("✅ ثبت سفارش")
    order = await app.latest_order(shop)
    assert (order.delivery_data_limit_gb, order.delivery_duration_days) == (50, 90)
    assert order.price_toman == 50 * RATE
    panel = app.panel.user(live.username)
    assert panel.data_limit == 50 * GB and 89.9 < panel.days_left() <= 90.01


@smoke("۸.۳۳")
@pytest.mark.parametrize("typed", ["0", "1001", "۱۰۰۱", "abc", "12.5", "-3"])
async def test_a_volume_outside_one_to_a_thousand_is_refused_with_the_range(
    app: App, store: Store, typed: str
) -> None:
    shop = await a_shop(app)
    await buy_custom(app, shop, 37)
    await to_renew_screen(shop)
    await shop.send(typed)
    assert shop.last_text == report_plain(
        texts.purchase_volume_typed_invalid.format(low="1", high="1000")
    )
    assert SAME in shop.buttons, "still on the screen"
    await shop.tap(SAME)
    assert (await app.latest_order(shop)).renew_mode is None, "nothing was ordered by the refusal"


@smoke("۸.۳۴")
async def test_a_retail_customer_gets_the_screen_the_promo_question_and_a_cheaper_price(
    app: App, store: Store
) -> None:
    admin = app.admin()
    await open_store(admin)
    await tap_starting(admin, "🏷")
    await tap_starting(admin, "🏷 مدیریت")
    await tap_starting(admin, "➕")
    await admin.send("SAVE15 15")
    await skip_promo_rules(admin)

    ali = app.customer("ali")
    await ali.start()
    await app.give_wallet(ali, 1_000_000)
    live = await import_service(app, ali, gb=50, days=10, used_gb=20)
    await to_renew_screen(ali)
    retail = 50 * RETAIL_PER_GB
    assert screen_has(ali, 50, retail), ali.last_text
    await ali.tap(SAME)
    assert ali.last_text == PROMO_QUESTION
    await ali.send("save15")
    expected = retail - retail * 15 // 100
    assert str(expected) in digits(ali.last_text), ali.last_text
    await ali.tap("ثبت سفارش")
    order = await app.latest_order(ali)
    assert order.price_toman == expected and order.renew_mode == RENEW_MODE_RESET
    panel = app.panel.user(live.username)
    assert panel.data_limit == 50 * GB and panel.used_traffic == 0
    assert panel.group_ids == [2, 6, 8]


@smoke("۸.۳۵")
async def test_an_imported_service_takes_its_defaults_from_the_panel_and_keeps_its_groups(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await ali.start()
    await app.give_wallet(ali, 1_000_000)
    live = await import_service(app, ali, gb=50, groups=(2, 6, 8), hwid=3)
    await to_summary(ali)
    assert "50 گیگ" in ali.last_text and "1 ماه" in ali.last_text
    await ali.tap("ثبت سفارش")
    order = await app.latest_order(ali)
    assert order.status == OrderStatus.ACTIVE and order.renew_mode == RENEW_MODE_RESET
    panel = app.panel.user(live.username)
    assert 29.9 < panel.days_left() <= 30.01 and panel.data_limit == 50 * GB
    assert panel.used_traffic == 0
    assert panel.group_ids == [2, 6, 8] and panel.hwid_limit == 3
    assert order.delivery_hwid_limit == 3, "a later renew reads the real device limit"


@smoke("۸.۳۵")
async def test_a_resellers_imported_service_is_renewed_the_same_way(app: App, store: Store) -> None:
    shop = await a_shop(app)
    live = await import_service(app, shop, gb=30)
    await to_renew_screen(shop)
    assert screen_has(shop, 30, 30 * RATE), shop.last_text
    await shop.tap(SAME)
    await shop.tap("✅ ثبت سفارش")
    panel = app.panel.user(live.username)
    assert panel.data_limit == 30 * GB and panel.group_ids == [2, 6, 8]
    assert (await app.latest_order(shop)).price_toman == 30 * RATE


@smoke("۸.۳۶")
async def test_without_any_custom_plan_in_the_document_a_retail_import_goes_to_the_switch_flow(
    app: App,
) -> None:
    await publish_store(app)  # no add_custom_volume: the document has no custom plan
    ali = app.customer("ali")
    await ali.start()
    await app.give_wallet(ali, 1_000_000)
    live = await import_service(app, ali, gb=50)
    await to_renew_screen(ali)
    assert ali.last_text == plain(texts.purchase_switch_plan_gone)
    await tap_starting(ali, "حجمی")
    await tap_starting(ali, "اقتصادی")
    await tap_starting(ali, "۵۰ گیگ")
    await tap_starting(ali, "بدون کد")
    await ali.tap("ثبت سفارش")
    order = await app.latest_order(ali)
    assert order.renew_resets and order.renew_mode is None
    panel = app.panel.user(live.username)
    assert panel.data_limit == 50 * GB and panel.used_traffic == 0
    assert panel.group_ids == [2, 6, 8], "a switch on an imported service leaves its panel groups"


@smoke("۸.۳۷")
async def test_a_service_made_by_the_bot_still_changes_groups_on_a_switch(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    account = await buy_active(app, store, ali, "unlimited_1")
    assert account.group_ids == [1]
    await open_service_of(ali)
    await tap_starting(ali, RENEW)
    await ali.tap("بدون کد تخفیف")
    await tap_starting(ali, "🔁 پلن دیگه روی همین سرویس")
    await tap_starting(ali, store.groups["volume"].title)
    await tap_starting(ali, store.groups["special"].title)
    await tap_starting(ali, store.plans["special_50"].title)
    await tap_starting(ali, "بدون کد")
    await tap_starting(ali, "ثبت سفارش")
    order = await app.latest_order(ali)
    await ali.send_photo()
    await approve_order(app, order.id)
    assert app.panel.user(account.username).group_ids == [2]


@smoke("۸.۳۸")
async def test_a_ready_plan_renew_still_adds_volume_and_days(app: App, store: Store) -> None:
    ali = app.customer("ali")
    account = await buy_active(app, store, ali)
    account.used_traffic = 5 * GB
    expire_in(account, days=10)
    before = account.expire
    await open_service_of(ali)
    await tap_starting(ali, RENEW)
    assert ali.last_text == PROMO_QUESTION, "a ready plan matches: no from-scratch screen"
    await ali.tap("🎫 بدون کد تخفیف")
    await ali.tap("ثبت سفارش")
    order = await app.latest_order(ali)
    await ali.send_photo()
    await approve_order(app, order.id)
    live = app.panel.user(account.username)
    assert live.data_limit == 100 * GB
    assert 29.9 < (live.expire - before).total_seconds() / 86400 < 30.1
    assert (await app.latest_order(ali)).renew_mode is None


@smoke("۸.۳۸")
async def test_buying_the_same_custom_shape_again_and_choosing_renew_still_adds(
    app: App, store: Store
) -> None:
    """The explicit marker is the only thing that resets: a slug-less custom order that
    the customer sent to «تمدید همان سرویس» from the buy menu extends, as it always did."""
    ali = app.customer("ali")
    await ali.start()
    await app.give_wallet(ali, 2_000_000)
    for round_ in range(2):
        await open_shelf(ali, "حجمی", "ویژه")
        await tap_starting(ali, "🧩")
        await ali.send("50")
        await tap_starting(ali, "۱ ماه")
        await ali.send("ali12")
        if round_:
            assert ali.last_text == plain(texts.purchase_renew_or_new)
            await tap_starting(ali, "📅 تمدید همان سرویس")
        await ali.tap("بدون کد تخفیف")
        await ali.tap("ثبت سفارش")
    order = await app.latest_order(ali)
    assert order.status == OrderStatus.ACTIVE and order.renew_mode is None and order.custom_purchase
    assert app.panel.only_user().data_limit == 100 * GB, "added to the first 50 GB, not set anew"


# --------------------------------------------------------------------------- retries


async def _renew_to_confirm(app: App) -> tuple[Actor, PanelUser]:
    shop = await a_shop(app)
    live = await buy_custom(app, shop, 37)
    live.used_traffic = 10 * GB
    expire_in(live, days=5)
    await to_summary(shop)
    return shop, live


@smoke("۸.۳۹")
async def test_a_lost_reply_after_the_panel_applied_it_is_not_applied_twice(
    app: App, store: Store
) -> None:
    shop, live = await _renew_to_confirm(app)
    app.panel.timeout_next("PUT /api/user/{username}", applied=True)
    await shop.tap("✅ ثبت سفارش")
    assert (await app.latest_order(shop)).status == OrderStatus.PROVISIONING
    resets_before = len(app.panel.requests_to("POST /api/user/{username}/reset"))
    live.used_traffic = 3 * GB  # the customer already uses it again
    await to_summary(shop)  # the failed attempt retired the summary: back through the button
    await shop.tap("✅ ثبت سفارش")  # the retry
    assert (await app.latest_order(shop)).status == OrderStatus.ACTIVE
    assert len(app.panel.requests_to("POST /api/user/{username}/reset")) == resets_before
    assert app.panel.user(live.username).used_traffic == 3 * GB, "usage was not wiped again"
    assert len(await renews(app, shop)) == 1


@smoke("۸.۳۹")
async def test_a_timeout_after_the_usage_reset_and_before_the_write_converges(
    app: App, store: Store
) -> None:
    shop, live = await _renew_to_confirm(app)
    app.panel.timeout_next("PUT /api/user/{username}")  # the write never arrives
    await shop.tap("✅ ثبت سفارش")
    assert app.panel.user(live.username).used_traffic == 0, "the reset did happen"
    assert app.panel.user(live.username).data_limit == 37 * GB
    await to_summary(shop)
    await shop.tap("✅ ثبت سفارش")
    order = await app.latest_order(shop)
    assert order.status == OrderStatus.ACTIVE
    panel = app.panel.user(live.username)
    assert 29.9 < panel.days_left() <= 30.01 and panel.used_traffic == 0
    assert len(await renews(app, shop)) == 1


@smoke("۸.۴۰")
async def test_a_double_tap_on_confirm_makes_one_order_and_one_application(
    app: App, store: Store
) -> None:
    shop, live = await _renew_to_confirm(app)
    summary = shop.last_message
    balance = await app.wallet_balance(shop)
    await shop.tap("✅ ثبت سفارش")
    await shop.press(PurchaseCB.CONFIRM, message=summary, expect_answer=False)
    assert len(await renews(app, shop)) == 1
    assert await app.wallet_balance(shop) == balance - 37 * RATE
    assert len(app.panel.requests_to("POST /api/user/{username}/reset")) == 1
    assert len(app.panel.requests_to("PUT /api/user/{username}")) == 1


# --------------------------------------------------------------------------- the second renew


@smoke("۸.۴۱")
async def test_the_next_renew_offers_the_same_defaults_even_when_a_ready_plan_has_that_shape(
    app: App, store: Store
) -> None:
    shop = await a_shop(app)
    await buy_custom(app, shop, 37)
    await to_renew_screen(shop)
    await shop.send("50")  # 50 GB / 30 d is also the shape of a ready plan
    await shop.tap(SAME)
    await shop.tap("✅ ثبت سفارش")
    await to_renew_screen(shop)
    assert screen_has(shop, 50, 50 * RATE), shop.last_text
    assert SAME in shop.buttons, "a custom order is never matched to a ready plan by shape"


# --------------------------------------------------------------------------- trial, ownership, toggle


@smoke("۸.۴۲")
async def test_a_trial_has_no_renew_button_and_a_paid_service_has(app: App, store: Store) -> None:
    ali = app.customer("ali")
    await ali.start()
    await ali.tap_reply("🎟 تست رایگان")
    await tap_starting(ali, "ویژه")  # two groups have a trial: pick one
    # the delivered trial card
    card_buttons = ali.buttons
    assert not any("تمدید" in b for b in card_buttons), card_buttons
    assert any("خرید اشتراک" in b for b in card_buttons), card_buttons
    await my_service(ali)
    assert not any("تمدید" in b for b in ali.buttons) and any(
        "خرید اشتراک" in b for b in ali.buttons
    )

    bob = app.customer("bob")
    await buy_active(app, store, bob)
    await my_service(bob)
    assert any(RENEW in b for b in bob.buttons)


@smoke("۸.۴۲")
async def test_a_forged_renew_press_for_someone_elses_service_is_refused(
    app: App, store: Store
) -> None:
    owner = app.customer("owner")
    await owner.start()
    theirs = await import_service(app, owner, username="mirza_theirs", gb=50)
    account = await app.one(
        select(PasarGuardAccount).where(PasarGuardAccount.panel_username == theirs.username)
    )
    thief = await a_shop(app, name="thief")
    mark = thief.mark()
    await thief.press(AccountCB.svc_renew(int(account.id)))
    assert thief.texts_since(mark) == [plain(texts.account_error)]
    assert SAME not in thief.buttons
    assert await renews(app, thief) == []


async def _group_renew(app: App, store: Store, on: bool) -> None:
    async with app.session() as session:
        await update_draft_category(session, store.category_ids["special"], allow_renew=on)
        await commit_and_activate(session, notes="تمدید عوض شد")


@smoke("۸.۴۳")
async def test_the_groups_renew_switch_off_refuses_both_renews_and_on_allows_them(
    app: App, store: Store
) -> None:
    # a ready-plan renew
    ali = app.customer("ali")
    await buy_active(app, store, ali)
    # and a from-scratch one
    shop = await a_shop(app)
    await buy_custom(app, shop, 37)

    await _group_renew(app, store, False)
    for who in (ali, shop):
        await open_service_of(who)
        await tap_starting(who, RENEW)
        assert who.last_text == plain(texts.purchase_renew_plan_off), who.last_text
        assert "🛒 سرویس جدید" in who.buttons

    await _group_renew(app, store, True)
    await open_service_of(ali)
    await tap_starting(ali, RENEW)
    assert ali.last_text == PROMO_QUESTION
    await open_service_of(shop)
    await tap_starting(shop, RENEW)
    assert SAME in shop.buttons


@smoke("۸.۴۳")
async def test_a_stale_confirm_screen_is_refused_after_the_group_turns_renew_off(
    app: App, store: Store
) -> None:
    shop = await a_shop(app)
    await buy_custom(app, shop, 37)
    await to_renew_screen(shop)
    await _group_renew(app, store, False)
    await shop.tap(SAME)
    assert shop.last_text == plain(texts.purchase_renew_plan_off)
    assert await renews(app, shop) == []


@smoke("۸.۴۴")
async def test_no_one_sees_a_custom_setting_in_an_unlimited_group_of_the_store_admin(
    app: App, store: Store
) -> None:
    admin = app.admin()
    await open_store(admin)
    await tap_starting(admin, "نامحدود")
    await tap_starting(admin, "🛒")
    assert not any("خرید سفارشی" in b for b in admin.buttons), admin.buttons
    await admin.send("/admin")
    await open_store(admin)
    await tap_starting(admin, "حجمی")
    await tap_starting(admin, "ویژه")
    await tap_starting(admin, "🛒")
    assert any("خرید سفارشی" in b for b in admin.buttons), admin.buttons

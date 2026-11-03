"""Section 8, the customer's side: renewing, switching plan on the same link, the link, the guide.

Rows ۸.۱ to ۸.۴, ۸.۹ to ۸.۱۴. The owner's side of the service desk is in
``test_smoke_s08_desk.py``; the QR and the payment card in ``test_smoke_s08_qr_card.py``.
"""

from __future__ import annotations

import pytest

from bot.texts import texts
from database.models import Order, OrderStatus
from tests.e2e._s07b_helpers import approve_order, tap_starting
from tests.e2e._s08_helpers import buy_active, expire_in, my_service, retire_plan
from tests.e2e.harness import (
    GB,
    App,
    Store,
    browse_to_plan,
    digits,
    plain,
    publish_store,
    smoke,
)


@pytest.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


async def _tap_renew(ali, *, via: str = "تمدید این سرویس") -> None:
    """The card's renew button. A plan still on sale meets the promo question next."""
    await my_service(ali)
    await tap_starting(ali, via)


async def _renew_to_summary(ali, *, via: str = "تمدید این سرویس") -> None:
    await _tap_renew(ali, via=via)
    await tap_starting(ali, "🎫 بدون کد تخفیف")


async def _pay_and_approve(app: App, ali) -> Order:
    await tap_starting(ali, "ثبت سفارش")
    order = await app.latest_order(ali)
    await ali.send_photo()
    await approve_order(app, order.id)
    return await app.latest_order(ali)


@smoke("۸.۱")
async def test_renew_button_quotes_the_same_plan_after_the_promo_step(
    app: App, store: Store
) -> None:
    """The card's renew button no longer asks «تمدید یا جدید»: after the promo step it quotes the same plan."""
    ali = app.customer("ali")
    await buy_active(app, store, ali)
    await _renew_to_summary(ali)
    plan = store.plans["special_50"]
    assert str(plan.price) in digits(ali.last_text), ali.last_text
    assert "50 گیگ" in ali.last_text
    assert "🔁 پلن دیگه روی همین سرویس" in ali.buttons
    assert len(app.panel.users) == 1, "asking to renew created nothing"


@smoke("۸.۱")
async def test_buying_the_same_plan_again_offers_renewing_the_same_service(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await buy_active(app, store, ali)
    await browse_to_plan(ali, store, "special_50")
    await ali.send("bobo")
    assert ali.last_text == plain(texts.purchase_renew_or_new)
    assert ali.rows[:2] == [["📅 تمدید همان سرویس"], ["✨ سرویس جدید"]], ali.rows


@smoke("۸.۲", "۸.۳")
async def test_renewing_keeps_username_and_link_and_moves_expiry_and_volume(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    account = await buy_active(app, store, ali)
    username, token = account.username, account.token
    account.used_traffic = 5 * GB
    expire_in(account, days=10)
    before = account.expire
    await _renew_to_summary(ali)
    mark = ali.mark()
    order = await _pay_and_approve(app, ali)

    assert order.status == OrderStatus.ACTIVE
    assert list(app.panel.users) == [username], "no second account was made"
    assert app.panel.user(username).token == token, "the link stayed"
    assert len(app.panel.requests_to("POST /api/user")) == 1
    # ۸.۳: the expiry moved 30 days forward from where it was, and the plan's 50 GB came on top.
    gained = (app.panel.user(username).expire - before).total_seconds() / 86400
    assert 29.9 < gained < 30.1, gained
    assert app.panel.user(username).data_limit == 100 * GB
    # The customer got the card with the same link.
    card = ali.messages_since(mark)[-1]
    assert f"{app.panel.base_url}/sub/{token}" in card.text
    assert username in card.text


@smoke("۸.۴")
async def test_choosing_new_service_makes_a_new_username_and_link(app: App, store: Store) -> None:
    ali = app.customer("ali")
    first = await buy_active(app, store, ali)
    await browse_to_plan(ali, store, "special_50")
    await ali.send("bobo")
    await tap_starting(ali, "✨ سرویس جدید")
    await tap_starting(ali, "بدون کد")
    order = await _pay_and_approve(app, ali)
    assert order.status == OrderStatus.ACTIVE
    assert len(app.panel.users) == 2
    second = next(u for u in app.panel.users.values() if u.username != first.username)
    assert second.username.startswith("bobo_")
    assert second.token != first.token
    assert (
        app.panel.user(first.username).expire == first.expire
    ), "the first service was not touched"


@smoke("۸.۹")
async def test_the_customer_revokes_the_link_after_a_confirm_and_the_old_one_dies(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    account = await buy_active(app, store, ali)
    old = account.token
    await my_service(ali)
    await tap_starting(ali, "تغییر لینک")
    assert ali.last_text == plain(texts.account_revoke_warn), ali.last_text
    assert app.panel.user(account.username).token == old, "nothing changes before the confirm"
    await tap_starting(ali, "بله، لینک جدید")
    new = app.panel.user(account.username).token
    assert new != old
    assert all(u.token != old for u in app.panel.users.values()), "the old link matches nobody"
    assert f"{app.panel.base_url}/sub/{new}" in ali.last_text
    assert f"/sub/{old}" not in ali.last_text
    assert ali.popup == texts.account_revoke_success


@smoke("۸.۹")
async def test_declining_the_confirm_leaves_the_link_alone(app: App, store: Store) -> None:
    ali = app.customer("ali")
    account = await buy_active(app, store, ali)
    old = account.token
    await my_service(ali)
    await tap_starting(ali, "تغییر لینک")
    await tap_starting(ali, "انصراف")
    assert app.panel.user(account.username).token == old
    assert len(app.panel.requests_to("POST /api/user/{username}/revoke_sub")) == 0


@smoke("۸.۱۰")
async def test_every_connection_guide_opens_and_can_go_back(app: App, store: Store) -> None:
    ali = app.customer("ali")
    await buy_active(app, store, ali)
    await my_service(ali)
    await tap_starting(ali, "راهنمای اتصال")
    systems = ["Android", "iOS", "Windows", "macOS"]
    pages = set()
    for system in systems:
        assert any(system in b for b in ali.buttons), ali.buttons
        await tap_starting(ali, system)
        pages.add(ali.last_text)
        assert any("بازگشت" in b for b in ali.buttons), f"{system}: no way back: {ali.buttons}"
    assert len(pages) == 4, "each system has its own guide"
    await tap_starting(ali, "بازگشت")
    assert "لینک سابسکریپشن" in ali.last_text, "back lands on the service"


# --------------------------------------------- a plan that is no longer sold: switch in place


async def _stale_service(app: App, store: Store, ali, *, retire: str = "special_50"):
    """A service whose plan was taken off sale (the shape of every imported account)."""
    account = await buy_active(app, store, ali, retire)
    await retire_plan(app, store, retire)
    return account


@smoke("۸.۱۱")
async def test_renewing_a_retired_plan_offers_the_store_on_the_same_link(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    account = await _stale_service(app, store, ali)
    await _tap_renew(ali)
    assert ali.last_text == plain(texts.purchase_switch_plan_gone)
    assert store.groups["volume"].title in ali.buttons, ali.buttons
    # Pick a plan: the name is not asked again, the summary comes after the promo question.
    await tap_starting(ali, store.groups["volume"].title)
    await tap_starting(ali, store.groups["economy"].title)
    await tap_starting(ali, store.plans["economy_50"].title)
    assert ali.last_text != plain(texts.purchase_enter_service_name)
    assert "بدون کد تخفیف" in " ".join(ali.buttons)
    assert len(app.panel.users) == 1 and account.username in app.panel.users


@smoke("۸.۱۲")
async def test_the_switch_summary_says_the_leftover_goes_and_the_new_plan_starts_today(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await _stale_service(app, store, ali)
    await _tap_renew(ali)
    await tap_starting(ali, store.groups["volume"].title)
    await tap_starting(ali, store.groups["economy"].title)
    await tap_starting(ali, store.plans["economy_50"].title)
    await tap_starting(ali, "بدون کد")
    assert plain(texts.purchase_switch_note) in ali.last_text, ali.last_text
    assert str(store.plans["economy_50"].price) in digits(ali.last_text)


@smoke("۸.۱۳")
async def test_a_paid_switch_rebuilds_the_account_on_the_new_plan(app: App, store: Store) -> None:
    ali = app.customer("ali")
    account = await _stale_service(app, store, ali)
    username, token = account.username, account.token
    account.used_traffic = 20 * GB
    expire_in(account, days=10)
    await _tap_renew(ali)
    await tap_starting(ali, store.groups["volume"].title)
    await tap_starting(ali, store.groups["economy"].title)
    await tap_starting(ali, store.plans["economy_50"].title)
    await tap_starting(ali, "بدون کد")
    order = await _pay_and_approve(app, ali)

    assert order.status == OrderStatus.ACTIVE
    live = app.panel.user(username)
    assert list(app.panel.users) == [username] and live.token == token
    plan = store.plans["economy_50"]
    assert 29.5 < live.days_left() <= plan.duration_days, "today + the new plan, not the leftover"
    assert live.data_limit == plan.volume_gb * GB and live.used_traffic == 0
    assert live.group_ids == store.groups["economy"].panel_groups == [3]
    assert live.hwid_limit == 0


@smoke("۸.۱۴")
async def test_the_other_plan_button_on_a_normal_renew_switches_direct_to_tunnel(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    account = await buy_active(app, store, ali, "unlimited_1")
    username, token = account.username, account.token
    assert account.group_ids == [1] and account.unlimited
    await _renew_to_summary(ali)
    await tap_starting(ali, "🔁 پلن دیگه روی همین سرویس")
    assert ali.last_text == plain(texts.purchase_switch_pick)
    await tap_starting(ali, store.groups["volume"].title)
    await tap_starting(ali, store.groups["special"].title)
    await tap_starting(ali, store.plans["special_50"].title)
    await tap_starting(ali, "بدون کد")
    assert plain(texts.purchase_switch_note) in ali.last_text
    order = await _pay_and_approve(app, ali)
    assert order.status == OrderStatus.ACTIVE
    live = app.panel.user(username)
    assert list(app.panel.users) == [username] and live.token == token
    assert live.group_ids == [2] and live.data_limit == 50 * GB
    assert live.hwid_limit == 0, "the tunnel plan is unlimited devices, not the old plan's one"

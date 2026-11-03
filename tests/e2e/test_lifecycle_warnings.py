"""Section ۱۶, rows ۱۶.۵ to ۱۶.۹: the day warning and the volume warning."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from bot.texts import texts
from database.models import Order, OrderStatus
from tests.e2e._s08_helpers import buy_active
from tests.e2e._s16_helpers import seed_service, set_number
from tests.e2e.harness import GB, App, Store, plain, publish_store, smoke
from tests.e2e.harness.expectations import report_plain


@pytest.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


async def _expire_order_in(app: App, hours: float) -> None:
    async with app.session() as session:
        await session.execute(
            update(Order)
            .where(Order.status == OrderStatus.ACTIVE)
            .values(expires_at=datetime.now(UTC) + timedelta(hours=hours))
        )
        await session.commit()


def _renew_texts(messages: list[str]) -> list[str]:
    return [m for m in messages if "منقضی می‌شه" in m]


# --- days left ---------------------------------------------------------------


@smoke("۱۶.۵")
async def test_the_day_reminder_follows_the_days_the_owner_typed(app: App, store: Store) -> None:
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    await buy_active(app, store, ali)
    await _expire_order_in(app, 5 * 24 - 12)  # 4.5 days left

    mark = ali.mark()
    await app.run_job("reminders")  # the env default is «۳ ۱»: nothing is due yet
    assert _renew_texts(ali.texts_since(mark)) == []

    await set_number(app.admin(0), "warn_days", "۵ ۲")
    mark = ali.mark()
    await app.run_job("reminders")
    sent = _renew_texts(ali.texts_since(mark))
    assert len(sent) == 1 and "5 روز مونده" in sent[0], sent

    mark = ali.mark()
    await app.run_job("reminders")  # same window: not again
    assert _renew_texts(ali.texts_since(mark)) == []


@smoke("۱۶.۵")
async def test_an_empty_day_list_turns_the_day_reminder_off(app: App, store: Store) -> None:
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    await buy_active(app, store, ali)
    await _expire_order_in(app, 2 * 24 - 12)  # 1.5 days left: due under the default
    await set_number(app.admin(0), "warn_days", "0")

    mark = ali.mark()
    await app.run_job("reminders")
    assert _renew_texts(ali.texts_since(mark)) == []


# --- volume left -------------------------------------------------------------


def _volume_texts(messages: list[str]) -> list[str]:
    return [m for m in messages if "حجم" in m and "داره تموم می‌شه" in m]


async def _run_volume(app: App, who: object) -> list[str]:
    mark = who.mark()  # type: ignore[attr-defined]
    await app.run_job("volume")
    return _volume_texts(who.texts_since(mark))  # type: ignore[attr-defined]


@smoke("۱۶.۶")
async def test_crossing_twenty_then_five_sends_one_message_each_and_never_again(
    app: App,
) -> None:
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    await seed_service(app, ali.user_id, "svc_a", expires_in_days=20, gb=10, used_gb=7)
    panel_user = app.panel.user("svc_a")

    assert await _run_volume(app, ali) == []  # 30% left

    panel_user.used_traffic = int(8.5 * GB)  # 15% left
    first = await _run_volume(app, ali)
    assert len(first) == 1 and "15٪" in first[0], first
    assert await _run_volume(app, ali) == [], "the next run does not repeat it"

    panel_user.used_traffic = int(9.7 * GB)  # 3% left
    second = await _run_volume(app, ali)
    assert len(second) == 1 and "3٪" in second[0], second
    assert await _run_volume(app, ali) == []
    assert await _run_volume(app, ali) == []


@smoke("۱۶.۷")
async def test_jumping_straight_under_five_sends_one_message_not_two(app: App) -> None:
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    await seed_service(app, ali.user_id, "svc_b", expires_in_days=20, gb=10, used_gb=9.8)
    assert len(await _run_volume(app, ali)) == 1
    assert await _run_volume(app, ali) == []


@smoke("۱۶.۸")
async def test_added_volume_or_a_renewal_re_arms_both_warnings(app: App) -> None:
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    await seed_service(app, ali.user_id, "svc_c", expires_in_days=20, gb=10, used_gb=9.7)
    panel_user = app.panel.user("svc_c")
    assert len(await _run_volume(app, ali)) == 1

    # The owner adds 10 GB: a new period, and 14.7 of 20 GB used is 26% left.
    panel_user.data_limit = 20 * GB
    panel_user.used_traffic = int(14.7 * GB)
    assert await _run_volume(app, ali) == []
    panel_user.used_traffic = int(17 * GB)  # 15% left in the new period
    assert len(await _run_volume(app, ali)) == 1, "the 20% warning is armed again"
    panel_user.used_traffic = int(19.5 * GB)
    assert len(await _run_volume(app, ali)) == 1, "and so is the 5% one"

    # A renewal: later expiry, usage reset, same volume. A new period again.
    panel_user.expire = datetime.now(UTC) + timedelta(days=50)
    panel_user.used_traffic = 0
    assert await _run_volume(app, ali) == []
    panel_user.used_traffic = int(19 * GB)  # 5% left
    assert len(await _run_volume(app, ali)) == 1


@smoke("۱۶.۸")
async def test_a_monthly_usage_reset_re_arms_the_warning_too(app: App) -> None:
    """The limit and the expiry do not change on a reset, so «healthy again» re-arms."""
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    await seed_service(app, ali.user_id, "svc_d", expires_in_days=90, gb=10, used_gb=9)
    panel_user = app.panel.user("svc_d")
    assert len(await _run_volume(app, ali)) == 1
    panel_user.used_traffic = 0
    assert await _run_volume(app, ali) == []
    panel_user.used_traffic = int(9 * GB)
    assert len(await _run_volume(app, ali)) == 1


@smoke("۱۶.۹")
async def test_an_unlimited_service_gets_no_volume_warning(app: App) -> None:
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    await seed_service(app, ali.user_id, "svc_u", expires_in_days=20, gb=None, used_gb=500)
    assert await _run_volume(app, ali) == []
    assert app.panel.requests_to("GET /api/user/{username}"), "it did read the panel"


@smoke("۱۶.۹")
async def test_a_panel_error_for_one_service_skips_it_and_the_rest_are_still_warned(
    app: App,
) -> None:
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    await seed_service(app, ali.user_id, "svc_x", expires_in_days=20, gb=10, used_gb=9)
    await seed_service(app, ali.user_id, "svc_y", expires_in_days=20, gb=10, used_gb=9)
    app.panel.fail_next("GET /api/user/{username}", status=500)
    got = await _run_volume(app, ali)
    assert len(got) == 1, got
    # The skipped one is picked up by the next run.
    assert len(await _run_volume(app, ali)) == 1
    assert await _run_volume(app, ali) == []


@smoke("۱۶.۹")
async def test_the_warning_goes_to_the_reseller_who_owns_the_service_with_a_renew_button(
    app: App,
) -> None:
    shop = app.customer("shop", first_name="Shop")
    await shop.start()
    await seed_service(
        app,
        shop.user_id,
        "cust_1",
        expires_in_days=20,
        gb=10,
        used_gb=9,
        reseller=True,
        label="رضا | 09120000000",
    )
    mark = shop.mark()
    await app.run_job("volume")
    sent = [m for m in shop.messages_since(mark) if "داره تموم می‌شه" in m.text]
    assert len(sent) == 1
    assert "رضا" in sent[0].text
    assert any("تمدید" in b for b in sent[0].buttons), sent[0].buttons


@smoke("۱۶.۹")
async def test_a_changed_percent_list_is_what_the_next_run_uses(app: App) -> None:
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    await seed_service(app, ali.user_id, "svc_p", expires_in_days=20, gb=10, used_gb=7)  # 30%
    assert await _run_volume(app, ali) == []
    await set_number(app.admin(0), "warn_volume_percents", "۴۰")
    got = await _run_volume(app, ali)
    assert len(got) == 1 and "30٪" in got[0]
    await set_number(app.admin(0), "warn_volume_percents", "0")
    app.panel.user("svc_p").used_traffic = int(9.9 * GB)
    assert await _run_volume(app, ali) == [], "an empty list is off"


@smoke("۱۶.۹")
async def test_the_warning_text_is_the_notify_string_with_english_digits(app: App) -> None:
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    await seed_service(app, ali.user_id, "svc_t", expires_in_days=20, gb=10, used_gb=9)
    got = await _run_volume(app, ali)
    assert len(got) == 1
    assert report_plain(texts.notify_volume_warning.format(name="svc_t", percent="10")) == got[0]
    assert not re.search(r"[۰-۹٠-٩]", got[0])


@smoke("۱۶.۹")
async def test_a_free_trial_gets_no_volume_warning_but_a_paid_service_at_the_same_level_does(
    app: App,
) -> None:
    """A trial cannot be renewed: «۲۰٪ مانده» with a renew button would lead nowhere."""
    from database.models.order import TRIAL_KIND_PERSONAL, TRIAL_KIND_RESELLER
    from tests.e2e._s16_helpers import seed_order

    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    await seed_service(app, ali.user_id, "trial_a", expires_in_days=1, gb=1, used_gb=0.9)
    await seed_order(
        app, ali.user_id, "trial_a", status=OrderStatus.ACTIVE, trial_kind=TRIAL_KIND_PERSONAL
    )
    await seed_service(app, ali.user_id, "trial_r", expires_in_days=1, gb=1, used_gb=0.9)
    await seed_order(
        app, ali.user_id, "trial_r", status=OrderStatus.ACTIVE, trial_kind=TRIAL_KIND_RESELLER
    )
    await seed_service(app, ali.user_id, "paid_a", expires_in_days=20, gb=10, used_gb=9)
    await seed_order(app, ali.user_id, "paid_a", status=OrderStatus.ACTIVE)
    # A trial that was later bought into (a paid order references it) is a paid service.
    await seed_service(app, ali.user_id, "grown_a", expires_in_days=20, gb=10, used_gb=9)
    await seed_order(
        app, ali.user_id, "grown_a", status=OrderStatus.ACTIVE, trial_kind=TRIAL_KIND_PERSONAL
    )
    await seed_order(app, ali.user_id, "grown_a", status=OrderStatus.ACTIVE)

    got = await _run_volume(app, ali)
    assert len(got) == 2, got
    assert any("paid_a" in m for m in got) and any("grown_a" in m for m in got)
    assert not any("trial_" in m for m in got)
    # The trials were not even read from the panel.
    read = [r.path for r in app.panel.requests_to("GET /api/user/{username}")]
    assert not any("trial_" in p for p in read), read


# --- a free trial: its own message, never the renewal reminder -------------------------------------


def _trial_ending(messages: list[str]) -> list[str]:
    return [m for m in messages if "تست رایگانت" in m]


def _reminders(messages: list[str]) -> list[str]:
    return [m for m in messages if "سرویست به‌زودی منقضی می‌شه" in m]


@smoke("۱۶.۲۸")
async def test_a_trial_ending_in_a_day_gets_its_own_message_once_and_no_renewal_reminder(
    app: App,
) -> None:
    from database.models.order import TRIAL_KIND_PERSONAL
    from tests.e2e._s16_helpers import seed_order

    ali = await _customer_with_service_card(app)
    await seed_service(app, ali.user_id, "trial_x", expires_in_days=1, gb=1)
    await seed_order(
        app,
        ali.user_id,
        "trial_x",
        status=OrderStatus.ACTIVE,
        trial_kind=TRIAL_KIND_PERSONAL,
        price_toman=0,
        expires_in_hours=20,
    )
    mark = ali.mark()
    await app.run_job("reminders")
    got = ali.messages_since(mark)
    ending = _trial_ending([m.text for m in got])
    assert len(ending) == 1, [m.text for m in got]
    assert _reminders([m.text for m in got]) == []
    message = next(m for m in got if "تست رایگانت" in m.text)
    assert any("خرید اشتراک" in b for b in message.buttons)
    assert not any("تمدید" in b for b in message.buttons), message.buttons
    assert plain(texts.notify_trial_ending) == message.text

    mark = ali.mark()
    await app.run_job("reminders")  # a second run sends nothing
    assert ali.texts_since(mark) == []


@smoke("۱۶.۲۸")
async def test_a_paid_service_still_gets_the_renewal_reminder_and_a_resellers_trial_nothing(
    app: App,
) -> None:
    from database.models.order import TRIAL_KIND_RESELLER
    from tests.e2e._s16_helpers import seed_order

    shop = app.customer("shop", first_name="Shop")
    await shop.start()
    await seed_service(app, shop.user_id, "paid_x", expires_in_days=1, gb=10, reseller=True)
    await seed_order(app, shop.user_id, "paid_x", status=OrderStatus.ACTIVE, expires_in_hours=20)
    await seed_service(app, shop.user_id, "rtrial_x", expires_in_days=1, gb=1)
    await seed_order(
        app,
        shop.user_id,
        "rtrial_x",
        status=OrderStatus.ACTIVE,
        trial_kind=TRIAL_KIND_RESELLER,
        price_toman=0,
        expires_in_hours=20,
    )
    mark = shop.mark()
    await app.run_job("reminders")
    got = shop.texts_since(mark)
    assert len(_reminders(got)) == 1 and "paid_x" in _reminders(got)[0], got
    assert _trial_ending(got) == []
    assert not any("rtrial_x" in t for t in got)


@smoke("۱۶.۲۸")
async def test_a_trial_that_is_still_far_from_its_end_gets_nothing_yet(app: App) -> None:
    from database.models.order import TRIAL_KIND_PERSONAL
    from tests.e2e._s16_helpers import seed_order

    ali = await _customer_with_service_card(app)
    await seed_service(app, ali.user_id, "trial_far", expires_in_days=3, gb=1)
    await seed_order(
        app,
        ali.user_id,
        "trial_far",
        status=OrderStatus.ACTIVE,
        trial_kind=TRIAL_KIND_PERSONAL,
        price_toman=0,
        expires_in_hours=30,
    )
    mark = ali.mark()
    await app.run_job("reminders")
    assert ali.texts_since(mark) == []


async def _customer_with_service_card(app: App):  # noqa: ANN202
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    return ali

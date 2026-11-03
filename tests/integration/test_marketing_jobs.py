"""The marketing jobs (``services/marketing_run.py``): who gets a nudge, and when.

Every case is paired with the plain one that is sent, so a test goes red when the
rule it names is broken and not when its setup is.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.texts import texts
from database.models import OrderStatus, User
from database.models.lifecycle import ServiceNotice
from database.models.marketing import (
    NOTICE_MKT_TRIAL_CONVERT,
    NOTICE_MKT_WINBACK,
    MarketingSettings,
)
from database.models.order import TRIAL_KIND_PERSONAL, TRIAL_KIND_RESELLER
from services.marketing_run import MAX_SENDS_PER_RUN, run_cart_nudges, run_hourly_nudges
from tests.integration.marketing_helpers import (
    NOW,
    hours_ago,
    make_account,
    make_notice,
    make_order,
    make_plan,
    make_user,
)

Factory = async_sessionmaker[AsyncSession]

ON_SINCE = NOW - timedelta(days=60)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, Any]] = []

    async def send_message(self, *, chat_id: int, text: str, reply_markup: Any = None) -> None:
        self.sent.append((chat_id, text, reply_markup))


class FakePanel:
    """``get_user`` answers a status per username; a missing one raises (a failed read)."""

    def __init__(self, statuses: dict[str, str] | None = None) -> None:
        self.statuses = statuses or {}
        self.asked: list[str] = []

    async def get_user(self, username: str) -> Any:
        self.asked.append(username)
        if username not in self.statuses:
            raise RuntimeError("panel down")
        return SimpleNamespace(status=SimpleNamespace(value=self.statuses[username]))

    async def aclose(self) -> None:
        return None


def _settings(expiry_minutes: int = 30) -> Settings:
    return Settings.model_validate(
        {
            "BOT_TOKEN": "test-token",
            "SCHEDULER_ENABLED": False,
            "ORDER_EXPIRY_MINUTES": expiry_minutes,
            "PASARGUARD_API_KEY": "pg_key_test",
        }
    )


async def _switch_on(factory: Factory, **moments: datetime | None) -> None:
    async with factory() as session:
        row = await session.get(MarketingSettings, 1)
        if row is None:
            row = MarketingSettings(id=1)
            session.add(row)
        for name, moment in moments.items():
            setattr(row, f"{name}_enabled_at", moment)
        await session.commit()


async def _cart(factory: Factory, bot: Any, *, expiry: int = 30) -> int:
    return await run_cart_nudges(
        session_factory=factory, bot=bot, settings=_settings(expiry), now=NOW
    )


async def _hourly(factory: Factory, bot: Any, panel: Any = None) -> int:
    return await run_hourly_nudges(
        session_factory=factory,
        bot=bot,
        settings=_settings(),
        pause_seconds=0,
        client=panel or FakePanel(),
        now=NOW,
    )


async def _keys(factory: Factory) -> list[str]:
    async with factory() as session:
        return sorted((await session.scalars(select(ServiceNotice.event_key))).all())


# --- the cart reminder -------------------------------------------------------


async def _open_order(
    session: AsyncSession,
    telegram_id: int,
    *,
    minutes_old: float,
    reseller: bool = False,
    banned: bool = False,
    label: str | None = None,
    status: OrderStatus = OrderStatus.PENDING_PAYMENT,
) -> tuple[User, int]:
    user = await make_user(session, telegram_id, reseller=reseller, banned=banned)
    plan = await make_plan(session)
    order = await make_order(
        session,
        user,
        plan,
        status=status,
        created_at=NOW - timedelta(minutes=minutes_old),
        customer_label=label,
    )
    return user, int(order.id)


@pytest.mark.asyncio
async def test_an_open_order_in_its_last_minutes_gets_one_reminder_ever(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user, order_id = await _open_order(session, 1, minutes_old=20)
        await session.commit()
    await _switch_on(factory, cart=ON_SINCE)
    bot = FakeBot()

    assert await _cart(factory, bot) == 1
    chat_id, text, markup = bot.sent[0]
    assert chat_id == user.telegram_id and text == texts.notify_cart_reminder
    assert markup.inline_keyboard[0][0].callback_data == "menu:buy"
    assert markup.inline_keyboard[0][0].text == "ادامهٔ سفارش"
    assert await _keys(factory) == [f"mkt:cart:{order_id}"]

    assert await _cart(factory, bot) == 0, "one reminder per order, ever"
    assert len(bot.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "kwargs"),
    [
        ("too_early_for_the_window", {"minutes_old": 10}),
        ("already_expired", {"minutes_old": 31}),
        ("receipt_sent", {"minutes_old": 20, "status": OrderStatus.PAYMENT_REVIEW}),
        ("reseller", {"minutes_old": 20, "reseller": True}),
        ("banned", {"minutes_old": 20, "banned": True}),
        ("a_walk_in_customers_order", {"minutes_old": 20, "label": "رضا"}),
    ],
)
async def test_an_order_that_does_not_qualify_gets_nothing(
    two_sessions: tuple[Factory, str], case: str, kwargs: dict[str, Any]
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        await _open_order(session, 1, **kwargs)
        await session.commit()
    await _switch_on(factory, cart=ON_SINCE)
    bot = FakeBot()
    assert await _cart(factory, bot) == 0, case
    assert bot.sent == [] and await _keys(factory) == []


@pytest.mark.asyncio
async def test_the_window_edges_are_the_last_fifteen_minutes(
    two_sessions: tuple[Factory, str],
) -> None:
    """Expiry 30: 15 minutes old is in the window, 29.9 too, 30 is gone."""
    factory, _ = two_sessions
    async with factory() as session:
        await _open_order(session, 1, minutes_old=15)
        await _open_order(session, 2, minutes_old=29.9)
        await _open_order(session, 3, minutes_old=30)
        await _open_order(session, 4, minutes_old=14.9)
        await session.commit()
    await _switch_on(factory, cart=ON_SINCE)
    bot = FakeBot()
    assert await _cart(factory, bot) == 2


@pytest.mark.asyncio
async def test_an_order_made_before_switch_on_is_never_reminded(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        await _open_order(session, 1, minutes_old=20)
        await session.commit()
    await _switch_on(factory, cart=NOW - timedelta(minutes=19))
    bot = FakeBot()
    assert await _cart(factory, bot) == 0


@pytest.mark.asyncio
async def test_the_cart_reminder_is_off_until_switched_on(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        await _open_order(session, 1, minutes_old=20)
        await session.commit()
    bot = FakeBot()
    assert await _cart(factory, bot) == 0
    await _switch_on(factory, cart=None, winback=ON_SINCE)
    assert await _cart(factory, bot) == 0


@pytest.mark.asyncio
async def test_an_expiry_of_fifteen_minutes_or_less_leaves_no_window(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        await _open_order(session, 1, minutes_old=5)
        await session.commit()
    await _switch_on(factory, cart=ON_SINCE)
    bot = FakeBot()
    assert await _cart(factory, bot, expiry=15) == 0
    assert await _cart(factory, bot, expiry=10) == 0
    assert bot.sent == []


@pytest.mark.asyncio
async def test_the_cart_reminder_ignores_the_cap_and_does_not_count_toward_it(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user, _order = await _open_order(session, 1, minutes_old=20)
        await make_notice(
            session,
            user,
            kind=NOTICE_MKT_WINBACK,
            event_key="mkt:winback:old",
            delivered_at=hours_ago(1),
        )
        await session.commit()
    await _switch_on(factory, cart=ON_SINCE)
    bot = FakeBot()
    assert await _cart(factory, bot) == 1, "a capped user still gets the cart reminder"


@pytest.mark.asyncio
async def test_a_cart_run_sends_at_most_thirty_and_the_rest_wait(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        for index in range(MAX_SENDS_PER_RUN + 5):
            await _open_order(session, 1000 + index, minutes_old=20)
        await session.commit()
    await _switch_on(factory, cart=ON_SINCE)
    bot = FakeBot()
    assert await _cart(factory, bot) == MAX_SENDS_PER_RUN
    assert await _cart(factory, bot) == 5
    assert await _cart(factory, bot) == 0


# --- trial ended, never bought -----------------------------------------------


async def _ended_trial(
    session: AsyncSession,
    telegram_id: int,
    *,
    ended_hours_ago: float,
    kind: str = TRIAL_KIND_PERSONAL,
    reseller: bool = False,
    banned: bool = False,
    paid: bool = False,
    label: str | None = None,
    deleted_account: bool = False,
    user: User | None = None,
    slug: str | None = None,
    voided: bool = False,
) -> User:
    user = user or await make_user(session, telegram_id, reseller=reseller, banned=banned)
    plan = await make_plan(session)
    name = f"trial_{telegram_id}_{slug or 'a'}"
    if deleted_account:
        await make_account(session, user, name, deleted_at=NOW - timedelta(hours=1))
    else:
        await make_account(session, user, name)
    order = await make_order(
        session,
        user,
        plan,
        price=0,
        trial_kind=kind,
        expires_at=hours_ago(ended_hours_ago),
        activated_at=hours_ago(ended_hours_ago + 24),
        panel_username=name,
        customer_label=label,
    )
    order.trial_category_slug = slug
    if voided:
        order.trial_voided_at = NOW - timedelta(hours=2)
    if paid:
        await make_order(
            session,
            user,
            plan,
            expires_at=NOW + timedelta(days=10),
            activated_at=hours_ago(5),
        )
    return user


@pytest.mark.asyncio
async def test_a_trial_that_ended_a_day_ago_gets_one_message_ever(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user = await _ended_trial(session, 1, ended_hours_ago=30)
        await session.commit()
    await _switch_on(factory, trial_convert=ON_SINCE)
    bot = FakeBot()

    assert await _hourly(factory, bot) == 1
    chat_id, text, markup = bot.sent[0]
    assert chat_id == user.telegram_id and text == texts.notify_trial_convert
    assert markup.inline_keyboard[0][0].text == "خرید اشتراک"
    assert await _keys(factory) == [f"mkt:trial_convert:{user.id}"]
    assert await _hourly(factory, bot) == 0
    assert len(bot.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "kwargs"),
    [
        ("ended_twelve_hours_ago", {"ended_hours_ago": 12}),
        ("ended_eight_days_ago", {"ended_hours_ago": 8 * 24}),
        ("a_resellers_trial_kind", {"ended_hours_ago": 30, "kind": TRIAL_KIND_RESELLER}),
        ("a_reseller", {"ended_hours_ago": 30, "reseller": True}),
        ("banned", {"ended_hours_ago": 30, "banned": True}),
        ("already_bought", {"ended_hours_ago": 30, "paid": True}),
        ("labelled", {"ended_hours_ago": 30, "label": "رضا"}),
        ("voided_by_an_admin", {"ended_hours_ago": 30, "voided": True}),
    ],
)
async def test_a_trial_that_does_not_qualify_gets_nothing(
    two_sessions: tuple[Factory, str], case: str, kwargs: dict[str, Any]
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        await _ended_trial(session, 1, **kwargs)
        await session.commit()
    await _switch_on(factory, trial_convert=ON_SINCE)
    bot = FakeBot()
    assert await _hourly(factory, bot) == 0, case
    assert bot.sent == []


@pytest.mark.asyncio
async def test_a_trial_that_ended_before_switch_on_is_never_nudged(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        await _ended_trial(session, 1, ended_hours_ago=30)
        await session.commit()
    await _switch_on(factory, trial_convert=NOW - timedelta(hours=29))
    bot = FakeBot()
    assert await _hourly(factory, bot) == 0


@pytest.mark.asyncio
async def test_a_trial_whose_service_lifecycle_already_deleted_is_still_nudged(
    two_sessions: tuple[Factory, str],
) -> None:
    """Candidates come from orders, not from live accounts."""
    factory, _ = two_sessions
    async with factory() as session:
        await _ended_trial(session, 1, ended_hours_ago=30, deleted_account=True)
        await session.commit()
    await _switch_on(factory, trial_convert=ON_SINCE)
    bot = FakeBot()
    assert await _hourly(factory, bot) == 1


@pytest.mark.asyncio
async def test_two_ended_trials_of_one_user_make_one_message(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user = await _ended_trial(session, 1, ended_hours_ago=30, slug="direct")
        await _ended_trial(session, 1, ended_hours_ago=40, slug="tunnel", user=user)
        await session.commit()
    await _switch_on(factory, trial_convert=ON_SINCE)
    bot = FakeBot()
    assert await _hourly(factory, bot) == 1
    assert await _hourly(factory, bot) == 0


@pytest.mark.asyncio
async def test_a_capped_user_waits_and_a_second_user_does_not(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        capped = await _ended_trial(session, 1, ended_hours_ago=30)
        await _ended_trial(session, 2, ended_hours_ago=30)
        await make_notice(
            session,
            capped,
            kind=NOTICE_MKT_WINBACK,
            event_key="mkt:winback:earlier",
            delivered_at=hours_ago(2),
        )
        await session.commit()
    await _switch_on(factory, trial_convert=ON_SINCE)
    bot = FakeBot()
    assert await _hourly(factory, bot) == 1
    assert bot.sent[0][0] == 2


@pytest.mark.asyncio
async def test_an_hourly_run_sends_at_most_thirty(two_sessions: tuple[Factory, str]) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        for index in range(MAX_SENDS_PER_RUN + 4):
            await _ended_trial(session, 2000 + index, ended_hours_ago=30)
        await session.commit()
    await _switch_on(factory, trial_convert=ON_SINCE)
    bot = FakeBot()
    assert await _hourly(factory, bot) == MAX_SENDS_PER_RUN
    assert await _hourly(factory, bot) == 4


# --- paid service ended, not renewed -----------------------------------------


async def _paid_service(
    session: AsyncSession,
    telegram_id: int,
    name: str,
    *,
    ended_days_ago: float | None,
    user: User | None = None,
    reseller: bool = False,
    banned: bool = False,
    label: str | None = None,
    deleted: bool = False,
) -> User:
    """A live paid service; ``ended_days_ago=None`` means it has no end (running)."""
    user = user or await make_user(session, telegram_id, reseller=reseller, banned=banned)
    plan = await make_plan(session)
    await make_account(session, user, name, deleted_at=NOW if deleted else None)
    ends = None if ended_days_ago is None else NOW - timedelta(days=ended_days_ago)
    await make_order(
        session,
        user,
        plan,
        expires_at=ends,
        activated_at=NOW - timedelta(days=60),
        panel_username=name,
        customer_label=label,
    )
    return user


@pytest.mark.asyncio
async def test_a_paid_service_that_ended_ten_days_ago_gets_one_message(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user = await _paid_service(session, 1, "svc_a", ended_days_ago=10)
        await session.commit()
    await _switch_on(factory, winback=ON_SINCE)
    bot = FakeBot()
    panel = FakePanel({"svc_a": "expired"})

    assert await _hourly(factory, bot, panel) == 1
    chat_id, text, markup = bot.sent[0]
    assert chat_id == user.telegram_id
    assert text == texts.notify_winback.format(name="svc_a")
    assert markup.inline_keyboard[0][0].text == "خرید یا تمدید"
    expected_date = (NOW - timedelta(days=10)).date().isoformat()
    assert await _keys(factory) == [f"mkt:winback:{user.id}:{expected_date}"]
    assert panel.asked == ["svc_a"], "the panel was read once for the candidate"
    assert await _hourly(factory, bot, panel) == 0
    assert len(bot.sent) == 1


@pytest.mark.asyncio
async def test_the_panel_has_the_last_word_and_a_failed_read_never_sends(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        await _paid_service(session, 1, "svc_a", ended_days_ago=10)
        await session.commit()
    await _switch_on(factory, winback=ON_SINCE)
    bot = FakeBot()

    assert await _hourly(factory, bot, FakePanel({"svc_a": "active"})) == 0
    assert await _hourly(factory, bot, FakePanel({})) == 0, "a failed read is a skip"
    assert bot.sent == [] and await _keys(factory) == []
    # The next run, with the panel agreeing, sends it.
    assert await _hourly(factory, bot, FakePanel({"svc_a": "expired"})) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "kwargs"),
    [
        ("ended_four_days_ago", {"ended_days_ago": 4}),
        ("ended_forty_days_ago", {"ended_days_ago": 40}),
        ("a_reseller", {"ended_days_ago": 10, "reseller": True}),
        ("banned", {"ended_days_ago": 10, "banned": True}),
        ("a_walk_in_customers_service", {"ended_days_ago": 10, "label": "رضا"}),
        ("soft_deleted", {"ended_days_ago": 10, "deleted": True}),
        ("still_running", {"ended_days_ago": None}),
    ],
)
async def test_a_service_that_does_not_qualify_gets_nothing_and_the_panel_is_not_asked(
    two_sessions: tuple[Factory, str], case: str, kwargs: dict[str, Any]
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        await _paid_service(session, 1, "svc_a", **kwargs)
        await session.commit()
    await _switch_on(factory, winback=ON_SINCE)
    bot = FakeBot()
    panel = FakePanel({"svc_a": "expired"})
    assert await _hourly(factory, bot, panel) == 0, case
    assert panel.asked == []


@pytest.mark.asyncio
async def test_a_service_that_ended_before_switch_on_is_never_nudged(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        await _paid_service(session, 1, "svc_a", ended_days_ago=10)
        await session.commit()
    await _switch_on(factory, winback=NOW - timedelta(days=9))
    bot = FakeBot()
    assert await _hourly(factory, bot, FakePanel({"svc_a": "expired"})) == 0


@pytest.mark.asyncio
async def test_another_service_still_running_keeps_the_customer_quiet(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user = await _paid_service(session, 1, "svc_old", ended_days_ago=10)
        await _paid_service(session, 1, "svc_new", ended_days_ago=-20, user=user)
        await session.commit()
    await _switch_on(factory, winback=ON_SINCE)
    bot = FakeBot()
    panel = FakePanel({"svc_old": "expired", "svc_new": "active"})
    assert await _hourly(factory, bot, panel) == 0


@pytest.mark.asyncio
async def test_a_service_nobody_can_date_counts_as_running(
    two_sessions: tuple[Factory, str],
) -> None:
    """An imported service has no order: unknown is treated as running, never as ended."""
    factory, _ = two_sessions
    async with factory() as session:
        user = await _paid_service(session, 1, "svc_old", ended_days_ago=10)
        await make_account(session, user, "svc_imported")
        await session.commit()
    await _switch_on(factory, winback=ON_SINCE)
    bot = FakeBot()
    assert await _hourly(factory, bot, FakePanel({"svc_old": "expired"})) == 0


@pytest.mark.asyncio
async def test_several_ended_services_make_one_message_about_the_latest(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user = await _paid_service(session, 1, "svc_old", ended_days_ago=20)
        await _paid_service(session, 1, "svc_late", ended_days_ago=8, user=user)
        await session.commit()
    await _switch_on(factory, winback=ON_SINCE)
    bot = FakeBot()
    panel = FakePanel({"svc_old": "expired", "svc_late": "expired"})
    assert await _hourly(factory, bot, panel) == 1
    assert texts.notify_winback.format(name="svc_late") == bot.sent[0][1]
    assert await _hourly(factory, bot, panel) == 0


@pytest.mark.asyncio
async def test_at_most_one_winback_per_user_in_ninety_days(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user = await _paid_service(session, 1, "svc_a", ended_days_ago=10)
        await make_notice(
            session,
            user,
            kind=NOTICE_MKT_WINBACK,
            event_key="mkt:winback:1:2026-06-01",
            delivered_at=NOW - timedelta(days=60),
        )
        await session.commit()
    await _switch_on(factory, winback=ON_SINCE)
    bot = FakeBot()
    panel = FakePanel({"svc_a": "expired"})
    assert await _hourly(factory, bot, panel) == 0, "a winback 60 days ago is still inside 90"
    async with factory() as session:
        old = (
            await session.scalars(
                select(ServiceNotice).where(ServiceNotice.event_key == "mkt:winback:1:2026-06-01")
            )
        ).one()
        old.sent_at = NOW - timedelta(days=91)
        await session.commit()
    assert await _hourly(factory, bot, panel) == 1


@pytest.mark.asyncio
async def test_a_user_nudged_today_gets_no_winback_and_the_panel_is_not_asked(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user = await _paid_service(session, 1, "svc_a", ended_days_ago=10)
        await make_notice(
            session,
            user,
            kind=NOTICE_MKT_TRIAL_CONVERT,
            event_key="mkt:trial_convert:earlier",
            delivered_at=hours_ago(1),
        )
        await session.commit()
    await _switch_on(factory, winback=ON_SINCE)
    bot = FakeBot()
    panel = FakePanel({"svc_a": "expired"})
    assert await _hourly(factory, bot, panel) == 0
    assert panel.asked == []
    # Past the 24 hours the same winback goes out.
    async with factory() as session:
        notice = (await session.scalars(select(ServiceNotice))).one()
        notice.delivered_at = hours_ago(25)
        await session.commit()
    assert await _hourly(factory, bot, panel) == 1


@pytest.mark.asyncio
async def test_the_hourly_job_is_off_until_a_campaign_is_switched_on(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        await _ended_trial(session, 1, ended_hours_ago=30)
        await _paid_service(session, 2, "svc_a", ended_days_ago=10)
        await session.commit()
    bot = FakeBot()
    panel = FakePanel({"svc_a": "expired"})
    assert await _hourly(factory, bot, panel) == 0
    assert bot.sent == [] and panel.asked == []
    assert await _keys(factory) == []

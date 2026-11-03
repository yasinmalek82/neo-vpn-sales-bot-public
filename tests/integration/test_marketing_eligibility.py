"""Who may be nudged (``services/marketing.py``): one function, one test per condition.

Each condition is paired with the plain case that passes, so a test goes red when
its own line is broken and not when the setup is.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import OrderStatus
from database.models.marketing import (
    NOTICE_MKT_CART,
    NOTICE_MKT_TRIAL_CONVERT,
    NOTICE_MKT_WINBACK,
)
from database.models.order import TRIAL_KIND_PERSONAL, TRIAL_KIND_RESELLER
from services.marketing import NUDGE_CAP, may_nudge, nudged_within_cap
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


@pytest.mark.asyncio
async def test_a_retail_customer_with_an_ordinary_service_may_be_nudged(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user = await make_user(session, 1)
        plan = await make_plan(session)
        account = await make_account(session, user, "svc_one")
        await make_order(session, user, plan, panel_username="svc_one")
        assert await may_nudge(session, user_id=int(user.id), account_id=int(account.id))


@pytest.mark.asyncio
async def test_a_reseller_is_never_nudged(two_sessions: tuple[Factory, str]) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user = await make_user(session, 1, reseller=True)
        account = await make_account(session, user, "svc_one")
        assert not await may_nudge(session, user_id=int(user.id), account_id=int(account.id))


@pytest.mark.asyncio
async def test_a_banned_user_is_never_nudged(two_sessions: tuple[Factory, str]) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user = await make_user(session, 1, banned=True)
        account = await make_account(session, user, "svc_one")
        assert not await may_nudge(session, user_id=int(user.id), account_id=int(account.id))


@pytest.mark.asyncio
async def test_a_former_reseller_is_not_nudged_about_a_walk_in_customers_service(
    two_sessions: tuple[Factory, str],
) -> None:
    """The flag was removed, the label stays on the order that made the service."""
    factory, _ = two_sessions
    async with factory() as session:
        user = await make_user(session, 1)
        plan = await make_plan(session)
        account = await make_account(session, user, "svc_walkin")
        await make_order(session, user, plan, panel_username="svc_walkin", customer_label="رضا")
        assert not await may_nudge(session, user_id=int(user.id), account_id=int(account.id))


@pytest.mark.asyncio
async def test_a_label_on_a_renewal_of_the_service_also_blocks_it(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user = await make_user(session, 1)
        plan = await make_plan(session)
        account = await make_account(session, user, "svc_walkin")
        await make_order(session, user, plan, panel_username="svc_walkin")
        await make_order(session, user, plan, renew_account_id=int(account.id), customer_label="x")
        assert not await may_nudge(session, user_id=int(user.id), account_id=int(account.id))


@pytest.mark.asyncio
async def test_the_label_of_the_order_itself_blocks_a_nudge_about_it(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        # One open order per user, so two users.
        user = await make_user(session, 1)
        other = await make_user(session, 2)
        plan = await make_plan(session)
        plain = await make_order(session, user, plan, status=OrderStatus.PENDING_PAYMENT)
        labelled = await make_order(
            session, other, plan, status=OrderStatus.PENDING_PAYMENT, customer_label="رضا"
        )
        assert await may_nudge(session, user_id=int(user.id), order_id=int(plain.id))
        assert not await may_nudge(session, user_id=int(other.id), order_id=int(labelled.id))


@pytest.mark.asyncio
async def test_the_trial_nudge_needs_a_personal_trial_order(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user = await make_user(session, 1)
        plan = await make_plan(session)
        personal = await make_order(session, user, plan, trial_kind=TRIAL_KIND_PERSONAL, price=0)
        reseller_kind = await make_order(
            session, user, plan, trial_kind=TRIAL_KIND_RESELLER, price=0
        )
        paid = await make_order(session, user, plan)
        for order, expected in ((personal, True), (reseller_kind, False), (paid, False)):
            got = await may_nudge(
                session,
                user_id=int(user.id),
                order_id=int(order.id),
                require_personal_trial=True,
            )
            assert got is expected, order.trial_kind


@pytest.mark.asyncio
async def test_an_unknown_user_or_account_is_not_nudged(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user = await make_user(session, 1)
        assert not await may_nudge(session, user_id=999_999)
        assert not await may_nudge(session, user_id=int(user.id), account_id=999_999)


# --- the 24-hour cap ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_delivered_nudge_inside_24_hours_caps_the_user(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user = await make_user(session, 1)
        await make_notice(
            session,
            user,
            kind=NOTICE_MKT_WINBACK,
            event_key="mkt:winback:1:a",
            delivered_at=hours_ago(23),
        )
        assert await nudged_within_cap(session, user_id=int(user.id), now=NOW)
        assert not await nudged_within_cap(
            session, user_id=int(user.id), now=NOW + timedelta(hours=2)
        )


@pytest.mark.asyncio
async def test_the_cap_is_one_user_at_a_time(two_sessions: tuple[Factory, str]) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        one = await make_user(session, 1)
        two = await make_user(session, 2)
        await make_notice(
            session,
            one,
            kind=NOTICE_MKT_TRIAL_CONVERT,
            event_key="mkt:trial_convert:1",
            delivered_at=hours_ago(1),
        )
        assert await nudged_within_cap(session, user_id=int(one.id), now=NOW)
        assert not await nudged_within_cap(session, user_id=int(two.id), now=NOW)


@pytest.mark.asyncio
async def test_the_cart_reminder_and_lifecycle_notices_do_not_count(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user = await make_user(session, 1)
        account = await make_account(session, user, "svc_one")
        await make_notice(
            session,
            user,
            kind=NOTICE_MKT_CART,
            event_key="mkt:cart:1",
            delivered_at=hours_ago(1),
        )
        await make_notice(
            session,
            user,
            kind="volume_warning",
            event_key="volwarn:1:20:x",
            delivered_at=hours_ago(1),
            account_id=int(account.id),
        )
        assert not await nudged_within_cap(session, user_id=int(user.id), now=NOW)


@pytest.mark.asyncio
async def test_a_nudge_the_customer_never_got_does_not_count(
    two_sessions: tuple[Factory, str],
) -> None:
    """Claimed but not delivered, or refused for good (blocked): nothing reached them."""
    factory, _ = two_sessions
    async with factory() as session:
        user = await make_user(session, 1)
        await make_notice(
            session,
            user,
            kind=NOTICE_MKT_WINBACK,
            event_key="mkt:winback:1:a",
            delivered_at=None,
        )
        await make_notice(
            session,
            user,
            kind=NOTICE_MKT_TRIAL_CONVERT,
            event_key="mkt:trial_convert:1",
            delivered_at=hours_ago(1),
            undelivered=True,
        )
        assert not await nudged_within_cap(session, user_id=int(user.id), now=NOW)


def test_the_cap_window_is_24_hours() -> None:
    assert timedelta(hours=24) == NUDGE_CAP

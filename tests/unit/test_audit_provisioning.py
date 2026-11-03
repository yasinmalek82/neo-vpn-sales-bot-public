"""Logic audit, area PROV: provisioning, renewal, service ops, trials.

Every test here asserts the CORRECT behaviour and is marked strict-xfail while
the bug is open (logic-audit protocol, 2026-09-30). When someone fixes a bug,
its test goes XPASS(strict) = red, which is the cue to delete the marker.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from api_client import UserModify, UserResponse, UserStatus
from api_client.exceptions import PasarGuardAPIError
from database.base import Base
from database.models import Order, OrderStatus, PasarGuardAccount, Plan, User
from database.models.plan import DataLimitResetStrategy
from services.automation import find_renewal_reminder_candidates
from services.customer_lookup import ManualAdjustError, get_customer_card, reset_trial
from services.provisioning import provision_order
from services.service_ops import ServiceOpError, extend_days
from services.trial import get_reseller_trial_quota, list_trial_offers, trial_open_for
from tests.unit.test_trial_per_group import SETTINGS, _legacy_trial

GB = 1024**3
DAY = 86_400


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _user(session: AsyncSession, tg: int = 700, *, reseller: bool = False) -> User:
    user = User(
        telegram_id=tg,
        username=f"u{tg}",
        first_name="U",
        language_code="fa",
        referral_code=f"A{tg:07d}",
        is_admin=False,
        is_reseller=reseller,
    )
    session.add(user)
    await session.flush()
    return user


class _StatefulPanel:
    """A panel whose user keeps ``used_traffic`` across a modify, like the real one.

    Only ``reset_user_data_usage`` zeroes usage on PasarGuard; ``modify_user``
    writes the cap and nothing else.
    """

    def __init__(self, user: UserResponse) -> None:
        self.user = user
        self.client = AsyncMock()
        self.client.get_user = AsyncMock(side_effect=self._get)
        self.client.modify_user = AsyncMock(side_effect=self._modify)
        self.client.subscription_url_from_response = lambda u: u.subscription_url

    async def _get(self, username: str) -> UserResponse:
        _ = username
        return self.user.model_copy()

    async def _modify(self, username: str, payload: UserModify) -> UserResponse:
        _ = username
        if payload.expire is not None:
            self.user.expire = payload.expire
        if payload.data_limit is not None:
            self.user.data_limit = payload.data_limit
        return self.user.model_copy()

    @property
    def remaining_bytes(self) -> int:
        return int(self.user.data_limit or 0) - int(self.user.used_traffic)


def _panel_user(username: str, *, expire: int | None, limit_gb: int, used_gb: int) -> UserResponse:
    return UserResponse(
        id=42,
        username=username,
        status=UserStatus.ACTIVE,
        used_traffic=used_gb * GB,
        created_at=datetime.now(UTC),
        subscription_url=f"https://panel.example.com/sub/{username}",
        data_limit=limit_gb * GB,
        expire=expire,
    )


async def _owned_service(
    session: AsyncSession, *, expires_in: timedelta
) -> tuple[User, Plan, PasarGuardAccount, Order]:
    """A customer with one live 10 GB / 30 d service bought earlier."""
    customer = await _user(session)
    plan = Plan(
        name="volume-10GB-30d",
        description="audit",
        data_limit_gb=10,
        duration_days=30,
        price_toman=100_000,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1],
        hwid_limit=2,
        is_active=True,
    )
    session.add(plan)
    await session.flush()
    account = PasarGuardAccount(
        user_id=customer.id,
        panel_user_id=42,
        panel_username="alice_a1b2",
        subscription_url="https://panel.example.com/sub/alice_a1b2",
        plan_fingerprint="volume:10:30",
    )
    session.add(account)
    first = Order(
        user_id=customer.id,
        plan_id=plan.id,
        status=OrderStatus.ACTIVE,
        price_toman=100_000,
        discount_toman=0,
        panel_username="alice_a1b2",
        delivery_data_limit_gb=10,
        delivery_duration_days=30,
        delivery_hwid_limit=2,
        delivery_group_ids=[1],
        paid_at=datetime.now(UTC) - timedelta(days=27),
        activated_at=datetime.now(UTC) - timedelta(days=27),
        expires_at=datetime.now(UTC) + expires_in,
    )
    session.add(first)
    await session.flush()
    return customer, plan, account, first


async def _renewal_order(session: AsyncSession, customer: User, plan: Plan) -> Order:
    order = Order(
        user_id=customer.id,
        plan_id=plan.id,
        status=OrderStatus.PAYMENT_APPROVED,
        price_toman=100_000,
        discount_toman=0,
        delivery_data_limit_gb=10,
        delivery_duration_days=30,
        delivery_hwid_limit=2,
        delivery_group_ids=[1],
        paid_at=datetime.now(UTC),
    )
    session.add(order)
    await session.flush()
    return order


# --------------------------------------------------------------------------- #
# Renewal
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_early_renew_leaves_leftover_plus_new_volume_usable(
    session: AsyncSession,
) -> None:
    """10 GB cap, 3 GB used, renew 10 GB: the customer should be able to use 17 GB.

    ``modify_user`` does not reset usage (only ``reset_user_data_usage`` does),
    so a cap of ``remaining + plan`` = 17 GB with 3 GB still counted leaves 14.
    """
    customer, plan, _account, _first = await _owned_service(session, expires_in=timedelta(days=3))
    expire = int((datetime.now(UTC) + timedelta(days=3)).timestamp())
    panel = _StatefulPanel(_panel_user("alice_a1b2", expire=expire, limit_gb=10, used_gb=3))
    renewal = await _renewal_order(session, customer, plan)

    await provision_order(session, panel.client, order_id=renewal.id, default_group_ids=[1])

    assert panel.remaining_bytes == 17 * GB


@pytest.mark.asyncio
async def test_a_renewed_service_does_not_get_an_expiry_reminder_for_the_old_date(
    session: AsyncSession,
) -> None:
    customer, plan, _account, _first = await _owned_service(
        session, expires_in=timedelta(days=2, hours=12)
    )
    expire = int((datetime.now(UTC) + timedelta(days=2, hours=12)).timestamp())
    panel = _StatefulPanel(_panel_user("alice_a1b2", expire=expire, limit_gb=10, used_gb=1))
    renewal = await _renewal_order(session, customer, plan)
    await provision_order(session, panel.client, order_id=renewal.id, default_group_ids=[1])

    # The panel now says ~32 days left; nothing about this service is expiring.
    assert panel.user.expire is not None and panel.user.expire > expire + 29 * DAY
    candidates = await find_renewal_reminder_candidates(session, reminder_days=[3])

    assert [c.order.id for c in candidates if c.order.panel_username == "alice_a1b2"] == []


# --------------------------------------------------------------------------- #
# Manual service operations
# --------------------------------------------------------------------------- #


async def _extend_fixture(
    session: AsyncSession, *, expire: int | None
) -> tuple[PasarGuardAccount, AsyncMock]:
    customer = await _user(session, 710)
    account = PasarGuardAccount(
        user_id=customer.id,
        panel_user_id=7,
        panel_username="bob_c3d4",
        subscription_url="https://panel.example.com/sub/bob_c3d4",
    )
    session.add(account)
    await session.flush()
    live = _panel_user("bob_c3d4", expire=expire, limit_gb=10, used_gb=0)
    client = AsyncMock()
    client.get_user = AsyncMock(return_value=live)
    client.extend_subscription = AsyncMock()
    return account, client


@pytest.mark.asyncio
async def test_a_failed_seven_day_extend_does_not_swallow_the_next_thirty_day_request(
    session: AsyncSession,
) -> None:
    expire = int((datetime.now(UTC) + timedelta(days=10)).timestamp())
    account, client = await _extend_fixture(session, expire=expire)
    client.extend_subscription = AsyncMock(side_effect=[PasarGuardAPIError("boom"), None])

    with pytest.raises(ServiceOpError):
        await extend_days(session, client, account_id=account.id, days=7, operation_key="tap-1")
    await extend_days(session, client, account_id=account.id, days=30, operation_key="tap-2")

    asked = client.extend_subscription.await_args_list[-1].kwargs["expire"]
    assert asked == expire + 30 * DAY


@pytest.mark.asyncio
async def test_extending_a_service_with_no_expiry_does_not_give_it_one(
    session: AsyncSession,
) -> None:
    account, client = await _extend_fixture(session, expire=None)

    with contextlib.suppress(ServiceOpError):
        await extend_days(session, client, account_id=account.id, days=30)

    client.extend_subscription.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Trials
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_giveaway_that_never_delivered_does_not_use_the_monthly_quota(
    session: AsyncSession,
) -> None:
    from services.reseller import set_rate

    shop = await _user(session, 720, reseller=True)
    await set_rate(session, shop.id, trial_quota_monthly=1)
    plan = Plan(
        name="trial-1GB-1d",
        description="audit",
        data_limit_gb=1,
        duration_days=1,
        price_toman=0,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[],
        hwid_limit=1,
        is_active=True,
    )
    session.add(plan)
    await session.flush()
    session.add(
        Order(
            user_id=shop.id,
            plan_id=plan.id,
            status=OrderStatus.PROVISION_FAILED,
            price_toman=0,
            discount_toman=0,
            trial_kind="reseller",
        )
    )
    await session.flush()

    quota = await get_reseller_trial_quota(session, shop.id)

    assert quota.used == 0


async def _two_direct_groups(session: AsyncSession) -> None:
    from services.store_admin import (
        commit_and_activate,
        create_draft_category,
        set_category_trial,
        update_draft_category,
    )

    for n, title in enumerate(
        (
            "\u0646\u0627\u0645\u062d\u062f\u0648\u062f \u0627\u0644\u0641",
            "\u0646\u0627\u0645\u062d\u062f\u0648\u062f \u0628",
        )
    ):
        cat = await create_draft_category(session, title_fa=title, kind="unlimited")
        await update_draft_category(session, cat.id, panel_group_ids=[30 + n])
        await set_category_trial(session, cat.id, data_limit_gb=1, duration_days=1)
    await commit_and_activate(session, notes="seed")


@pytest.mark.asyncio
async def test_the_card_marks_every_group_the_customer_cannot_take(session: AsyncSession) -> None:
    await _two_direct_groups(session)
    user = await _user(session, 730)
    await _legacy_trial(session, user, kind="personal")
    user.trial_claimed_at = datetime.now(UTC)
    await session.flush()

    offers = await list_trial_offers(session, SETTINGS)
    blocked = [o.title_fa for o in offers if not await trial_open_for(session, user, o)]
    card = await get_customer_card(session, user.id)
    marked = [m.title for m in (card.trials or []) if m.taken_at is not None]

    assert len(blocked) == 2
    assert sorted(marked) == sorted(blocked)


@pytest.mark.asyncio
async def test_reset_unblocks_a_trial_whose_kind_was_never_backfilled(
    session: AsyncSession,
) -> None:
    await _two_direct_groups(session)
    user = await _user(session, 740)
    await _legacy_trial(session, user, kind=None)
    user.trial_claimed_at = datetime.now(UTC)
    await session.flush()

    with contextlib.suppress(ManualAdjustError):
        await reset_trial(session, user.id)
    offers = await list_trial_offers(session, SETTINGS)

    assert [await trial_open_for(session, user, o) for o in offers] == [True, True]

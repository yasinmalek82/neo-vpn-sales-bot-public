"""The pure parts of «تمدید از نو»: the retry test, the group a service belongs to, the toggle."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from api_client import UserResponse, UserStatus
from bot.handlers.purchase import _duration_text
from database.base import Base
from database.models import Order, OrderStatus, PasarGuardAccount, Plan, User
from database.models.plan import DataLimitResetStrategy
from services.account_orders import account_was_created_by_an_order, is_trial_account
from services.provisioning import _scratch_applied
from services.renewal import category_renew_off, volume_category_of_service

GB = 1024**3
NOW = datetime.now(UTC)


def _user(*, expire_in_days: float = 30, limit_gb: int = 37, hwid: int | None = 0) -> UserResponse:
    return UserResponse(
        id=1,
        username="u",
        status=UserStatus.ACTIVE,
        used_traffic=12 * GB,
        created_at=NOW,
        expire=int((NOW + timedelta(days=expire_in_days)).timestamp()),
        data_limit=limit_gb * GB,
        group_ids=[2, 6, 8],
        hwid_limit=hwid,
    )


TARGET = int((NOW + timedelta(days=30)).timestamp())


def test_applied_is_equality_over_every_value_written_and_ignores_usage() -> None:
    ok = {"expire_unix": TARGET, "data_limit": 37 * GB, "hwid_limit": 0}
    assert _scratch_applied(_user(), **ok)  # 12 GB used: the customer is using it again
    assert not _scratch_applied(_user(expire_in_days=10), **ok)
    assert not _scratch_applied(_user(limit_gb=50), **ok)
    assert not _scratch_applied(_user(hwid=2), **ok)
    # A shorter new period is not «applied» just because the old expiry is later.
    assert not _scratch_applied(_user(expire_in_days=60), **ok)
    # No device limit to keep (panel global policy): it is neither written nor compared.
    assert _scratch_applied(_user(hwid=5), expire_unix=TARGET, data_limit=37 * GB, hwid_limit=None)


def _cat(
    cid: int, slug: str, *, groups: list[int], parent: int | None = None, kind: str = "volume"
):
    return SimpleNamespace(
        id=cid,
        slug=slug,
        title_fa=slug,
        enabled=True,
        parent_id=parent,
        sort_order=cid,
        panel_group_ids=groups,
        sales_flags={"kind": kind},
    )


def _cfg(*cats: Any) -> Any:
    return SimpleNamespace(categories=list(cats))


def test_the_group_of_a_volume_service_is_found_by_slug_then_panel_groups_then_the_only_one() -> (
    None
):
    parent = _cat(1, "volume", groups=[])
    special = _cat(2, "special", groups=[2], parent=1)
    economy = _cat(3, "economy", groups=[3], parent=1)
    unlimited = _cat(4, "unlimited", groups=[1], kind="unlimited")
    cfg = _cfg(parent, special, economy, unlimited)
    assert volume_category_of_service(cfg, slug="economy", groups=[2]).slug == "economy"
    assert volume_category_of_service(cfg, slug=None, groups=[2, 6, 8]).slug == "special"
    # Two volume groups and nothing that matches: never a silent default.
    assert volume_category_of_service(cfg, slug=None, groups=[9]) is None
    assert volume_category_of_service(cfg, slug="gone", groups=[]) is None
    only = _cfg(_cat(1, "volume", groups=[5]), unlimited)
    assert volume_category_of_service(only, slug=None, groups=[9]).slug == "volume"
    # An unlimited group is never a volume service's group.
    assert volume_category_of_service(_cfg(unlimited), slug="unlimited", groups=[1]) is None


def test_a_group_below_one_with_renew_off_is_off_too() -> None:
    parent = _cat(1, "volume", groups=[])
    child = _cat(2, "special", groups=[2], parent=1)
    assert not category_renew_off(_cfg(parent, child), child)
    parent.sales_flags["allow_renew"] = False
    assert category_renew_off(_cfg(parent, child), child)
    parent.sales_flags["allow_renew"] = True
    child.sales_flags["allow_renew"] = False
    assert category_renew_off(_cfg(parent, child), child)
    assert not category_renew_off(_cfg(parent, child), parent)


def test_the_summary_says_days_when_it_is_not_whole_months() -> None:
    assert [_duration_text(d) for d in (30, 90, 10, 45)] == ["۱ ماه", "۳ ماه", "۱۰ روز", "۴۵ روز"]


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def _account(session: AsyncSession, orders: list[dict[str, Any]]) -> PasarGuardAccount:
    user = User(telegram_id=1, referral_code="R1", wallet_balance_toman=0)
    plan = Plan(
        name="p",
        description="p",
        data_limit_gb=1,
        duration_days=1,
        price_toman=1,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1],
        hwid_limit=0,
        is_active=True,
    )
    session.add_all([user, plan])
    await session.flush()
    account = PasarGuardAccount(user_id=user.id, panel_user_id=1, panel_username="svc")
    session.add(account)
    await session.flush()
    for fields in orders:
        fields.setdefault("renew_account_id", None)
        session.add(
            Order(
                user_id=user.id,
                plan_id=plan.id,
                status=fields.pop("status", OrderStatus.ACTIVE),
                price_toman=0,
                discount_toman=0,
                panel_username="svc",
                **fields,
            )
        )
    await session.flush()
    return account


@pytest.mark.asyncio
async def test_a_trial_is_an_account_whose_every_order_is_a_trial(session: AsyncSession) -> None:
    only_trial = await _account(session, [{"trial_kind": "personal"}])
    assert await is_trial_account(session, only_trial)


@pytest.mark.asyncio
async def test_no_order_is_not_a_trial_and_a_paid_order_ends_it(session: AsyncSession) -> None:
    imported = await _account(session, [])
    assert not await is_trial_account(session, imported)
    assert not await account_was_created_by_an_order(session, imported)


@pytest.mark.asyncio
async def test_a_paid_renewal_after_a_trial_makes_it_a_service(session: AsyncSession) -> None:
    account = await _account(
        session,
        [{"trial_kind": "personal"}, {"trial_kind": None, "renew_account_id": 1}],
    )
    assert not await is_trial_account(session, account)


@pytest.mark.asyncio
async def test_only_switches_and_resets_do_not_make_an_account_ours(session: AsyncSession) -> None:
    account = await _account(
        session,
        [
            {"renew_account_id": 1, "renew_resets": True},
            {"renew_account_id": 1, "renew_mode": "reset"},
        ],
    )
    assert not await account_was_created_by_an_order(session, account)
    session.add(
        Order(
            user_id=account.user_id,
            plan_id=1,
            status=OrderStatus.ACTIVE,
            price_toman=0,
            discount_toman=0,
            panel_username="svc",
        )
    )
    await session.flush()
    assert await account_was_created_by_an_order(session, account)

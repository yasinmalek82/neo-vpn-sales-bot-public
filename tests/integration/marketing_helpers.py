"""Rows for the marketing tests: users, services, orders, notices. One place, so a
campaign test says only what makes its case different."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Order, OrderStatus, PasarGuardAccount, Plan, User
from database.models.lifecycle import ServiceNotice
from database.models.plan import DataLimitResetStrategy

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

_panel_ids = itertools.count(1)


async def make_plan(session: AsyncSession, *, gb: int = 10, days: int = 30) -> Plan:
    plan = Plan(
        name=f"volume-{gb}GB-{days}d",
        description="test",
        data_limit_gb=gb,
        duration_days=days,
        price_toman=150_000,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1],
        hwid_limit=2,
        is_active=True,
    )
    session.add(plan)
    await session.flush()
    return plan


async def make_user(
    session: AsyncSession, telegram_id: int, *, reseller: bool = False, banned: bool = False
) -> User:
    user = User(
        telegram_id=telegram_id,
        first_name=f"U{telegram_id}",
        referral_code=f"MK{telegram_id:08d}"[-16:],
        is_reseller=reseller,
        is_banned=banned,
    )
    session.add(user)
    await session.flush()
    return user


async def make_account(
    session: AsyncSession, user: User, name: str, *, deleted_at: datetime | None = None
) -> PasarGuardAccount:
    account = PasarGuardAccount(
        user_id=user.id,
        panel_user_id=next(_panel_ids),
        panel_username=name,
        deleted_at=deleted_at,
    )
    session.add(account)
    await session.flush()
    return account


async def make_order(
    session: AsyncSession,
    user: User,
    plan: Plan,
    *,
    status: OrderStatus = OrderStatus.ACTIVE,
    price: int = 150_000,
    created_at: datetime | None = None,
    activated_at: datetime | None = None,
    expires_at: datetime | None = None,
    customer_label: str | None = None,
    trial_kind: str | None = None,
    panel_username: str | None = None,
    renew_account_id: int | None = None,
) -> Order:
    order = Order(
        user_id=user.id,
        plan_id=plan.id,
        status=status,
        price_toman=price,
        discount_toman=0,
        created_at=created_at or NOW,
        activated_at=activated_at,
        expires_at=expires_at,
        customer_label=customer_label,
        trial_kind=trial_kind,
        panel_username=panel_username,
        renew_account_id=renew_account_id,
    )
    session.add(order)
    await session.flush()
    return order


async def make_notice(
    session: AsyncSession,
    user: User,
    *,
    kind: str,
    event_key: str,
    delivered_at: datetime | None,
    undelivered: bool = False,
    account_id: int | None = None,
) -> ServiceNotice:
    notice = ServiceNotice(
        event_key=event_key,
        kind=kind,
        account_id=account_id,
        user_id=user.id,
        sent_at=delivered_at,
        delivered_at=delivered_at,
        undelivered=undelivered,
    )
    session.add(notice)
    await session.flush()
    return notice


def hours_ago(hours: float, *, now: datetime = NOW) -> datetime:
    return now - timedelta(hours=hours)

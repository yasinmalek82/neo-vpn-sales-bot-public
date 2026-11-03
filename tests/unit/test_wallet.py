"""Wallet credit/debit + provision-fail credit + checkout debit."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from database.base import Base
from database.models import Order, OrderStatus, Plan, User
from database.models.plan import DataLimitResetStrategy
from services.wallet import (
    REASON_CHECKOUT_DEBIT,
    InsufficientWalletFunds,
    checkout_debit_key,
    credit,
    credit_provision_failed,
    debit,
    get_balance,
    list_recent_entries,
    provision_fail_credit_key,
)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _user(session: AsyncSession) -> User:
    user = User(
        telegram_id=42,
        username="u",
        first_name="U",
        last_name=None,
        language_code="fa",
        referral_code="WALLE001",
        is_admin=False,
        wallet_balance_toman=0,
    )
    session.add(user)
    await session.flush()
    return user


async def _order(session: AsyncSession, user: User, price: int = 100_000) -> Order:
    plan = Plan(
        name="volume-10GB-30d",
        description="t",
        data_limit_gb=10,
        duration_days=30,
        price_toman=price,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1],
        hwid_limit=1,
        is_active=True,
    )
    session.add(plan)
    await session.flush()
    order = Order(
        user_id=user.id,
        plan_id=plan.id,
        status=OrderStatus.PROVISION_FAILED,
        price_toman=price,
        discount_toman=0,
    )
    session.add(order)
    await session.flush()
    return order


@pytest.mark.asyncio
async def test_credit_debit_and_idempotency(session: AsyncSession) -> None:
    user = await _user(session)
    e1, created = await credit(
        session,
        user_id=user.id,
        amount_toman=50_000,
        reason="test",
        idempotency_key="k1",
    )
    assert created is True
    assert await get_balance(session, user.id) == 50_000
    e2, created2 = await credit(
        session,
        user_id=user.id,
        amount_toman=50_000,
        reason="test",
        idempotency_key="k1",
    )
    assert created2 is False
    assert e1.id == e2.id
    assert await get_balance(session, user.id) == 50_000

    await debit(
        session,
        user_id=user.id,
        amount_toman=20_000,
        reason=REASON_CHECKOUT_DEBIT,
        idempotency_key="d1",
    )
    assert await get_balance(session, user.id) == 30_000
    with pytest.raises(InsufficientWalletFunds):
        await debit(
            session,
            user_id=user.id,
            amount_toman=40_000,
            reason=REASON_CHECKOUT_DEBIT,
            idempotency_key="d2",
        )


@pytest.mark.asyncio
async def test_provision_fail_credit_n1(session: AsyncSession) -> None:
    user = await _user(session)
    order = await _order(session, user, price=120_000)
    entry, created = await credit_provision_failed(
        session,
        user_id=user.id,
        order_id=order.id,
        amount_toman=order.price_toman,
        max_n=1,
    )
    assert created is True
    assert entry is not None
    assert entry.idempotency_key == provision_fail_credit_key(order.id)
    assert await get_balance(session, user.id) == 120_000
    _, created2 = await credit_provision_failed(
        session,
        user_id=user.id,
        order_id=order.id,
        amount_toman=order.price_toman,
        max_n=1,
    )
    assert created2 is False
    assert await get_balance(session, user.id) == 120_000
    recent = await list_recent_entries(session, user_id=user.id, limit=5)
    assert len(recent) == 1


@pytest.mark.asyncio
async def test_checkout_debit_key(session: AsyncSession) -> None:
    user = await _user(session)
    await credit(session, user_id=user.id, amount_toman=10_000, reason="x", idempotency_key="seed")
    order = await _order(session, user, price=7_000)
    await debit(
        session,
        user_id=user.id,
        amount_toman=7_000,
        reason=REASON_CHECKOUT_DEBIT,
        idempotency_key=checkout_debit_key(order.id),
        order_id=order.id,
    )
    assert await get_balance(session, user.id) == 3_000

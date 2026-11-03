"""``order_is_delivered`` is what keeps a replayed approve or retry from re-sending the card."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from database.base import Base
from database.models import OrderStatus, Plan, User
from database.models.plan import DataLimitResetStrategy
from services.orders import create_pending_order, order_is_delivered
from services.pricing import PriceResult


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "delivered"),
    [
        (OrderStatus.PAYMENT_REVIEW, False),
        (OrderStatus.PAYMENT_APPROVED, False),
        (OrderStatus.PROVISIONING, False),
        (OrderStatus.PROVISION_FAILED, False),
        (OrderStatus.ACTIVE, True),
    ],
)
async def test_only_an_active_order_counts_as_delivered(
    session: AsyncSession, status: OrderStatus, delivered: bool
) -> None:
    buyer = User(telegram_id=8001, referral_code="BUY8001")
    plan = Plan(
        name="p",
        description="t",
        data_limit_gb=10,
        duration_days=30,
        price_toman=50_000,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1],
        hwid_limit=0,
        is_active=True,
    )
    session.add_all([buyer, plan])
    await session.flush()
    order = await create_pending_order(
        session,
        user_id=buyer.id,
        plan=plan,
        price=PriceResult(
            original_price=50_000,
            discount_amount=0,
            final_price=50_000,
            currency="IRT",
            meta={"volume_gb": 10, "duration_days": 30},
        ),
    )
    order.status = status
    await session.commit()
    assert await order_is_delivered(session, order.id) is delivered
    assert await order_is_delivered(session, order.id + 99) is False

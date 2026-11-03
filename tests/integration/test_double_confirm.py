"""Two confirms at once must leave a single open order."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from database.base import Base
from database.models import Order, OrderStatus, Plan, User
from database.models.plan import DataLimitResetStrategy
from services.orders import create_pending_order
from services.pricing import PriceResult

PRICE = PriceResult(
    original_price=100_000,
    discount_amount=0,
    final_price=100_000,
    currency="IRT",
    meta={},
)


@pytest.mark.asyncio
async def test_two_simultaneous_confirms_share_one_open_order(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'race.db'}"
    engine = create_async_engine(url, connect_args={"timeout": 5})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            user = User(
                telegram_id=8801,
                username="buyer",
                first_name="B",
                last_name=None,
                language_code="fa",
                referral_code="RACE8801",
                is_admin=False,
            )
            plan = Plan(
                name="volume-10GB-30d",
                description="t",
                data_limit_gb=10,
                duration_days=30,
                price_toman=100_000,
                data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
                group_ids=[1],
                hwid_limit=1,
                is_active=True,
            )
            session.add_all([user, plan])
            await session.commit()
            user_id = int(user.id)
            plan_id = int(plan.id)

        async def confirm() -> int:
            async with factory() as session:
                live = await session.get(Plan, plan_id)
                assert live is not None
                order = await create_pending_order(session, user_id=user_id, plan=live, price=PRICE)
                await session.commit()
                return int(order.id)

        first, second = await asyncio.gather(confirm(), confirm())
        assert first == second

        async with factory() as session:
            open_count = await session.scalar(
                select(func.count())
                .select_from(Order)
                .where(
                    Order.user_id == user_id,
                    Order.status.in_((OrderStatus.PENDING_PAYMENT, OrderStatus.PAYMENT_REVIEW)),
                )
            )
        assert int(open_count or 0) == 1
    finally:
        await engine.dispose()

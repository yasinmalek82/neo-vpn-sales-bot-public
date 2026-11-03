"""Two customers racing for the last use of a promo code: exactly one order carries it."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import Order, Plan, PromoRedemption
from services.orders import create_pending_order
from services.pricing import PriceResult
from services.promo_rules import PromoUsedUp
from tests.integration.marketing_helpers import make_plan, make_user

Factory = async_sessionmaker[AsyncSession]


def _price(total_limit: int) -> PriceResult:
    return PriceResult(
        original_price=100_000,
        discount_amount=15_000,
        final_price=85_000,
        currency="IRT",
        meta={"promo_redeem": {"code": "RACE", "per_user_limit": 0, "total_limit": total_limit}},
    )


async def _race(factory: Factory, plan_id: int, user_ids: list[int], limit: int) -> list[object]:
    async def confirm(user_id: int) -> object:
        async with factory() as session:
            live = await session.get(Plan, plan_id)
            assert live is not None
            try:
                order = await create_pending_order(
                    session, user_id=user_id, plan=live, price=_price(limit)
                )
                await session.commit()
            except PromoUsedUp:
                await session.rollback()
                return "used_up"
            return int(order.id)

    return list(await asyncio.gather(*(confirm(u) for u in user_ids)))


@pytest.mark.asyncio
async def test_two_orders_racing_for_the_last_use_leave_exactly_one(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        first = await make_user(session, 9301)
        second = await make_user(session, 9302)
        plan = await make_plan(session)
        await session.commit()
        users, plan_id = [int(first.id), int(second.id)], int(plan.id)

    results = await _race(factory, plan_id, users, limit=1)
    assert sorted(map(str, results)).count("used_up") == 1, results
    assert len([r for r in results if isinstance(r, int)]) == 1, results

    async with factory() as session:
        orders = await session.scalar(select(func.count()).select_from(Order))
        rows = (await session.execute(select(PromoRedemption))).scalars().all()
        assert int(orders or 0) == 1
        assert len(rows) == 1
        winner = next(r for r in results if isinstance(r, int))
        assert rows[0].order_id == winner and rows[0].code == "RACE"


@pytest.mark.asyncio
async def test_three_racing_for_two_uses_leave_two(two_sessions: tuple[Factory, str]) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        users = [int((await make_user(session, 9310 + i)).id) for i in range(3)]
        plan = await make_plan(session)
        await session.commit()
        plan_id = int(plan.id)

    results = await _race(factory, plan_id, users, limit=2)
    assert len([r for r in results if isinstance(r, int)]) == 2, results
    assert results.count("used_up") == 1, results
    async with factory() as session:
        assert (
            int(await session.scalar(select(func.count()).select_from(PromoRedemption)) or 0) == 2
        )
        assert int(await session.scalar(select(func.count()).select_from(Order)) or 0) == 2

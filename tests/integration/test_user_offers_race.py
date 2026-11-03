"""Two confirms at once attach a personal offer to one order, once."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import Order, OrderStatus, Plan, UserOffer
from services.orders import create_pending_order
from services.pricing import PriceResult
from services.user_offers import OfferNoLongerLive
from tests.integration.marketing_helpers import make_plan, make_user

Factory = async_sessionmaker[AsyncSession]


@pytest.mark.asyncio
async def test_a_double_tap_on_confirm_attaches_the_offer_once(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user = await make_user(session, 9101)
        plan = await make_plan(session)
        offer = UserOffer(
            user_id=user.id,
            campaign="winback",
            percent=10,
            event_key="race:1",
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
        session.add(offer)
        await session.commit()
        user_id, plan_id, offer_id = int(user.id), int(plan.id), int(offer.id)

    price = PriceResult(
        original_price=100_000,
        discount_amount=10_000,
        final_price=90_000,
        currency="IRT",
        meta={"user_offer_id": offer_id, "user_offer_percent": 10},
    )

    async def confirm() -> int | None:
        async with factory() as session:
            live = await session.get(Plan, plan_id)
            assert live is not None
            try:
                order = await create_pending_order(session, user_id=user_id, plan=live, price=price)
                await session.commit()
            except OfferNoLongerLive:
                await session.rollback()
                return None
            return int(order.id)

    first, second = await asyncio.gather(confirm(), confirm())
    won = {x for x in (first, second) if x is not None}
    assert len(won) == 1, (first, second)

    async with factory() as session:
        orders = await session.scalar(
            select(func.count()).select_from(Order).where(Order.user_id == user_id)
        )
        stored = await session.get(UserOffer, offer_id)
        assert stored is not None
        assert int(orders or 0) == 1
        assert stored.order_id == won.pop()
        assert stored.voided_at is None


@pytest.mark.asyncio
async def test_a_second_user_order_cannot_take_an_offer_a_live_order_holds(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user = await make_user(session, 9102)
        plan = await make_plan(session)
        held = Order(
            user_id=user.id,
            plan_id=plan.id,
            status=OrderStatus.ACTIVE,
            price_toman=1,
            discount_toman=0,
        )
        session.add(held)
        await session.flush()
        offer = UserOffer(
            user_id=user.id,
            campaign="winback",
            percent=10,
            event_key="race:2",
            expires_at=datetime.now(UTC) + timedelta(hours=24),
            order_id=held.id,
        )
        session.add(offer)
        await session.commit()
        user_id, offer_id, held_id = int(user.id), int(offer.id), int(held.id)
        plan_row = plan

    price = PriceResult(
        100_000, 10_000, 90_000, "IRT", {"user_offer_id": offer_id, "user_offer_percent": 10}
    )
    async with factory() as session:
        live = await session.get(Plan, plan_row.id)
        assert live is not None
        with pytest.raises(OfferNoLongerLive):
            await create_pending_order(session, user_id=user_id, plan=live, price=price)
        await session.rollback()
    async with factory() as session:
        stored = await session.get(UserOffer, offer_id)
        assert stored is not None and stored.order_id == held_id
        others = await session.scalar(
            select(func.count()).select_from(Order).where(Order.id != held_id)
        )
        assert int(others or 0) == 0

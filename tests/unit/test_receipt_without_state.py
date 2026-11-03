"""A receipt photo is matched to the customer's order, not only to their FSM state.

A bottom-keyboard tap (or a restart: the storage is in memory) drops the state while
the order stays PENDING_PAYMENT. These pin what the stateless receipt filter accepts.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from bot.handlers.payment import _OrderAwaitingReceipt
from database.base import Base
from database.models import OrderStatus, Plan, User
from database.models.plan import DataLimitResetStrategy
from services.orders import create_pending_order
from services.pricing import PriceResult

PRICE = 50_000


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _customer_with_order(
    session: AsyncSession, *, status: OrderStatus, wallet: int = 0
) -> tuple[User, int]:
    buyer = User(telegram_id=8001, referral_code="BUY8001", first_name="Ali")
    plan = Plan(
        name="p",
        description="t",
        data_limit_gb=10,
        duration_days=30,
        price_toman=PRICE,
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
            original_price=PRICE,
            discount_amount=0,
            final_price=PRICE,
            currency="IRT",
            meta={"volume_gb": 10, "duration_days": 30},
        ),
        service_name="ali",
        wallet_applied_toman=wallet,
    )
    order.status = status
    await session.commit()
    return buyer, int(order.id)


async def _match(session: AsyncSession, buyer: User | None) -> bool | dict:
    return await _OrderAwaitingReceipt()(SimpleNamespace(), session, buyer)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_one_order_waiting_for_a_card_receipt_is_matched(session: AsyncSession) -> None:
    buyer, order_id = await _customer_with_order(session, status=OrderStatus.PENDING_PAYMENT)
    assert await _match(session, buyer) == {"receipt_order_id": order_id}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [OrderStatus.PAYMENT_REVIEW, OrderStatus.ACTIVE, OrderStatus.EXPIRED]
)
async def test_an_order_that_is_not_waiting_for_a_receipt_is_not_matched(
    session: AsyncSession, status: OrderStatus
) -> None:
    buyer, _ = await _customer_with_order(session, status=status)
    assert await _match(session, buyer) is False


@pytest.mark.asyncio
async def test_a_wallet_covered_order_has_no_card_receipt_to_wait_for(
    session: AsyncSession,
) -> None:
    buyer, _ = await _customer_with_order(session, status=OrderStatus.PENDING_PAYMENT, wallet=PRICE)
    assert await _match(session, buyer) is False


@pytest.mark.asyncio
async def test_a_stranger_with_no_order_is_not_matched(session: AsyncSession) -> None:
    stranger = User(telegram_id=9009, referral_code="STR9009")
    session.add(stranger)
    await session.commit()
    assert await _match(session, stranger) is False
    assert await _match(session, None) is False

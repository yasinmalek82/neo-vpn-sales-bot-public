"""Unit tests for card-to-card payment transitions."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from database.base import Base
from database.models import Order, OrderStatus, PaymentStatus, Plan, User
from database.models.plan import DataLimitResetStrategy
from services.payments import (
    PaymentError,
    approve_payment,
    reject_payment,
    submit_card_receipt,
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


async def _seed(session: AsyncSession) -> tuple[User, User, Order]:
    customer = User(
        telegram_id=1001,
        username="cust",
        first_name="C",
        last_name=None,
        language_code="fa",
        referral_code="CUST0001",
        is_admin=False,
    )
    admin = User(
        telegram_id=9001,
        username="admin",
        first_name="A",
        last_name=None,
        language_code="fa",
        referral_code="ADMIN001",
        is_admin=True,
    )
    plan = Plan(
        name="volume-10GB-30d",
        description="test",
        data_limit_gb=10,
        duration_days=30,
        price_toman=150_000,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1],
        hwid_limit=1,
        is_active=True,
    )
    session.add_all([customer, admin, plan])
    await session.flush()
    order = Order(
        user_id=customer.id,
        plan_id=plan.id,
        status=OrderStatus.PENDING_PAYMENT,
        price_toman=150_000,
        discount_toman=0,
    )
    session.add(order)
    await session.flush()
    return customer, admin, order


@pytest.mark.asyncio
async def test_submit_receipt_transition(session: AsyncSession) -> None:
    customer, _admin, order = await _seed(session)
    payment = await submit_card_receipt(
        session,
        order_id=order.id,
        user_id=customer.id,
        receipt_file_id="file_abc",
        card_number_display="6037-1111",
    )
    await session.refresh(order)
    assert payment.status == PaymentStatus.pending
    assert payment.method == "card_to_card"
    assert payment.receipt_file_id == "file_abc"
    assert order.status == OrderStatus.PAYMENT_REVIEW


@pytest.mark.asyncio
async def test_duplicate_submit_idempotent(session: AsyncSession) -> None:
    customer, _admin, order = await _seed(session)
    p1 = await submit_card_receipt(
        session,
        order_id=order.id,
        user_id=customer.id,
        receipt_file_id="file_1",
        card_number_display="6037",
    )
    p2 = await submit_card_receipt(
        session,
        order_id=order.id,
        user_id=customer.id,
        receipt_file_id="file_2",
        card_number_display="6037",
    )
    assert p1.id == p2.id
    assert p2.receipt_file_id == "file_1"


@pytest.mark.asyncio
async def test_approve_and_reject(session: AsyncSession) -> None:
    customer, admin, order = await _seed(session)
    await submit_card_receipt(
        session,
        order_id=order.id,
        user_id=customer.id,
        receipt_file_id="f",
        card_number_display="x",
    )
    order2, payment = await approve_payment(session, order_id=order.id, admin_user_id=admin.id)
    assert order2.status == OrderStatus.PAYMENT_APPROVED
    assert payment.status == PaymentStatus.verified
    assert payment.admin_id == admin.id
    assert payment.verified_at is not None


@pytest.mark.asyncio
async def test_reject_payment(session: AsyncSession) -> None:
    customer, admin, order = await _seed(session)
    await submit_card_receipt(
        session,
        order_id=order.id,
        user_id=customer.id,
        receipt_file_id="f",
        card_number_display="x",
    )
    order2, payment = await reject_payment(
        session, order_id=order.id, admin_user_id=admin.id, note="bad"
    )
    assert order2.status == OrderStatus.REJECTED
    assert payment.status == PaymentStatus.rejected
    assert payment.note == "bad"


@pytest.mark.asyncio
async def test_duplicate_approve_noop(session: AsyncSession) -> None:
    customer, admin, order = await _seed(session)
    await submit_card_receipt(
        session,
        order_id=order.id,
        user_id=customer.id,
        receipt_file_id="f",
        card_number_display="x",
    )
    o1, p1 = await approve_payment(session, order_id=order.id, admin_user_id=admin.id)
    o2, p2 = await approve_payment(session, order_id=order.id, admin_user_id=admin.id)
    assert o1.status == o2.status == OrderStatus.PAYMENT_APPROVED
    assert p1.id == p2.id
    assert p2.status == PaymentStatus.verified


@pytest.mark.asyncio
async def test_submit_wrong_owner(session: AsyncSession) -> None:
    customer, admin, order = await _seed(session)
    with pytest.raises(PaymentError):
        await submit_card_receipt(
            session,
            order_id=order.id,
            user_id=admin.id,
            receipt_file_id="f",
            card_number_display="x",
        )

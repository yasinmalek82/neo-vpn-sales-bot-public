"""Purchase hardening: one open order, EXPIRED, receipts helper, service names."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from database.base import Base
from database.models import Order, OrderStatus, Plan, User
from database.models.plan import DataLimitResetStrategy
from services.automation import expire_stale_orders
from services.orders import create_pending_order, get_open_order, user_has_successful_paid_order
from services.payments import submit_card_receipt
from services.pricing import PriceResult
from services.service_names import build_panel_username, sanitize_service_name


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed(session: AsyncSession) -> tuple[User, Plan]:
    user = User(
        telegram_id=7,
        username="c",
        first_name="C",
        last_name=None,
        language_code="fa",
        referral_code="HARD0001",
        is_admin=False,
    )
    plan = Plan(
        name="volume-10GB-30d",
        description="t",
        data_limit_gb=10,
        duration_days=30,
        price_toman=50_000,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1],
        hwid_limit=1,
        is_active=True,
    )
    session.add_all([user, plan])
    await session.flush()
    return user, plan


@pytest.mark.asyncio
async def test_get_open_order_one(session: AsyncSession) -> None:
    user, plan = await _seed(session)
    o1 = Order(
        user_id=user.id,
        plan_id=plan.id,
        status=OrderStatus.PENDING_PAYMENT,
        price_toman=50_000,
        discount_toman=0,
    )
    session.add(o1)
    await session.flush()
    open_order = await get_open_order(session, user.id)
    assert open_order is not None
    assert open_order.id == o1.id


@pytest.mark.asyncio
async def test_expire_to_expired_not_rejected(session: AsyncSession) -> None:
    user, plan = await _seed(session)
    stale = Order(
        user_id=user.id,
        plan_id=plan.id,
        status=OrderStatus.PENDING_PAYMENT,
        price_toman=50_000,
        discount_toman=0,
        created_at=datetime.now(UTC) - timedelta(minutes=60),
    )
    session.add(stale)
    await session.flush()
    expired = await expire_stale_orders(session, expiry_minutes=30)
    assert len(expired) == 1
    await session.refresh(stale)
    assert stale.status == OrderStatus.EXPIRED
    # New purchase allowed: no open order
    assert await get_open_order(session, user.id) is None


@pytest.mark.asyncio
async def test_unique_payment_idempotent(session: AsyncSession) -> None:
    user, plan = await _seed(session)
    order = Order(
        user_id=user.id,
        plan_id=plan.id,
        status=OrderStatus.PENDING_PAYMENT,
        price_toman=50_000,
        discount_toman=0,
        wallet_applied_toman=10_000,
    )
    session.add(order)
    await session.flush()
    p1 = await submit_card_receipt(
        session,
        order_id=order.id,
        user_id=user.id,
        receipt_file_id="img1",
        card_number_display="6037",
    )
    assert p1.amount_toman == 40_000
    p2 = await submit_card_receipt(
        session,
        order_id=order.id,
        user_id=user.id,
        receipt_file_id="img2",
        card_number_display="6037",
    )
    assert p1.id == p2.id


@pytest.mark.asyncio
async def test_referred_discount_first_paid_only(session: AsyncSession) -> None:
    user, plan = await _seed(session)
    assert await user_has_successful_paid_order(session, user.id) is False
    paid = Order(
        user_id=user.id,
        plan_id=plan.id,
        status=OrderStatus.ACTIVE,
        price_toman=50_000,
        discount_toman=0,
    )
    session.add(paid)
    await session.flush()
    assert await user_has_successful_paid_order(session, user.id) is True


def test_sanitize_service_name_and_panel_username() -> None:
    assert sanitize_service_name("MyVPN") == "myvpn"
    assert sanitize_service_name("my vpn!!") == "my_vpn"
    with pytest.raises(ValueError):
        sanitize_service_name("!!!")
    uname = build_panel_username("MyVPN")
    assert uname.startswith("myvpn_")
    assert "neo_" not in uname


@pytest.mark.asyncio
async def test_create_pending_with_service_name(session: AsyncSession) -> None:
    user, plan = await _seed(session)
    price = PriceResult(
        original_price=50_000,
        discount_amount=0,
        final_price=50_000,
        currency="IRT",
        meta={"volume_gb": 10, "duration_days": 30},
    )
    order = await create_pending_order(
        session,
        user_id=user.id,
        plan=plan,
        price=price,
        service_name="myvpn",
        wallet_applied_toman=5_000,
    )
    assert order.service_name == "myvpn"
    assert order.wallet_applied_toman == 5_000
    assert order.status == OrderStatus.PENDING_PAYMENT

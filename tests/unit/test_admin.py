"""Unit tests for admin panel services."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from api_client import UserResponse, UserStatus
from api_client.exceptions import PasarGuardNotFoundError
from database.base import Base
from database.models import Order, OrderStatus, Plan, User
from database.models.payment import PaymentStatus
from database.models.plan import DataLimitResetStrategy
from services.admin import (
    AdminError,
    dashboard_counts,
    get_order_detail,
    list_orders,
    list_pending_payments,
    retry_provision,
    set_user_banned,
    set_user_banned_by_telegram_id,
)
from services.payments import approve_payment, submit_card_receipt
from services.provisioning import provision_order


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed(session: AsyncSession) -> tuple[User, User, Plan]:
    customer = User(
        telegram_id=1001,
        username="cust",
        first_name="C",
        last_name=None,
        language_code="fa",
        referral_code="CUST0001",
        is_admin=False,
        is_banned=False,
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
    return customer, admin, plan


async def _make_order(
    session: AsyncSession,
    customer: User,
    plan: Plan,
    status: OrderStatus,
) -> Order:
    order = Order(
        user_id=customer.id,
        plan_id=plan.id,
        status=status,
        price_toman=150_000,
        discount_toman=0,
    )
    session.add(order)
    await session.flush()
    return order


@pytest.mark.asyncio
async def test_set_user_banned_idempotent(session: AsyncSession) -> None:
    customer, _admin, _plan = await _seed(session)
    assert customer.is_banned is False
    u1 = await set_user_banned(session, customer.id, True)
    assert u1.is_banned is True
    u2 = await set_user_banned(session, customer.id, True)
    assert u2.is_banned is True
    u3 = await set_user_banned(session, customer.id, False)
    assert u3.is_banned is False
    u4 = await set_user_banned_by_telegram_id(session, customer.telegram_id, True)
    assert u4.is_banned is True


@pytest.mark.asyncio
async def test_set_user_banned_missing(session: AsyncSession) -> None:
    await _seed(session)
    with pytest.raises(AdminError, match="user_not_found"):
        await set_user_banned(session, 99999, True)
    with pytest.raises(AdminError, match="user_not_found"):
        await set_user_banned_by_telegram_id(session, 424242, True)


@pytest.mark.asyncio
async def test_list_pending_payments(session: AsyncSession) -> None:
    customer, admin, plan = await _seed(session)
    o1 = await _make_order(session, customer, plan, OrderStatus.PENDING_PAYMENT)
    await submit_card_receipt(
        session,
        order_id=o1.id,
        user_id=customer.id,
        receipt_file_id="f1",
        card_number_display="6037",
    )
    other = User(
        telegram_id=1002,
        username="cust2",
        first_name="D",
        last_name=None,
        language_code="fa",
        referral_code="CUST0002",
        is_admin=False,
    )
    session.add(other)
    await session.flush()
    o2 = await _make_order(session, other, plan, OrderStatus.PENDING_PAYMENT)
    await submit_card_receipt(
        session,
        order_id=o2.id,
        user_id=other.id,
        receipt_file_id="f2",
        card_number_display="6037",
    )
    await _make_order(session, customer, plan, OrderStatus.ACTIVE)

    page = await list_pending_payments(session, offset=0, limit=1)
    assert page.total == 2  # noqa: PLR2004
    assert len(page.items) == 1
    assert page.has_next is True
    assert page.has_prev is False
    page2 = await list_pending_payments(session, offset=1, limit=1)
    assert len(page2.items) == 1
    assert page2.has_prev is True
    assert page2.has_next is False
    ids = {page.items[0].id, page2.items[0].id}
    assert ids == {o1.id, o2.id}


@pytest.mark.asyncio
async def test_list_orders_filter_and_detail(session: AsyncSession) -> None:
    customer, _admin, plan = await _seed(session)
    active = await _make_order(session, customer, plan, OrderStatus.ACTIVE)
    await _make_order(session, customer, plan, OrderStatus.REJECTED)
    page = await list_orders(session, status=OrderStatus.ACTIVE, offset=0, limit=10)
    assert page.total == 1
    assert page.items[0].id == active.id
    detail = await get_order_detail(session, active.id)
    assert detail.user.telegram_id == customer.telegram_id
    with pytest.raises(AdminError, match="order_not_found"):
        await get_order_detail(session, 99999)


@pytest.mark.asyncio
async def test_dashboard_counts(session: AsyncSession) -> None:
    customer, admin, plan = await _seed(session)
    customer.is_banned = True
    await _make_order(session, customer, plan, OrderStatus.ACTIVE)
    o = await _make_order(session, customer, plan, OrderStatus.PENDING_PAYMENT)
    await submit_card_receipt(
        session,
        order_id=o.id,
        user_id=customer.id,
        receipt_file_id="f",
        card_number_display="1",
    )
    counts = await dashboard_counts(session)
    assert counts.users == 2  # noqa: PLR2004
    assert counts.banned_users == 1
    assert counts.pending_payments == 1
    assert counts.orders_by_status.get(OrderStatus.ACTIVE.value) == 1
    assert counts.orders_by_status.get(OrderStatus.PAYMENT_REVIEW.value) == 1


@pytest.mark.asyncio
async def test_retry_provision_success(session: AsyncSession) -> None:
    customer, admin, plan = await _seed(session)
    order = await _make_order(session, customer, plan, OrderStatus.PENDING_PAYMENT)
    await submit_card_receipt(
        session,
        order_id=order.id,
        user_id=customer.id,
        receipt_file_id="file",
        card_number_display="6037",
    )
    await approve_payment(session, order_id=order.id, admin_user_id=admin.id)
    order.status = OrderStatus.PROVISION_FAILED
    await session.flush()

    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    created = UserResponse(
        id=42,
        username=f"neo_{order.id}",
        status=UserStatus.ACTIVE,
        used_traffic=0,
        created_at=datetime.now(UTC),
        subscription_url=f"https://panel.example.com/sub/neo_{order.id}",
        data_limit=10 * 1024**3,
    )
    client.create_user = AsyncMock(return_value=created)
    client.subscription_url_from_response = lambda u: u.subscription_url

    account, _reward = await retry_provision(session, client, order_id=order.id)
    await session.refresh(order)
    assert order.status == OrderStatus.ACTIVE
    assert account.panel_username == f"neo_{order.id}"
    client.create_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_provision_invalid_status(session: AsyncSession) -> None:
    customer, _admin, plan = await _seed(session)
    order = await _make_order(session, customer, plan, OrderStatus.PENDING_PAYMENT)
    client = AsyncMock()
    with pytest.raises(AdminError, match="invalid_status"):
        await retry_provision(session, client, order_id=order.id)


@pytest.mark.asyncio
async def test_retry_provision_active_noop(session: AsyncSession) -> None:
    """ACTIVE order: retry_provision delegates to provision_order which returns account."""
    customer, admin, plan = await _seed(session)
    order = await _make_order(session, customer, plan, OrderStatus.PENDING_PAYMENT)
    await submit_card_receipt(
        session,
        order_id=order.id,
        user_id=customer.id,
        receipt_file_id="file",
        card_number_display="6037",
    )
    await approve_payment(session, order_id=order.id, admin_user_id=admin.id)

    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    created = UserResponse(
        id=7,
        username=f"neo_{order.id}",
        status=UserStatus.ACTIVE,
        used_traffic=0,
        created_at=datetime.now(UTC),
        subscription_url=f"https://panel.example.com/sub/neo_{order.id}",
        data_limit=10 * 1024**3,
    )
    client.create_user = AsyncMock(return_value=created)
    client.subscription_url_from_response = lambda u: u.subscription_url

    await provision_order(
        session, client, order_id=order.id, default_group_ids=[1]
    )  # returns (account, reward)
    await session.refresh(order)
    assert order.status == OrderStatus.ACTIVE

    # Second retry should be idempotent (no new create)
    client.create_user.reset_mock()
    account, _reward = await retry_provision(session, client, order_id=order.id)
    assert account.panel_username == f"neo_{order.id}"
    client.create_user.assert_not_awaited()


def test_order_detail_keyboard_includes_receipt_button() -> None:
    from types import SimpleNamespace

    from bot.keyboards.admin import AdminCB, order_detail_keyboard
    from bot.keyboards.admin_payment import PayCB
    from database.models import OrderStatus

    payment = SimpleNamespace(receipt_file_id="AgAC_receipt_1")
    user = SimpleNamespace(id=7, is_banned=False)
    order = SimpleNamespace(id=42, status=OrderStatus.PAYMENT_REVIEW, payment=payment, user=user)
    kb = order_detail_keyboard(order)
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert f"{PayCB.APPROVE_PREFIX}42" in callbacks
    assert f"{PayCB.REJECT_PREFIX}42" in callbacks
    assert f"{AdminCB.SHOW_RECEIPT}42" in callbacks

    no_receipt = SimpleNamespace(
        id=43,
        status=OrderStatus.PAYMENT_REVIEW,
        payment=SimpleNamespace(receipt_file_id=None),
        user=user,
    )
    callbacks2 = [
        btn.callback_data
        for row in order_detail_keyboard(no_receipt).inline_keyboard
        for btn in row
    ]
    assert not any((cb or "").startswith(AdminCB.SHOW_RECEIPT) for cb in callbacks2)


@pytest.mark.asyncio
async def test_deliver_order_detail_sends_receipt_photo() -> None:
    """Admin order detail prefers answer_photo when receipt_file_id is set."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from bot.handlers.admin import _deliver_order_detail
    from database.models import OrderStatus

    order = SimpleNamespace(
        id=42,
        status=OrderStatus.PAYMENT_REVIEW,
        price_toman=150_000,
        panel_username=None,
        user=SimpleNamespace(
            id=1, telegram_id=1001, username="cust", first_name="C", is_banned=False
        ),
        plan=SimpleNamespace(name="volume-10GB-30d"),
        payment=SimpleNamespace(
            status=PaymentStatus.pending,
            receipt_file_id="AgAC_test_receipt",
            receipt_media_type="photo",
        ),
    )

    message = MagicMock()
    message.answer_photo = AsyncMock()
    message.edit_text = AsyncMock()
    message.answer = AsyncMock()

    await _deliver_order_detail(message, order, prefer_edit=True)  # type: ignore[arg-type]

    message.answer_photo.assert_awaited_once()
    kwargs = message.answer_photo.await_args.kwargs
    assert kwargs["photo"] == "AgAC_test_receipt"
    assert "caption" in kwargs
    assert kwargs["reply_markup"] is not None
    message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_deliver_order_detail_text_when_no_receipt() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from bot.handlers.admin import _deliver_order_detail
    from database.models import OrderStatus

    order = SimpleNamespace(
        id=43,
        status=OrderStatus.ACTIVE,
        price_toman=150_000,
        panel_username="neo_1",
        user=SimpleNamespace(
            id=1, telegram_id=1001, username="cust", first_name="C", is_banned=False
        ),
        plan=SimpleNamespace(name="volume-10GB-30d"),
        payment=None,
    )

    message = MagicMock()
    message.answer_photo = AsyncMock()
    message.edit_text = AsyncMock()
    message.answer = AsyncMock()

    await _deliver_order_detail(message, order, prefer_edit=True)  # type: ignore[arg-type]

    message.answer_photo.assert_not_awaited()
    message.edit_text.assert_awaited_once()

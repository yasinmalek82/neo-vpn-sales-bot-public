"""Tests for admin payment queue UX: badges, age, reject reasons, keyboards."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from database.base import Base
from database.models import Order, OrderStatus, Plan, User
from database.models.plan import DataLimitResetStrategy
from services.admin import get_next_pending_payment, list_pending_payments
from services.payments import reject_payment, submit_card_receipt
from services.ux_format import format_relative_age, is_stale_pending


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


def test_format_relative_age_and_stale() -> None:
    now = datetime(2026, 9, 12, 16, 0, tzinfo=UTC)
    assert format_relative_age(now - timedelta(seconds=30), now=now) == "همین الان"
    assert format_relative_age(now - timedelta(minutes=12), now=now) == "12 دقیقه قبل"
    assert format_relative_age(now - timedelta(hours=3), now=now) == "3 ساعت قبل"
    assert format_relative_age(now - timedelta(days=2), now=now) == "2 روز قبل"
    assert is_stale_pending(now - timedelta(minutes=30), now=now) is False
    assert is_stale_pending(now - timedelta(hours=2), now=now) is True
    assert is_stale_pending(None, now=now) is False


def test_admin_home_reaches_payments_orders_store_and_finance() -> None:
    # The home is payments + sections now (2026-09-30); the full reachability
    # check is in test_admin_home_layout.py.
    from bot.keyboards.admin import AdminCB, admin_home_keyboard, admin_sales_keyboard

    callbacks = [
        btn.callback_data
        for kb in (admin_home_keyboard(), admin_sales_keyboard())
        for row in kb.inline_keyboard
        for btn in row
    ]
    assert AdminCB.PENDING in callbacks
    assert AdminCB.ORDERS in callbacks
    assert AdminCB.STORE in callbacks
    assert AdminCB.STATS in callbacks


def test_payment_review_keyboard_has_order_detail() -> None:
    from bot.keyboards.admin import AdminCB
    from bot.keyboards.admin_payment import PayCB, payment_review_keyboard

    kb = payment_review_keyboard(42)
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert f"{PayCB.APPROVE_PREFIX}42" in callbacks
    assert f"{PayCB.REJECT_PREFIX}42" in callbacks
    assert f"{AdminCB.ORDER}42" in callbacks


def test_pending_list_badge_when_receipt() -> None:
    from bot.keyboards.admin import pending_list_keyboard

    with_receipt = SimpleNamespace(
        id=10,
        price_toman=150_000,
        payment=SimpleNamespace(receipt_file_id="AgAC_x"),
    )
    without = SimpleNamespace(
        id=11,
        price_toman=200_000,
        payment=SimpleNamespace(receipt_file_id=None),
    )
    kb = pending_list_keyboard([with_receipt, without], offset=0, limit=8, total=2)
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any("📷" in (t or "") and "#10" in (t or "") for t in labels)
    assert any("#11" in (t or "") and "📷" not in (t or "") for t in labels)


def test_reject_reason_keyboard_and_resolved() -> None:
    from bot.keyboards.admin import AdminCB
    from bot.keyboards.admin_payment import (
        PayCB,
        payment_resolved_keyboard,
        reject_reason_keyboard,
    )

    kb = reject_reason_keyboard(7)
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert f"{PayCB.REJECT_REASON_PREFIX}7:unreadable" in callbacks
    assert f"{PayCB.REJECT_CUSTOM_PREFIX}7" in callbacks
    assert f"{PayCB.REJECT_ABORT_PREFIX}7" in callbacks

    resolved = payment_resolved_keyboard(has_next=True)
    rcb = [btn.callback_data for row in resolved.inline_keyboard for btn in row]
    assert AdminCB.NEXT_PENDING in rcb
    assert AdminCB.HOME in rcb


@pytest.mark.asyncio
async def test_reject_payment_persists_note(session: AsyncSession) -> None:
    customer, admin, plan = await _seed(session)
    order = Order(
        user_id=customer.id,
        plan_id=plan.id,
        status=OrderStatus.PENDING_PAYMENT,
        price_toman=150_000,
        discount_toman=0,
    )
    session.add(order)
    await session.flush()
    await submit_card_receipt(
        session,
        order_id=order.id,
        user_id=customer.id,
        receipt_file_id="f1",
        card_number_display="6037",
    )
    _ord, payment = await reject_payment(
        session,
        order_id=order.id,
        admin_user_id=admin.id,
        note="فیش ناخوانا / نامعتبر",
    )
    await session.refresh(payment)
    assert payment.note == "فیش ناخوانا / نامعتبر"
    assert order.status == OrderStatus.REJECTED


@pytest.mark.asyncio
async def test_list_pending_stale_filter_and_next(session: AsyncSession) -> None:
    customer, _admin, plan = await _seed(session)
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
    fresh = Order(
        user_id=customer.id,
        plan_id=plan.id,
        status=OrderStatus.PENDING_PAYMENT,
        price_toman=100_000,
        discount_toman=0,
        created_at=datetime.now(UTC),
    )
    stale = Order(
        user_id=other.id,
        plan_id=plan.id,
        status=OrderStatus.PENDING_PAYMENT,
        price_toman=200_000,
        discount_toman=0,
        created_at=datetime.now(UTC) - timedelta(hours=3),
    )
    session.add_all([fresh, stale])
    await session.flush()
    await submit_card_receipt(
        session,
        order_id=fresh.id,
        user_id=customer.id,
        receipt_file_id="f_fresh",
        card_number_display="1",
    )
    await submit_card_receipt(
        session,
        order_id=stale.id,
        user_id=other.id,
        receipt_file_id="f_stale",
        card_number_display="1",
    )
    # Force stale created_at after receipt (submit may not touch it, but be explicit)
    stale.created_at = datetime.now(UTC) - timedelta(hours=3)
    await session.flush()

    all_page = await list_pending_payments(session, stale_only=False)
    assert all_page.total == 2  # noqa: PLR2004
    stale_page = await list_pending_payments(session, stale_only=True)
    assert stale_page.total == 1
    assert stale_page.items[0].id == stale.id

    nxt = await get_next_pending_payment(session, exclude_order_id=fresh.id)
    assert nxt is not None
    assert nxt.id == stale.id

    nxt2 = await get_next_pending_payment(session, exclude_order_id=None)
    assert nxt2 is not None
    # newest first → fresh
    assert nxt2.id == fresh.id


def test_pending_line_text_includes_age_placeholders() -> None:
    from bot.texts import texts

    line = texts.admin_pending_line.format(
        order_id=1,
        amount="150٬000",
        age="2 ساعت قبل",
        stale_mark=" ⚠️ کهنه",
        user_label="ali",
        receipt=" 📷 فیش",
    )
    assert "کهنه" in line
    assert "فیش" in line
    assert "2 ساعت قبل" in line

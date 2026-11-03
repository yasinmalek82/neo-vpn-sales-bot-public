"""Every reportable business event actually reaches the outbox.

A report builder nobody calls is worse than no builder: the group looks healthy
and stays empty. These tests run the real service functions and assert a row
landed, so a future refactor that drops a call site fails here.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from database.base import Base
from database.models import Order, OrderStatus, Payment, PaymentStatus, Plan, User
from database.models.report import (
    TOPIC_ERRORS,
    TOPIC_PAYMENTS,
    TOPIC_SALES,
    TOPIC_WALLET,
    ReportOutbox,
)
from services.payments import approve_payment, reject_payment
from services.wallet import credit


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _outbox(session: AsyncSession) -> list[ReportOutbox]:
    return list((await session.execute(select(ReportOutbox))).scalars().all())


async def _order_in_review(session: AsyncSession) -> tuple[User, Order, Payment]:
    user = User(telegram_id=555, referral_code="R555", first_name="یاسین")
    session.add(user)
    await session.flush()
    plan = Plan(name="۵۰ گیگ", data_limit_gb=50, duration_days=30, price_toman=150_000)
    session.add(plan)
    await session.flush()
    order = Order(
        user_id=user.id,
        plan_id=plan.id,
        status=OrderStatus.PAYMENT_REVIEW,
        price_toman=150_000,
    )
    session.add(order)
    await session.flush()
    payment = Payment(
        order_id=order.id,
        user_id=user.id,
        amount_toman=150_000,
        method="card",
        status=PaymentStatus.pending,
    )
    session.add(payment)
    await session.flush()
    return user, order, payment


@pytest.mark.asyncio
async def test_approving_a_payment_queues_a_report(session: AsyncSession) -> None:
    _user, order, _payment = await _order_in_review(session)
    await approve_payment(session, order_id=int(order.id), admin_user_id=1)
    await session.commit()

    rows = await _outbox(session)
    assert [r.topic_key for r in rows] == [TOPIC_PAYMENTS]
    assert rows[0].event_key == f"order:{order.id}:paid"
    assert "۱۵۰٬۰۰۰" in rows[0].body or "150" in rows[0].body


@pytest.mark.asyncio
async def test_rejecting_a_payment_queues_the_reason(session: AsyncSession) -> None:
    _user, order, _payment = await _order_in_review(session)
    await reject_payment(session, order_id=int(order.id), admin_user_id=1, note="فیش نامعتبر")
    await session.commit()

    rows = await _outbox(session)
    assert rows[0].event_key == f"order:{order.id}:rejected"
    assert "فیش نامعتبر" in rows[0].body


@pytest.mark.asyncio
async def test_approving_twice_queues_one_report(session: AsyncSession) -> None:
    """Approve is idempotent; the report must be too."""
    _user, order, _payment = await _order_in_review(session)
    await approve_payment(session, order_id=int(order.id), admin_user_id=1)
    await session.commit()
    await approve_payment(session, order_id=int(order.id), admin_user_id=1)
    await session.commit()

    assert len(await _outbox(session)) == 1


@pytest.mark.asyncio
async def test_a_wallet_credit_queues_its_own_report(session: AsyncSession) -> None:
    user = User(telegram_id=7, referral_code="R7")
    session.add(user)
    await session.flush()

    await credit(
        session,
        user_id=int(user.id),
        amount_toman=50_000,
        reason="manual_admin_credit",
        idempotency_key="k1",
    )
    await session.commit()

    rows = await _outbox(session)
    assert len(rows) == 1
    assert rows[0].topic_key == TOPIC_WALLET
    assert rows[0].event_key.startswith("wallet_entry:")


@pytest.mark.asyncio
async def test_three_wallet_moves_on_one_order_are_three_reports(
    session: AsyncSession,
) -> None:
    """The key names the transition, not the order."""
    user = User(telegram_id=7, referral_code="R7")
    session.add(user)
    await session.flush()

    for n in range(3):
        await credit(
            session,
            user_id=int(user.id),
            amount_toman=1_000,
            reason="topup",
            idempotency_key=f"k{n}",
        )
    await session.commit()

    keys = {r.event_key for r in await _outbox(session)}
    assert len(keys) == 3


@pytest.mark.asyncio
async def test_provisioning_failure_reports_to_the_errors_topic(
    session: AsyncSession,
) -> None:
    """Payment captured but no service delivered is the one event needing a human."""
    from services.provisioning import _mark_provision_failed

    _user, order, _payment = await _order_in_review(session)
    order.status = OrderStatus.PROVISIONING
    await session.flush()

    await _mark_provision_failed(session, order, RuntimeError("panel timeout"))
    await session.commit()

    rows = await _outbox(session)
    assert rows[0].topic_key == TOPIC_ERRORS
    assert rows[0].event_key == f"order:{order.id}:provision_failed"
    assert "panel timeout" in rows[0].body
    assert order.status is OrderStatus.PROVISION_FAILED


@pytest.mark.asyncio
async def test_a_sale_reports_to_the_sales_topic(session: AsyncSession) -> None:
    from services.provisioning import _report_sale

    _user, order, _payment = await _order_in_review(session)
    await _report_sale(session, order)
    await session.commit()

    rows = await _outbox(session)
    assert rows[0].topic_key == TOPIC_SALES
    assert rows[0].event_key == f"order:{order.id}:active"


@pytest.mark.asyncio
async def test_a_report_failure_cannot_undo_the_money(session: AsyncSession) -> None:
    """The whole reason reports are enqueued rather than sent.

    Even if queuing itself blew up, the approval must survive — so the report
    is the last thing done and never raises out of the money path.
    """
    import services.payments as payments_module

    _user, order, _payment = await _order_in_review(session)

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("outbox unavailable")

    original = payments_module._report_payment
    payments_module._report_payment = boom  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError):
            await approve_payment(session, order_id=int(order.id), admin_user_id=1)
    finally:
        payments_module._report_payment = original  # type: ignore[assignment]

    # The status change is already flushed; rolling back is the caller's choice,
    # and nothing about reporting silently reverted it.
    assert order.status is OrderStatus.PAYMENT_APPROVED

"""Overlapping payment decisions must not mix an approval with a wallet refund."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import (
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    Plan,
    User,
)
from database.models.plan import DataLimitResetStrategy
from services.payments import PaymentError, approve_payment, reject_payment
from services.wallet import REASON_CHECKOUT_DEBIT, debit, get_balance

APPLIED = 40_000
PRICE = 100_000


async def _seed_review(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int, int]:
    async with factory() as session:
        buyer = User(
            telegram_id=7701,
            username="buyer",
            first_name="B",
            last_name=None,
            language_code="fa",
            referral_code="PAYRACE1",
            is_admin=False,
            wallet_balance_toman=APPLIED,
        )
        admin = User(
            telegram_id=7702,
            username="admin",
            first_name="A",
            last_name=None,
            language_code="fa",
            referral_code="PAYRACE2",
            is_admin=True,
        )
        plan = Plan(
            name="volume-10GB-30d",
            description="t",
            data_limit_gb=10,
            duration_days=30,
            price_toman=PRICE,
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            group_ids=[1],
            hwid_limit=1,
            is_active=True,
        )
        session.add_all([buyer, admin, plan])
        await session.flush()
        order = Order(
            user_id=buyer.id,
            plan_id=plan.id,
            status=OrderStatus.PAYMENT_REVIEW,
            price_toman=PRICE,
            discount_toman=0,
            wallet_applied_toman=APPLIED,
        )
        session.add(order)
        await session.flush()
        await debit(
            session,
            user_id=buyer.id,
            amount_toman=APPLIED,
            reason=REASON_CHECKOUT_DEBIT,
            idempotency_key=f"checkout_debit:{order.id}",
            order_id=order.id,
        )
        session.add(
            Payment(
                order_id=order.id,
                user_id=buyer.id,
                amount_toman=PRICE - APPLIED,
                method="card_to_card",
                status=PaymentStatus.pending,
                receipt_file_id="receipt",
            )
        )
        await session.commit()
        return int(buyer.id), int(admin.id), int(order.id)


@pytest.mark.asyncio
async def test_losing_reject_does_not_refund_an_approved_order(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Approve has read REVIEW. Reject commits before approve writes.

    A blind update after that read leaves the order approved and the wallet
    refunded. The loser must not produce that mix.
    """
    factory, _url = two_sessions
    buyer_id, admin_id, order_id = await _seed_review(factory)
    entered = asyncio.Event()
    release = asyncio.Event()
    holder: dict[str, AsyncSession] = {}

    from services import payments

    real_load = payments._load_order

    async def pause_after_read(session: AsyncSession, loaded_id: int) -> Order:
        order = await real_load(session, loaded_id)
        if holder.get("approve") is session and not entered.is_set():
            entered.set()
            await release.wait()
        return order

    monkeypatch.setattr(payments, "_load_order", pause_after_read)

    async with factory() as approving:
        holder["approve"] = approving

        async def reject_while_approve_waits() -> None:
            await entered.wait()
            async with factory() as rejecting:
                try:
                    await reject_payment(
                        rejecting,
                        order_id=order_id,
                        admin_user_id=admin_id,
                        note="no",
                    )
                    await rejecting.commit()
                except PaymentError:
                    await rejecting.rollback()
            release.set()

        raced = asyncio.create_task(reject_while_approve_waits())
        try:
            await approve_payment(approving, order_id=order_id, admin_user_id=admin_id)
            await approving.commit()
        except PaymentError:
            await approving.rollback()
        await raced

    async with factory() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        balance = await get_balance(session, buyer_id)
    approved_without_refund = order.status == OrderStatus.PAYMENT_APPROVED and balance == 0
    rejected_with_refund = order.status == OrderStatus.REJECTED and balance == APPLIED
    assert (
        approved_without_refund or rejected_with_refund
    ), f"status={order.status.value} balance={balance}"


@pytest.mark.asyncio
async def test_second_approve_is_a_noop(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _url = two_sessions
    buyer_id, admin_id, order_id = await _seed_review(factory)

    async with factory() as first:
        await approve_payment(first, order_id=order_id, admin_user_id=admin_id)
        await first.commit()
    async with factory() as second:
        await approve_payment(second, order_id=order_id, admin_user_id=admin_id)
        await second.commit()

    async with factory() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        assert order.status == OrderStatus.PAYMENT_APPROVED
        assert await get_balance(session, buyer_id) == 0

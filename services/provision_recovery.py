"""Bounded recovery of already-paid intents; never initiates debit or refund."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api_client.factory import get_pasarguard_client
from bot.config import Settings
from database.models import Order, OrderStatus
from database.models.provision_recovery import ProvisionRecovery
from database.provision_lock import ProvisionBusy, provision_ownership
from services.provisioning import provision_order
from services.report_events import provision_pending
from services.report_outbox import enqueue_report

logger = logging.getLogger(__name__)
PENDING = (OrderStatus.PAYMENT_APPROVED, OrderStatus.PROVISIONING)
MIN_AGE = timedelta(minutes=1)
ALERT_AGE = timedelta(minutes=15)


async def _alert(session: AsyncSession, order: Order, now: datetime) -> None:
    since = order.paid_at or order.created_at
    since = since.replace(tzinfo=since.tzinfo or UTC)
    if now - since < ALERT_AGE:
        return
    report = provision_pending(order_id=order.id, user_id=order.user_id)
    await enqueue_report(
        session,
        event_key=report.event_key,
        topic_key=report.topic_key,
        body=report.body,
        link_user_id=report.link_user_id,
    )


async def recover_pending_provisions(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings, *, limit: int = 5
) -> tuple[int, int]:
    now = datetime.now(UTC)
    query = (
        select(Order.id)
        .outerjoin(ProvisionRecovery, ProvisionRecovery.order_id == Order.id)
        .where(
            Order.status.in_(PENDING),
            Order.updated_at <= now - MIN_AGE,
            or_(ProvisionRecovery.order_id.is_(None), ProvisionRecovery.next_attempt_at <= now),
        )
        .order_by(ProvisionRecovery.next_attempt_at.asc().nulls_first(), Order.updated_at, Order.id)
        .limit(limit)
    )
    async with session_factory() as session:
        ids = list((await session.scalars(query)).all())
    recovered = failed = 0
    for order_id in ids:
        async with session_factory() as session:
            try:
                async with provision_ownership(session, order_id):
                    # Recheck status/backoff after acquiring ownership; another
                    # worker or an admin may have completed the selected order.
                    if await session.scalar(query.where(Order.id == order_id)) is None:
                        continue
                    order = await session.get(Order, order_id)
                    assert order is not None
                    retry = await session.get(ProvisionRecovery, order_id)
                    if retry is None:
                        retry = ProvisionRecovery(order_id=order_id, attempts=0)
                        session.add(retry)
                    retry.attempts += 1
                    retry.next_attempt_at = datetime.now(UTC) + timedelta(
                        seconds=min(1800, 60 * 2 ** min(retry.attempts - 1, 5))
                    )
                    # Persist retry scheduling before IO; a crash is bounded too.
                    await session.commit()
                    try:
                        async with asyncio.timeout(120):
                            client = await get_pasarguard_client(session, settings)
                            try:
                                await provision_order(session, client, order_id=order_id)
                            finally:
                                await client.aclose()
                        retry.last_error = None
                        await session.commit()
                        recovered += 1
                    except Exception as exc:
                        # Roll back local failed markers, never a possibly
                        # successful remote write. Preserve the paid intent.
                        await session.rollback()
                        order = await session.get(Order, order_id)
                        retry = await session.get(ProvisionRecovery, order_id)
                        assert retry is not None
                        retry.last_error = type(exc).__name__[:64]
                        if order is not None and order.status in PENDING:
                            await _alert(session, order, datetime.now(UTC))
                        await session.commit()
                        failed += 1
                        logger.warning(
                            "Provision recovery deferred order=%s error_type=%s",
                            order_id,
                            type(exc).__name__,
                        )
            except ProvisionBusy:
                continue
    return recovered, failed

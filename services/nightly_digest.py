"""The Tehran day that just ended, as one message on the system topic.

The finance screen's «امروز» is midnight UTC. This one is midnight Tehran,
because that is the day the owner means when the digest arrives at 00:05.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import jdatetime
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Order, Plan, User
from database.models.order import TRIAL_KIND_PERSONAL, TRIAL_KIND_RESELLER
from services.disruption import disruption_counts
from services.report_events import DigestLeader, Report, nightly_digest
from services.reporting import EARNING_STATUSES
from services.ux_format import to_persian_digits

_TEHRAN = ZoneInfo("Asia/Tehran")


def previous_tehran_day(now: datetime) -> tuple[datetime, datetime, str, str]:
    """Start, end (UTC), ``YYYY-MM-DD`` key, and a Persian date label.

    ``now`` at 00:05 Tehran on the 23rd summarises the 22nd. A sale at 00:10
    on the 23rd belongs to the next digest, not this one.
    """
    moment = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    local = moment.astimezone(_TEHRAN)
    end_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = end_local - timedelta(days=1)
    jalali = jdatetime.datetime.fromgregorian(datetime=start_local.replace(tzinfo=None))
    key = f"{jalali.year:04d}-{jalali.month:02d}-{jalali.day:02d}"
    label = to_persian_digits(jalali.strftime("%Y/%m/%d"))
    return start_local.astimezone(UTC), end_local.astimezone(UTC), key, label


async def build_nightly_digest(session: AsyncSession, *, now: datetime | None = None) -> Report:
    moment = now or datetime.now(UTC)
    start, end, key, label = previous_tehran_day(moment)
    paid: tuple[ColumnElement[bool], ...] = (
        Order.status.in_(EARNING_STATUSES),
        Order.price_toman > 0,
        Order.activated_at.is_not(None),
        Order.activated_at >= start,
        Order.activated_at < end,
    )
    fresh = await _sum(session, *paid, Order.renew_account_id.is_(None))
    renewed = await _sum(session, *paid, Order.renew_account_id.is_not(None))
    trials = int(
        await session.scalar(
            select(func.count())
            .select_from(Order)
            .where(
                Order.trial_kind.in_((TRIAL_KIND_PERSONAL, TRIAL_KIND_RESELLER)),
                Order.activated_at.is_not(None),
                Order.activated_at >= start,
                Order.activated_at < end,
            )
        )
        or 0
    )
    volume = int(
        await session.scalar(
            select(
                func.coalesce(
                    func.sum(func.coalesce(Order.delivery_data_limit_gb, Plan.data_limit_gb)),
                    0,
                )
            )
            .select_from(Order)
            .join(Plan, Plan.id == Order.plan_id)
            .where(*paid)
        )
        or 0
    )
    newcomers = int(
        await session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.created_at >= start, User.created_at < end)
        )
        or 0
    )
    return nightly_digest(
        jalali_key=key,
        day_label=label,
        fresh_count=fresh[0],
        fresh_toman=fresh[1],
        renew_count=renewed[0],
        renew_toman=renewed[1],
        trials=trials,
        volume_gb=volume,
        new_users=newcomers,
        leaders=await _leaders(session, start, end),
        disruptions=await disruption_counts(session, start=start, end=end),
    )


async def _sum(session: AsyncSession, *where: ColumnElement[bool]) -> tuple[int, int]:
    row = (
        await session.execute(
            select(func.count(Order.id), func.coalesce(func.sum(Order.price_toman), 0)).where(
                *where
            )
        )
    ).one()
    return int(row[0] or 0), int(row[1] or 0)


async def _leaders(session: AsyncSession, start: datetime, end: datetime) -> list[DigestLeader]:
    rows = (
        await session.execute(
            select(
                User.username,
                User.first_name,
                User.is_reseller,
                func.coalesce(func.sum(Order.price_toman), 0).label("spent"),
            )
            .join(Order, Order.user_id == User.id)
            .where(
                Order.status.in_(EARNING_STATUSES),
                Order.price_toman > 0,
                Order.activated_at.is_not(None),
                Order.activated_at >= start,
                Order.activated_at < end,
            )
            .group_by(User.id)
            .order_by(func.sum(Order.price_toman).desc(), User.id.asc())
            .limit(5)
        )
    ).all()
    return [
        DigestLeader(
            username=row.username,
            name=row.first_name,
            is_reseller=bool(row.is_reseller),
            amount_toman=int(row.spent),
        )
        for row in rows
    ]

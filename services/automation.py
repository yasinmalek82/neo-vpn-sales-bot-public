"""Scheduler-facing automation: expire unpaid orders + renewal reminders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import Order, OrderStatus, User
from database.models.order import TRIAL_KIND_PERSONAL
from services.account_orders import trial_account_ids
from services.finance import refund_checkout_wallet_portion
from services.order_transitions import assert_transition


@dataclass(frozen=True)
class ReminderCandidate:
    order: Order
    user: User
    days_left: int


async def expire_stale_orders(
    session: AsyncSession,
    *,
    expiry_minutes: int,
    now: datetime | None = None,
) -> list[Order]:
    """Mark stale PENDING_PAYMENT orders as EXPIRED. Idempotent.

    PAYMENT_REVIEW is intentionally not auto-expired (receipt awaits admin review).
    """
    if expiry_minutes <= 0:
        return []
    clock = now or datetime.now(UTC)
    cutoff = clock - timedelta(minutes=expiry_minutes)
    result = await session.execute(
        select(Order).where(
            Order.status == OrderStatus.PENDING_PAYMENT,
            Order.created_at <= cutoff,
        )
    )
    expired: list[Order] = []
    for order in result.scalars().all():
        assert_transition(order.status, OrderStatus.EXPIRED)
        claimed = await session.execute(
            update(Order)
            .where(Order.id == order.id, Order.status == OrderStatus.PENDING_PAYMENT)
            .values(status=OrderStatus.EXPIRED, updated_at=clock)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            continue
        await session.refresh(order)
        # Combo-pay: refund wallet portion exactly + idempotently in same txn.
        await refund_checkout_wallet_portion(session, order=order)
        expired.append(order)
    if expired:
        await session.flush()
    return expired


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _threshold_for(delta: timedelta, days: list[int]) -> int | None:
    """The configured threshold (in days) a remaining time falls into, if any.

    Day ``d`` covers ``(d-1, d]`` days left; ``0`` also covers the last 12 hours.
    """
    for d in days:
        lower = timedelta(days=d - 1) if d > 0 else timedelta(0)
        upper = timedelta(days=d)
        if lower < delta <= upper or (d == 0 and delta <= timedelta(hours=12)):
            return d
    days_left = int(delta.total_seconds() // 86400)
    return days_left if days_left in days else None


async def find_renewal_reminder_candidates(
    session: AsyncSession,
    *,
    reminder_days: list[int],
    now: datetime | None = None,
) -> list[ReminderCandidate]:
    """ACTIVE orders whose expires_at falls within a reminder day window.

    One reminder per threshold per order. ``orders.last_reminder_at`` is the
    send time; the threshold it was sent for is recomputed from it
    (``expires_at - last_reminder_at``), so no extra column is needed and a
    reminder is skipped while the order is still in that same window, whatever
    the UTC date. Banned users are skipped, and only the latest ACTIVE order of
    an account (``panel_username``) is reminded: an older order of a renewed
    account would otherwise send a second, wrong «expires in 3 days».
    """
    if not reminder_days:
        return []
    clock = now or datetime.now(UTC)
    days = sorted({int(d) for d in reminder_days if int(d) >= 0})
    if not days:
        return []

    max_days = max(days)
    window_end = clock + timedelta(days=max_days + 1)
    result = await session.execute(
        select(Order)
        .join(User, User.id == Order.user_id)
        .where(
            Order.status == OrderStatus.ACTIVE,
            Order.expires_at.is_not(None),
            Order.expires_at > clock,
            Order.expires_at <= window_end,
            User.is_banned.is_(False),
        )
        .options(selectinload(Order.user))
    )
    candidates: list[ReminderCandidate] = []
    for order in result.scalars().all():
        if order.expires_at is None:
            continue
        expires_at = _utc(order.expires_at)
        matched = _threshold_for(expires_at - clock, days)
        if matched is None:
            continue
        if order.last_reminder_at is not None:
            sent_for = _threshold_for(expires_at - _utc(order.last_reminder_at), days)
            if sent_for == matched:
                continue
        if order.user is None:
            continue
        candidates.append(ReminderCandidate(order=order, user=order.user, days_left=matched))

    usernames = {c.order.panel_username for c in candidates if c.order.panel_username}
    if usernames:
        rows = await session.execute(
            select(Order.panel_username, func.max(Order.id))
            .where(Order.status == OrderStatus.ACTIVE, Order.panel_username.in_(usernames))
            .group_by(Order.panel_username)
        )
        latest: dict[str | None, int] = {name: int(top) for name, top in rows.all()}
        candidates = [
            c
            for c in candidates
            if not c.order.panel_username or latest.get(c.order.panel_username) == c.order.id
        ]
    # A free trial cannot be renewed: it never gets the renewal reminder. It has its own
    # message when it is about to end (`find_trial_ending_candidates`).
    trials = await trial_account_ids(
        session, {c.order.panel_username for c in candidates if c.order.panel_username}
    )
    return [c for c in candidates if c.order.panel_username not in trials]


@dataclass(frozen=True)
class TrialEndingCandidate:
    user: User
    account_id: int
    panel_username: str


async def find_trial_ending_candidates(
    session: AsyncSession, *, now: datetime | None = None
) -> list[TrialEndingCandidate]:
    """Personal trials that end within the next 24 hours, for their one «تست تموم می‌شه» message.

    A reseller's trial (``TRIAL_KIND_RESELLER``) is never listed: the reseller is not the one
    using it. Banned users are skipped. Idempotency is the caller's, by ``service_notices``.
    """
    clock = now or datetime.now(UTC)
    rows = await session.execute(
        select(Order)
        .join(User, User.id == Order.user_id)
        .where(
            Order.status == OrderStatus.ACTIVE,
            Order.trial_kind == TRIAL_KIND_PERSONAL,
            Order.expires_at.is_not(None),
            Order.expires_at > clock,
            Order.expires_at <= clock + timedelta(hours=24),
            Order.panel_username.is_not(None),
            User.is_banned.is_(False),
        )
        .options(selectinload(Order.user))
    )
    orders = [o for o in rows.scalars().all() if o.user is not None]
    trials = await trial_account_ids(session, {str(o.panel_username) for o in orders})
    out: list[TrialEndingCandidate] = []
    seen: set[int] = set()
    for order in orders:
        account_id = trials.get(str(order.panel_username))
        if account_id is None or account_id in seen:
            continue
        seen.add(account_id)
        out.append(TrialEndingCandidate(order.user, account_id, str(order.panel_username)))
    return out


async def mark_reminder_sent(
    session: AsyncSession, *, order_id: int, now: datetime | None = None
) -> None:
    result = await session.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if order is None:
        return
    order.last_reminder_at = now or datetime.now(UTC)
    await session.flush()

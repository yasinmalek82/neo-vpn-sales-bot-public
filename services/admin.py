"""Admin panel services — queues, lookup, ban, retry provision, DB stats."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api_client import PasarGuardClient
from database.models import Order, OrderStatus, PasarGuardAccount, ReferralLog, User
from services.orders import effective_delivery_duration_days
from services.provisioning import ProvisionError, provision_order
from services.report_events import product_shape_fa
from services.store_config import product_title_by_package_slug
from services.ux_format import STALE_PENDING_SECONDS
from services.wallet import (
    REASON_RETRY_DEBIT,
    InsufficientWalletFunds,
    debit,
    has_provision_fail_credit,
    retry_debit_key,
)

DEFAULT_PAGE_SIZE = 8


class AdminError(Exception):
    """Business-rule failure for admin operations."""


@dataclass(frozen=True)
class DashboardCounts:
    users: int
    banned_users: int
    pending_payments: int
    orders_by_status: dict[str, int]


@dataclass(frozen=True)
class PageResult:
    items: list[Order]
    total: int
    offset: int
    limit: int

    @property
    def page(self) -> int:
        if self.limit <= 0:
            return 0
        return self.offset // self.limit

    @property
    def has_prev(self) -> bool:
        return self.offset > 0

    @property
    def has_next(self) -> bool:
        return self.offset + self.limit < self.total


async def list_pending_payments(
    session: AsyncSession,
    *,
    offset: int = 0,
    limit: int = DEFAULT_PAGE_SIZE,
    stale_only: bool = False,
    stale_seconds: int = STALE_PENDING_SECONDS,
) -> PageResult:
    """Orders in PAYMENT_REVIEW, newest first.

    When ``stale_only`` is True, only include orders whose ``created_at`` is
    older than ``stale_seconds`` (default 1 hour).
    """
    offset = max(0, offset)
    limit = max(1, min(limit, 50))
    filters = [Order.status == OrderStatus.PAYMENT_REVIEW]
    if stale_only:
        cutoff = datetime.now(UTC) - timedelta(seconds=max(1, stale_seconds))
        filters.append(Order.created_at <= cutoff)
    total = await session.scalar(select(func.count()).select_from(Order).where(*filters))
    result = await session.execute(
        select(Order)
        .where(*filters)
        .options(
            selectinload(Order.user),
            selectinload(Order.plan),
            selectinload(Order.payment),
        )
        .order_by(Order.created_at.desc(), Order.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return PageResult(
        items=list(result.scalars().all()),
        total=int(total or 0),
        offset=offset,
        limit=limit,
    )


async def _with_product_label(session: AsyncSession, order: Order) -> Order:
    """Put the product's shelf name on a loaded order, for the admin's order screen.

    ``order.product_label_fa``: «group — plan» by ``store_package_slug``, else
    the volume and days, the same naming the sale report uses. The screen read
    «پلن: volume-5GB-0d-p30000-h2-g3», the dynamic plan row's name.
    Needs ``order.plan`` eagerly loaded, as both loaders below do.
    """
    plan = order.plan
    volume = order.delivery_data_limit_gb
    if volume is None and plan is not None:
        volume = plan.data_limit_gb
    days = effective_delivery_duration_days(order, plan) if plan else order.delivery_duration_days
    order.product_label_fa = await product_title_by_package_slug(  # type: ignore[attr-defined]
        session, order.store_package_slug
    ) or product_shape_fa(volume, days)
    return order


async def get_next_pending_payment(
    session: AsyncSession,
    *,
    exclude_order_id: int | None = None,
) -> Order | None:
    """Next PAYMENT_REVIEW order (newest first), optionally skipping one id."""
    stmt = (
        select(Order)
        .where(Order.status == OrderStatus.PAYMENT_REVIEW)
        .options(
            selectinload(Order.user),
            selectinload(Order.plan),
            selectinload(Order.payment),
        )
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(5)
    )
    result = await session.execute(stmt)
    for order in result.scalars().all():
        if exclude_order_id is not None and order.id == exclude_order_id:
            continue
        return await _with_product_label(session, order)
    return None


async def list_orders(
    session: AsyncSession,
    *,
    status: OrderStatus | None = None,
    offset: int = 0,
    limit: int = DEFAULT_PAGE_SIZE,
) -> PageResult:
    """Recent orders, optionally filtered by status."""
    offset = max(0, offset)
    limit = max(1, min(limit, 50))
    base = select(Order)
    count_base = select(func.count()).select_from(Order)
    if status is not None:
        base = base.where(Order.status == status)
        count_base = count_base.where(Order.status == status)
    total = await session.scalar(count_base)
    result = await session.execute(
        base.options(
            selectinload(Order.user),
            selectinload(Order.plan),
            selectinload(Order.payment),
        )
        .order_by(Order.created_at.desc(), Order.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return PageResult(
        items=list(result.scalars().all()),
        total=int(total or 0),
        offset=offset,
        limit=limit,
    )


async def get_order_detail(session: AsyncSession, order_id: int) -> Order:
    result = await session.execute(
        select(Order)
        .where(Order.id == order_id)
        .options(
            selectinload(Order.user),
            selectinload(Order.plan),
            selectinload(Order.payment),
        )
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise AdminError("order_not_found")
    return await _with_product_label(session, order)


async def set_user_banned(
    session: AsyncSession,
    user_id: int,
    banned: bool,
    *,
    admin_telegram_ids: Sequence[int] | None = None,
) -> User:
    """Set User.is_banned. Idempotent when already in the requested state.

    Banning an admin is refused: ``UserContextMiddleware`` blocks a banned user
    before any handler runs, so a self-ban is an unrecoverable in-bot lockout
    that only direct database access can undo.
    """
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise AdminError("user_not_found")
    if banned and _is_protected_admin(user, admin_telegram_ids):
        raise AdminError("cannot_ban_admin")
    if user.is_banned == banned:
        return user
    user.is_banned = banned
    await session.flush()
    return user


async def set_user_banned_by_telegram_id(
    session: AsyncSession,
    telegram_id: int,
    banned: bool,
    *,
    admin_telegram_ids: Sequence[int] | None = None,
) -> User:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise AdminError("user_not_found")
    return await set_user_banned(session, user.id, banned, admin_telegram_ids=admin_telegram_ids)


def _is_protected_admin(user: User, admin_telegram_ids: Sequence[int] | None) -> bool:
    """True for anyone who administers the bot, by env list or DB flag."""
    if user.is_admin:
        return True
    return bool(admin_telegram_ids) and user.telegram_id in set(admin_telegram_ids or ())


async def retry_provision(
    session: AsyncSession,
    client: PasarGuardClient,
    *,
    order_id: int,
) -> tuple[PasarGuardAccount, ReferralLog | None]:
    """Retry provisioning for PROVISION_FAILED (or resume PAYMENT_APPROVED / PROVISIONING).

    Wraps provision_order; already ACTIVE is a no-op success via provision_order.
    """
    order = await get_order_detail(session, order_id)
    if order.status not in {
        OrderStatus.PROVISION_FAILED,
        OrderStatus.PAYMENT_APPROVED,
        OrderStatus.PROVISIONING,
        OrderStatus.ACTIVE,
    }:
        raise AdminError(f"invalid_status:{order.status.value}")
    # After provision-fail wallet credit, retry must debit wallet first.
    debited_now = False
    if order.status == OrderStatus.PROVISION_FAILED and await has_provision_fail_credit(
        session, order_id
    ):
        try:
            _entry, debited_now = await debit(
                session,
                user_id=order.user_id,
                amount_toman=int(order.price_toman),
                reason=REASON_RETRY_DEBIT,
                idempotency_key=retry_debit_key(order_id),
                order_id=order_id,
                note="admin_retry_after_wallet_credit",
            )
        except InsufficientWalletFunds as exc:
            raise AdminError("insufficient_wallet_for_retry") from exc

    try:
        return await provision_order(
            session,
            client,
            order_id=order_id,
        )
    except ProvisionError as exc:
        raise AdminError(str(exc)) from exc
    except Exception:
        # Anything else (a panel 5xx, a local error) used to reach the handler's
        # «commit what we have» branch with the retry debit still pending: the
        # customer paid again and got nothing. Drop the uncommitted debit. The
        # order was already PROVISION_FAILED (the only state that debits), so
        # nothing else is lost. If provisioning committed its own intent first
        # the debit went with it; that state is «paid, retry pending» and the
        # idempotent debit key means the next retry does not charge again.
        if debited_now:
            await session.rollback()
        raise


async def dashboard_counts(session: AsyncSession) -> DashboardCounts:
    """Simple DB-only counts (no PasarGuard panel stats API)."""
    users = int(await session.scalar(select(func.count()).select_from(User)) or 0)
    banned_users = int(
        await session.scalar(select(func.count()).select_from(User).where(User.is_banned.is_(True)))
        or 0
    )
    pending_payments = int(
        await session.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.status == OrderStatus.PAYMENT_REVIEW)
        )
        or 0
    )
    status_rows = await session.execute(select(Order.status, func.count()).group_by(Order.status))
    orders_by_status = {row[0].value: int(row[1]) for row in status_rows.all()}
    return DashboardCounts(
        users=users,
        banned_users=banned_users,
        pending_payments=pending_payments,
        orders_by_status=orders_by_status,
    )

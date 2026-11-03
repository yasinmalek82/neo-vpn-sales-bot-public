"""Money the shop actually took, over time.

The admin «آمار» screen counted users and orders but showed no money at all —
the single biggest gap for a sales bot. Everything here is read-only and
DB-only: PasarGuard owns service state, this owns the commerce record.

Revenue is recognised at **activation**, not at order creation: an order is
only money once the customer has the service. ``Order.activated_at`` is the
timestamp that matters, and ``price_toman`` is what the customer was charged
after discounts, so it needs no further arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Order, OrderStatus, User
from database.models.pasarguard_account import PasarGuardAccount
from database.models.wallet import WalletEntryType, WalletLedgerEntry
from services.accounts import live_account_clause
from services.ux_format import DEFAULT_DISPLAY_TIMEZONE

__all__ = [
    "FinanceWindow",
    "FinanceReport",
    "get_finance_report",
    "user_sales",
    "window_start",
]

# Orders that represent money received. PAYMENT_APPROVED is included because the
# customer has paid and the panel work is the shop's problem, not theirs.
EARNING_STATUSES = (
    OrderStatus.PAYMENT_APPROVED,
    OrderStatus.PROVISIONING,
    OrderStatus.ACTIVE,
)


@dataclass(frozen=True)
class FinanceWindow:
    """One reporting period."""

    label_fa: str
    orders: int
    revenue_toman: int

    @property
    def average_toman(self) -> int:
        return self.revenue_toman // self.orders if self.orders else 0


@dataclass(frozen=True)
class FinanceReport:
    today: FinanceWindow
    week: FinanceWindow
    month: FinanceWindow
    jalali_month: FinanceWindow
    all_time: FinanceWindow
    wallet_liability_toman: int
    wallet_credited_toman: int
    wallet_spent_toman: int
    active_services: int
    paying_customers: int
    awaiting_review: int
    provision_failed: int


def window_start(now: datetime, days: int) -> datetime:
    """Start of the window, as a UTC instant. ``days=1`` is since Tehran midnight.

    The owner's «امروز» is the ``Asia/Tehran`` calendar day, so the window edge
    is local midnight (``days - 1`` days back for 7d / 30d), converted to UTC to
    compare with the UTC timestamps stored in the database. Iran has no DST
    today; ``ZoneInfo`` keeps this right if that ever changes.
    """
    tz = ZoneInfo(DEFAULT_DISPLAY_TIMEZONE)
    local_now = (now if now.tzinfo else now.replace(tzinfo=UTC)).astimezone(tz)
    local_day = (local_now - timedelta(days=days - 1)).date()
    return datetime(local_day.year, local_day.month, local_day.day, tzinfo=tz).astimezone(UTC)


async def _window(
    session: AsyncSession,
    *,
    label_fa: str,
    since: datetime | None,
    user_id: int | None = None,
) -> FinanceWindow:
    # `price_toman > 0` keeps giveaways out of the count. Revenue was already
    # right — a free trial adds zero — but «فروش امروز: ۵» counting three trial
    # handouts is the number the owner actually steers by.
    stmt = select(
        func.count(Order.id),
        func.coalesce(func.sum(Order.price_toman), 0),
    ).where(Order.status.in_(EARNING_STATUSES), Order.price_toman > 0)
    if user_id is not None:
        stmt = stmt.where(Order.user_id == user_id)
    if since is not None:
        # activated_at is null until the service exists, which is exactly the
        # point at which the sale counts.
        stmt = stmt.where(Order.activated_at.is_not(None), Order.activated_at >= since)
    row = (await session.execute(stmt)).one()
    return FinanceWindow(label_fa=label_fa, orders=int(row[0] or 0), revenue_toman=int(row[1] or 0))


async def today_sales(session: AsyncSession, *, now: datetime | None = None) -> FinanceWindow:
    """«امروز» exactly as the finance report counts it (the admin home shows it)."""
    return await _window(session, label_fa="امروز", since=window_start(now or datetime.now(UTC), 1))


async def user_sales(
    session: AsyncSession, user_id: int, *, now: datetime | None = None
) -> tuple[FinanceWindow, FinanceWindow, FinanceWindow]:
    """One person's today, Jalali month and all-time windows.

    The same ``_window`` the finance report uses, narrowed to one user's orders,
    so a reseller's own page can never count a sale differently from the owner's
    report. Returns ``(today, jalali_month, all_time)``.
    """
    from services.trial import _jalali_month_start

    moment = now or datetime.now(UTC)
    today = await _window(session, label_fa="امروز", since=window_start(moment, 1), user_id=user_id)
    month = await _window(
        session, label_fa="این ماه", since=_jalali_month_start(moment), user_id=user_id
    )
    total = await _window(session, label_fa="از ابتدا", since=None, user_id=user_id)
    return today, month, total


async def get_finance_report(
    session: AsyncSession, *, now: datetime | None = None
) -> FinanceReport:
    """Revenue windows, wallet position, and the counts that need action."""
    moment = now or datetime.now(UTC)

    today = await _window(session, label_fa="امروز", since=window_start(moment, 1))
    week = await _window(session, label_fa="۷ روز اخیر", since=window_start(moment, 7))
    month = await _window(session, label_fa="۳۰ روز اخیر", since=window_start(moment, 30))
    # «این ماه» is the Jalali calendar month in Tehran, the window the owner keeps
    # books by; the trailing 30 days above is a different number on purpose.
    from services.trial import _jalali_month_start

    jalali_month = await _window(session, label_fa="این ماه", since=_jalali_month_start(moment))
    all_time = await _window(session, label_fa="از ابتدا", since=None)

    # What the shop owes customers — real money it must be able to honour.
    liability = int(
        await session.scalar(select(func.coalesce(func.sum(User.wallet_balance_toman), 0))) or 0
    )
    credited = int(
        await session.scalar(
            select(func.coalesce(func.sum(WalletLedgerEntry.amount_toman), 0)).where(
                WalletLedgerEntry.entry_type == WalletEntryType.credit
            )
        )
        or 0
    )
    spent = int(
        await session.scalar(
            select(func.coalesce(func.sum(WalletLedgerEntry.amount_toman), 0)).where(
                WalletLedgerEntry.entry_type == WalletEntryType.debit
            )
        )
        or 0
    )

    active_services = int(
        await session.scalar(
            select(func.count()).select_from(PasarGuardAccount).where(live_account_clause())
        )
        or 0
    )
    paying_customers = int(
        await session.scalar(
            select(func.count(func.distinct(Order.user_id))).where(
                Order.status.in_(EARNING_STATUSES), Order.price_toman > 0
            )
        )
        or 0
    )
    awaiting_review = int(
        await session.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.status == OrderStatus.PAYMENT_REVIEW)
        )
        or 0
    )
    provision_failed = int(
        await session.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.status == OrderStatus.PROVISION_FAILED)
        )
        or 0
    )

    return FinanceReport(
        today=today,
        week=week,
        month=month,
        jalali_month=jalali_month,
        all_time=all_time,
        wallet_liability_toman=liability,
        wallet_credited_toman=credited,
        wallet_spent_toman=spent,
        active_services=active_services,
        paying_customers=paying_customers,
        awaiting_review=awaiting_review,
        provision_failed=provision_failed,
    )

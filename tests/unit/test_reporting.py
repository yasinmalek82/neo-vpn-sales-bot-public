"""Revenue arithmetic. Money reporting is only useful if it is exactly right."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from database.base import Base
from database.models import Order, OrderStatus, Plan, User
from database.models.wallet import WalletEntryType, WalletLedgerEntry
from services.reporting import get_finance_report, window_start

NOW = datetime(2026, 9, 17, 14, 30, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _user(session: AsyncSession, telegram_id: int, *, balance: int = 0) -> User:
    user = User(telegram_id=telegram_id, referral_code=f"ref{telegram_id}")
    user.wallet_balance_toman = balance
    session.add(user)
    await session.flush()
    return user


async def _plan(session: AsyncSession) -> Plan:
    plan = Plan(name="test", data_limit_gb=50, duration_days=30, price_toman=100_000)
    session.add(plan)
    await session.flush()
    return plan


async def _order(
    session: AsyncSession,
    user: User,
    plan: Plan,
    *,
    price: int,
    status: OrderStatus,
    activated_at: datetime | None,
) -> Order:
    order = Order(
        user_id=user.id,
        plan_id=plan.id,
        status=status,
        price_toman=price,
        discount_toman=0,
    )
    order.activated_at = activated_at
    session.add(order)
    await session.flush()
    return order


@pytest.mark.asyncio
async def test_revenue_is_counted_at_activation(session: AsyncSession) -> None:
    """An order is money once the customer has the service, not when placed."""
    user = await _user(session, 1)
    plan = await _plan(session)
    await _order(session, user, plan, price=150_000, status=OrderStatus.ACTIVE, activated_at=NOW)
    # Placed today but never activated — not revenue.
    await _order(
        session,
        user,
        plan,
        price=999_000,
        status=OrderStatus.PENDING_PAYMENT,
        activated_at=None,
    )
    await session.commit()

    report = await get_finance_report(session, now=NOW)
    assert report.today.revenue_toman == 150_000
    assert report.today.orders == 1


@pytest.mark.asyncio
async def test_windows_nest_correctly(session: AsyncSession) -> None:
    user = await _user(session, 1)
    plan = await _plan(session)
    for days_ago, price in ((0, 100_000), (3, 200_000), (20, 400_000), (200, 800_000)):
        await _order(
            session,
            user,
            plan,
            price=price,
            status=OrderStatus.ACTIVE,
            activated_at=NOW - timedelta(days=days_ago),
        )
    await session.commit()

    report = await get_finance_report(session, now=NOW)
    assert report.today.revenue_toman == 100_000
    assert report.week.revenue_toman == 300_000  # today + 3 days ago
    assert report.month.revenue_toman == 700_000  # + 20 days ago
    assert report.all_time.revenue_toman == 1_500_000  # + the old one


@pytest.mark.asyncio
async def test_today_means_since_midnight_not_last_24_hours(
    session: AsyncSession,
) -> None:
    """A sale at 01:00 UTC (04:30 Tehran) belongs to today when it is now 14:30 UTC."""
    user = await _user(session, 1)
    plan = await _plan(session)
    await _order(
        session,
        user,
        plan,
        price=50_000,
        status=OrderStatus.ACTIVE,
        activated_at=NOW.replace(hour=1, minute=0),
    )
    # 23:00 yesterday (Tehran, 19:30 UTC) is inside the last 24 hours but is not today.
    await _order(
        session,
        user,
        plan,
        price=70_000,
        status=OrderStatus.ACTIVE,
        activated_at=NOW.replace(hour=19, minute=30) - timedelta(days=1),
    )
    await session.commit()

    report = await get_finance_report(session, now=NOW)
    assert report.today.revenue_toman == 50_000
    # «today» is the Tehran day: 00:00 Tehran on 17 Sep is 20:30 UTC on the 16th.
    assert window_start(NOW, 1) == datetime(2026, 9, 16, 20, 30, tzinfo=UTC)


@pytest.mark.asyncio
async def test_rejected_and_expired_orders_are_not_revenue(
    session: AsyncSession,
) -> None:
    user = await _user(session, 1)
    plan = await _plan(session)
    for status in (
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
        OrderStatus.PROVISION_FAILED,
        OrderStatus.PAYMENT_REVIEW,
    ):
        await _order(session, user, plan, price=500_000, status=status, activated_at=NOW)
    await session.commit()

    report = await get_finance_report(session, now=NOW)
    assert report.all_time.revenue_toman == 0
    assert report.provision_failed == 1
    assert report.awaiting_review == 1


@pytest.mark.asyncio
async def test_paid_but_not_yet_provisioned_still_counts(
    session: AsyncSession,
) -> None:
    """The customer has paid; a panel delay is the shop's problem, not a refund."""
    user = await _user(session, 1)
    plan = await _plan(session)
    await _order(
        session,
        user,
        plan,
        price=120_000,
        status=OrderStatus.PAYMENT_APPROVED,
        activated_at=NOW,
    )
    await session.commit()
    assert (await get_finance_report(session, now=NOW)).today.revenue_toman == 120_000


@pytest.mark.asyncio
async def test_wallet_liability_is_what_the_shop_owes(session: AsyncSession) -> None:
    await _user(session, 1, balance=30_000)
    await _user(session, 2, balance=45_000)
    await _user(session, 3, balance=0)
    await session.commit()

    report = await get_finance_report(session, now=NOW)
    assert report.wallet_liability_toman == 75_000


@pytest.mark.asyncio
async def test_wallet_flows_are_reported_separately(session: AsyncSession) -> None:
    user = await _user(session, 1, balance=20_000)
    session.add_all(
        [
            WalletLedgerEntry(
                user_id=user.id,
                entry_type=WalletEntryType.credit,
                amount_toman=50_000,
                reason="provision_fail_credit",
                idempotency_key="k1",
                balance_after_toman=70_000,
            ),
            WalletLedgerEntry(
                user_id=user.id,
                entry_type=WalletEntryType.debit,
                amount_toman=30_000,
                reason="checkout",
                idempotency_key="k2",
                balance_after_toman=20_000,
            ),
        ]
    )
    await session.commit()

    report = await get_finance_report(session, now=NOW)
    assert report.wallet_credited_toman == 50_000
    assert report.wallet_spent_toman == 30_000


@pytest.mark.asyncio
async def test_paying_customers_counts_people_not_orders(
    session: AsyncSession,
) -> None:
    plan = await _plan(session)
    one = await _user(session, 1)
    two = await _user(session, 2)
    for _ in range(3):
        await _order(session, one, plan, price=10_000, status=OrderStatus.ACTIVE, activated_at=NOW)
    await _order(session, two, plan, price=10_000, status=OrderStatus.ACTIVE, activated_at=NOW)
    await session.commit()

    report = await get_finance_report(session, now=NOW)
    assert report.today.orders == 4
    assert report.paying_customers == 2


@pytest.mark.asyncio
async def test_average_order_value(session: AsyncSession) -> None:
    user = await _user(session, 1)
    plan = await _plan(session)
    for price in (100_000, 200_000):
        await _order(session, user, plan, price=price, status=OrderStatus.ACTIVE, activated_at=NOW)
    await session.commit()

    report = await get_finance_report(session, now=NOW)
    assert report.today.average_toman == 150_000


@pytest.mark.asyncio
async def test_an_empty_shop_reports_zeroes_not_errors(session: AsyncSession) -> None:
    report = await get_finance_report(session, now=NOW)
    assert report.all_time.revenue_toman == 0
    assert report.all_time.average_toman == 0
    assert report.wallet_liability_toman == 0
    assert report.active_services == 0


@pytest.mark.asyncio
async def test_this_month_is_the_jalali_month_in_tehran_and_skips_giveaways(
    session: AsyncSession,
) -> None:
    """1405/07/01 is 2026-09-23 in Tehran; a sale 2 min before midnight Tehran is last month."""
    from datetime import UTC, datetime

    user = await _user(session, 1)
    plan = await _plan(session)
    now = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    # 2026-09-22 20:29 UTC = 23:59 Tehran (+03:30) on 30 Shahrivar: last month.
    before = datetime(2026, 9, 22, 20, 29, tzinfo=UTC)
    # 2026-09-22 20:31 UTC = 00:01 Tehran on 1 Mehr: this month.
    after = datetime(2026, 9, 22, 20, 31, tzinfo=UTC)
    for at, price in ((before, 100_000), (after, 200_000), (after, 0)):
        await _order(session, user, plan, price=price, status=OrderStatus.ACTIVE, activated_at=at)
    await session.commit()

    report = await get_finance_report(session, now=now)
    assert report.jalali_month.label_fa == "این ماه"
    assert report.jalali_month.revenue_toman == 200_000
    assert report.jalali_month.orders == 1  # the price-0 giveaway is not a sale
    assert report.month.revenue_toman == 300_000  # the 30-day window is unchanged

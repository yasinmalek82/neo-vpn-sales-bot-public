"""Logic audit, area JOBS: reminders, broadcasts, outbox, finance windows.

Every test asserts the correct behaviour and is an open bug: strict xfail keeps
the gate green until somebody fixes it, then goes red until the marker is gone.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramRetryAfter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from database.base import Base
from database.models import Order, OrderStatus, PasarGuardAccount, Plan, User
from database.models.broadcast import BroadcastAudience
from database.models.order import TRIAL_KIND_PERSONAL
from database.models.report import (
    REPORT_SETTINGS_ROW_ID,
    OutboxState,
    ReportOutbox,
    ReportSettings,
)
from services.automation import find_renewal_reminder_candidates, mark_reminder_sent
from services.broadcast import create_broadcast, run_pending_broadcasts
from services.report_outbox import MAX_ATTEMPTS, enqueue_report, flush_reports
from services.reporting import get_finance_report, today_sales


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def _user(session: AsyncSession, tg: int, *, banned: bool = False) -> User:
    user = User(telegram_id=tg, referral_code=f"J{tg}", is_banned=banned)
    session.add(user)
    await session.flush()
    return user


async def _plan(session: AsyncSession) -> Plan:
    plan = Plan(name="jobs", data_limit_gb=50, duration_days=30, price_toman=1000)
    session.add(plan)
    await session.flush()
    return plan


async def _order(
    session: AsyncSession,
    user: User,
    plan: Plan,
    *,
    price: int = 1000,
    status: OrderStatus = OrderStatus.ACTIVE,
    activated_at: datetime | None = None,
    expires_at: datetime | None = None,
    panel_username: str | None = None,
    renew_account_id: int | None = None,
    trial_kind: str | None = None,
) -> Order:
    order = Order(
        user_id=user.id,
        plan_id=plan.id,
        status=status,
        price_toman=price,
        discount_toman=0,
        activated_at=activated_at,
        expires_at=expires_at,
        panel_username=panel_username,
        renew_account_id=renew_account_id,
        trial_kind=trial_kind,
    )
    session.add(order)
    await session.flush()
    return order


@pytest.mark.asyncio
async def test_today_sales_follows_the_tehran_day(session: AsyncSession) -> None:
    user = await _user(session, 1)
    plan = await _plan(session)
    # 00:15 Tehran on 1 Oct (20:45 UTC 30 Sep): today for the owner at 04:00.
    await _order(session, user, plan, activated_at=datetime(2026, 9, 30, 20, 45, tzinfo=UTC))
    # 13:30 Tehran on 30 Sep: yesterday for the owner at 04:00 on 1 Oct.
    await _order(session, user, plan, activated_at=datetime(2026, 9, 30, 10, 0, tzinfo=UTC))
    now = datetime(2026, 10, 1, 0, 30, tzinfo=UTC)  # 04:00 Tehran, 1 Oct

    assert (await today_sales(session, now=now)).orders == 1


@pytest.mark.asyncio
async def test_no_reminder_for_an_account_already_renewed(session: AsyncSession) -> None:
    user = await _user(session, 2)
    plan = await _plan(session)
    now = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    account = PasarGuardAccount(user_id=user.id, panel_username="svc_2", panel_user_id=2)
    session.add(account)
    await session.flush()
    await _order(
        session,
        user,
        plan,
        activated_at=now - timedelta(days=28),
        expires_at=now + timedelta(hours=60),
        panel_username="svc_2",
    )
    # The early renewal: same account, expiry pushed a month out.
    await _order(
        session,
        user,
        plan,
        activated_at=now - timedelta(hours=1),
        expires_at=now + timedelta(days=32),
        panel_username="svc_2",
        renew_account_id=account.id,
    )

    assert await find_renewal_reminder_candidates(session, reminder_days=[3, 1], now=now) == []


@pytest.mark.asyncio
async def test_same_reminder_window_is_sent_once_across_midnight(session: AsyncSession) -> None:
    user = await _user(session, 3)
    plan = await _plan(session)
    first = datetime(2026, 9, 30, 18, 0, tzinfo=UTC)
    await _order(
        session,
        user,
        plan,
        activated_at=first - timedelta(days=27),
        expires_at=first + timedelta(days=2, hours=12),
    )
    got = await find_renewal_reminder_candidates(session, reminder_days=[3, 1], now=first)
    assert len(got) == 1
    await mark_reminder_sent(session, order_id=got[0].order.id, now=first)

    # The next 6-hourly run is 00:00 UTC: new date, still the «3 days» window.
    later = first + timedelta(hours=6)
    assert await find_renewal_reminder_candidates(session, reminder_days=[3, 1], now=later) == []


@pytest.mark.asyncio
async def test_banned_user_gets_no_renewal_reminder(session: AsyncSession) -> None:
    user = await _user(session, 4, banned=True)
    plan = await _plan(session)
    now = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    await _order(session, user, plan, expires_at=now + timedelta(hours=20))

    assert await find_renewal_reminder_candidates(session, reminder_days=[1], now=now) == []


class _Bot:
    def __init__(self) -> None:
        self.sent: list[int] = []

    async def send_message(self, *, chat_id: int, text: str, **_: object) -> None:  # noqa: ARG002
        self.sent.append(chat_id)


@pytest.mark.asyncio
async def test_user_banned_mid_campaign_is_skipped(session: AsyncSession) -> None:
    await _user(session, 10)
    victim = await _user(session, 11)
    await create_broadcast(session, body="hi", audience=BroadcastAudience.all_customers)
    await session.commit()
    victim.is_banned = True
    await session.commit()

    bot = _Bot()
    await run_pending_broadcasts(session, bot)  # type: ignore[arg-type]

    assert 11 not in bot.sent


@pytest.mark.asyncio
async def test_report_survives_a_telegram_flood_wait(session: AsyncSession) -> None:
    session.add(ReportSettings(id=REPORT_SETTINGS_ROW_ID, chat_id=-100, enabled=1, topics={}))
    await session.commit()
    await enqueue_report(session, event_key="k:1", topic_key="sales", body="sale")
    await session.commit()

    class FloodBot:
        flooded = True

        async def send_message(self, **_: object) -> None:
            if self.flooded:
                raise TelegramRetryAfter(method=None, message="flood", retry_after=60)  # type: ignore[arg-type]

    bot = FloodBot()
    for _ in range(MAX_ATTEMPTS):  # five one-minute flushes inside one flood-wait
        await flush_reports(session, bot)  # type: ignore[arg-type]
    bot.flooded = False
    await flush_reports(session, bot)  # type: ignore[arg-type]

    row = await session.get(ReportOutbox, 1)
    assert row is not None and row.state is OutboxState.sent


@pytest.mark.asyncio
async def test_paying_customers_excludes_free_trial_takers(session: AsyncSession) -> None:
    user = await _user(session, 20)
    plan = await _plan(session)
    now = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    await _order(session, user, plan, price=0, activated_at=now, trial_kind=TRIAL_KIND_PERSONAL)

    report = await get_finance_report(session, now=now)

    assert report.paying_customers == 0

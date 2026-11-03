"""A job committing while a renewal reminder is being sent must not replay it.

The reminder job read its candidates (a WAL read snapshot), sent each reminder,
then marked the order. Another job's commit during a send made the mark fail
at once («database is locked»), the pass rolled back every mark it had made,
and the next pass sent the same reminders again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from database.models import Order, OrderStatus, Plan, User
from database.models.plan import DataLimitResetStrategy
from scheduler.jobs.renewal_reminders import run_renewal_reminders

Factory = async_sessionmaker[AsyncSession]


async def _seed(factory: Factory, customers: int) -> None:
    async with factory() as session:
        plan = Plan(
            name="volume-10GB-30d",
            description="test",
            data_limit_gb=10,
            duration_days=30,
            price_toman=150_000,
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            group_ids=[1],
            hwid_limit=2,
            is_active=True,
        )
        session.add(plan)
        await session.flush()
        for i in range(customers):
            user = User(telegram_id=5000 + i, first_name=f"C{i}", referral_code=f"REM{i:05d}")
            session.add(user)
            await session.flush()
            session.add(
                Order(
                    user_id=user.id,
                    plan_id=plan.id,
                    status=OrderStatus.ACTIVE,
                    price_toman=150_000,
                    discount_toman=0,
                    expires_at=datetime.now(UTC) + timedelta(days=2, hours=12),
                )
            )
        await session.commit()


class Bot:
    def __init__(self, factory: Factory) -> None:
        self._factory = factory
        self.sent_to: list[int] = []

    async def send_message(self, *, chat_id: int, **_kwargs: Any) -> Any:
        async with self._factory() as other:  # a scheduler job commits meanwhile
            row = await other.scalar(select(Plan))
            row.description = f"touched {datetime.now(UTC).timestamp()}"
            await other.commit()
        self.sent_to.append(chat_id)
        return SimpleNamespace(message_id=len(self.sent_to))


@pytest.mark.asyncio
async def test_a_job_committing_during_the_sends_does_not_replay_the_reminders(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _url = two_sessions
    await _seed(factory, customers=3)
    bot = Bot(factory)
    settings = Settings(_env_file=None, BOT_TOKEN="test-token")  # type: ignore[call-arg]

    for _pass in range(2):
        await run_renewal_reminders(
            session_factory=factory, bot=bot, reminder_days=[3], settings=settings  # type: ignore[arg-type]
        )

    assert sorted(bot.sent_to) == [5000, 5001, 5002], "one reminder each, across both passes"
    async with factory() as check:
        marks = (await check.scalars(select(Order.last_reminder_at))).all()
    assert all(mark is not None for mark in marks)

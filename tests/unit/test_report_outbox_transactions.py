"""Transactional guarantees for report-outbox idempotency."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from database.base import Base
from database.models import User
from database.models.report import ReportOutbox
from services.report_outbox import enqueue_report


@pytest_asyncio.fixture
async def factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    await engine.dispose()


async def _count(session: AsyncSession, model: type[User] | type[ReportOutbox]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


@pytest.mark.asyncio
async def test_event_key_collision_keeps_caller_business_write_committable(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing the savepoint makes the caught duplicate poison the caller transaction."""
    async with factory() as seed:
        seed.add(ReportOutbox(event_key="payment:7:approved", topic_key="payments", body="اول"))
        await seed.commit()

    async with factory() as session:
        original_scalar = session.scalar
        lookup_calls = 0

        async def stale_first_lookup(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            nonlocal lookup_calls
            lookup_calls += 1
            if lookup_calls == 1:
                return None
            return await original_scalar(*args, **kwargs)

        monkeypatch.setattr(session, "scalar", stale_first_lookup)
        session.add(User(telegram_id=701, referral_code="R701"))

        duplicate = await enqueue_report(
            session,
            event_key="payment:7:approved",
            topic_key="payments",
            body="تکراری",
        )
        await session.commit()

        assert duplicate is None
        assert lookup_calls == 2
        assert await _count(session, User) == 1
        assert await _count(session, ReportOutbox) == 1


@pytest.mark.asyncio
async def test_outer_rollback_undoes_business_write_and_new_report(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Moving the report into a savepoint must not detach it from the caller transaction."""
    async with factory() as session:
        session.add(User(telegram_id=702, referral_code="R702"))

        report = await enqueue_report(
            session,
            event_key="payment:8:approved",
            topic_key="payments",
            body="جدید",
        )
        assert report is not None

        await session.rollback()

    async with factory() as verify:
        assert await _count(verify, User) == 0
        assert await _count(verify, ReportOutbox) == 0


@pytest.mark.asyncio
async def test_unrelated_caller_integrity_error_propagates(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Catching every flush error would hide an invalid business write as a duplicate report."""
    async with factory() as seed:
        seed.add(User(telegram_id=703, referral_code="R703"))
        await seed.commit()

    async with factory() as session:
        session.add(User(telegram_id=703, referral_code="R704"))

        with pytest.raises(IntegrityError):
            await enqueue_report(
                session,
                event_key="payment:9:approved",
                topic_key="payments",
                body="نباید صف شود",
            )


@pytest.mark.asyncio
async def test_pending_same_session_event_with_autoflush_disabled_is_a_duplicate(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A pending event is flushed before the candidate savepoint, preserving idempotency."""
    async with factory() as session:
        session.add(ReportOutbox(event_key="payment:10:approved", topic_key="payments", body="اول"))

        duplicate = await enqueue_report(
            session,
            event_key="payment:10:approved",
            topic_key="payments",
            body="دوم",
        )
        await session.commit()

        assert duplicate is None
        assert await _count(session, ReportOutbox) == 1

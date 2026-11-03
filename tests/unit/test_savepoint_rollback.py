"""SAVEPOINT must actually roll back, including on SQLite.

The pysqlite/aiosqlite driver opens a transaction implicitly for DML but not
for SAVEPOINT, so a ``begin_nested()`` block runs in autocommit and its writes
survive an outer ``rollback()``. Money code uses savepoints to collapse a
concurrent duplicate onto the winner's row; without the fix in
``database/base.py`` a rollback-safety test can only pass by accident, and a
real rollback bug stays invisible on every local run.

Measured on this stack before the fix: a plain flush was undone by rollback,
the same write inside ``begin_nested()`` was not.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import database.models  # noqa: F401  (registers mappers)
from bot.config import Settings
from database.base import Base, get_engine, get_session_factory, init_db
from database.models import User


@pytest_asyncio.fixture
async def factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """Build the engine the way the app does, so the fix under test applies."""
    init_db(Settings(BOT_TOKEN="test-token", DATABASE_URL="sqlite+aiosqlite:///:memory:"))
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield get_session_factory()
    await engine.dispose()


async def _seed(session: AsyncSession) -> int:
    user = User(telegram_id=1, referral_code="R1", wallet_balance_toman=0)
    session.add(user)
    await session.flush()
    await session.commit()
    return int(user.id)


async def _balance(session: AsyncSession, user_id: int) -> int:
    return int(
        await session.scalar(select(User.wallet_balance_toman).where(User.id == user_id)) or 0
    )


@pytest.mark.asyncio
async def test_a_write_inside_a_savepoint_is_undone_by_rollback(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The exact shape services/referral.py uses to credit a bonus."""
    async with factory() as session:
        user_id = await _seed(session)

        async with session.begin_nested():
            user = await session.get(User, user_id)
            assert user is not None
            user.wallet_balance_toman = 14_250
            await session.flush()

        await session.rollback()
        assert await _balance(session, user_id) == 0


@pytest.mark.asyncio
async def test_nested_savepoints_roll_back_together(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The wallet opens one savepoint and the outbox opens another inside it."""
    async with factory() as session:
        user_id = await _seed(session)

        async with session.begin_nested():
            user = await session.get(User, user_id)
            assert user is not None
            user.wallet_balance_toman = 5_000
            await session.flush()
            async with session.begin_nested():
                user.wallet_balance_toman = 9_000
                await session.flush()

        await session.rollback()
        assert await _balance(session, user_id) == 0


@pytest.mark.asyncio
async def test_a_released_savepoint_still_commits_with_the_outer_transaction(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Rolling back must not be achieved by breaking the commit path."""
    async with factory() as session:
        user_id = await _seed(session)

        async with session.begin_nested():
            user = await session.get(User, user_id)
            assert user is not None
            user.wallet_balance_toman = 14_250
            await session.flush()

        await session.commit()
        assert await _balance(session, user_id) == 14_250


@pytest.mark.asyncio
async def test_an_inner_savepoint_can_fail_without_losing_the_outer_work(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A concurrent duplicate collapsing must not discard the caller's work."""
    async with factory() as session:
        user_id = await _seed(session)

        user = await session.get(User, user_id)
        assert user is not None
        user.wallet_balance_toman = 1_000
        await session.flush()

        with pytest.raises(RuntimeError):
            async with session.begin_nested():
                user.wallet_balance_toman = 99_999
                await session.flush()
                # Stands in for the IntegrityError a concurrent duplicate raises.
                raise RuntimeError("duplicate")

        await session.commit()
        assert await _balance(session, user_id) == 1_000

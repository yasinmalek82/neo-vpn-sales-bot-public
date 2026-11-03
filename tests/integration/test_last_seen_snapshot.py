"""Live finding: «database is locked» on an ordinary customer tap.

``UPDATE users SET last_seen_at=…`` failed about a second into the handler,
right after the scheduler's report flush committed: the request's session had
read the user (a WAL read snapshot), another connection committed, and SQLite
refuses to upgrade a stale snapshot to a write at once (SQLITE_BUSY_SNAPSHOT;
``busy_timeout`` does not apply). The customer got «یه مشکلی پیش اومد».
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.middlewares import user_context
from bot.middlewares.user_context import UserContextMiddleware
from database.models import User

TG_ID = 4242
Factory = async_sessionmaker[AsyncSession]


async def _seed(factory: Factory) -> None:
    async with factory() as session:
        session.add(
            User(
                telegram_id=TG_ID,
                first_name="Ali",
                language_code="fa",
                referral_code="SNAP0001",
                last_seen_at=datetime.now(UTC) - timedelta(days=2),
            )
        )
        session.add(User(telegram_id=9, first_name="Other", referral_code="SNAP0002"))
        await session.commit()


async def _scheduler_commits(factory: Factory) -> None:
    """What ``run_report_flush`` / the broadcast worker do: commit on another connection."""
    async with factory() as other:
        row = await other.scalar(select(User).where(User.telegram_id == 9))
        row.first_name = f"Other {datetime.now(UTC).timestamp()}"
        await other.commit()


def _update() -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=TG_ID, type="private"),
            from_user=TgUser(id=TG_ID, is_bot=False, first_name="Ali"),
            text="سلام",
        ),
    )


async def _run(factory: Factory, handler: Any) -> Any:
    """One request, the way ``DbSessionMiddleware`` wraps it."""
    settings = Settings(_env_file=None, BOT_TOKEN="test-token")  # type: ignore[call-arg]
    async with factory() as session:
        data = {"session": session, "settings": settings}

        async def wrapped(event: Any, data: dict[str, Any]) -> Any:
            return await handler(session, data)

        result = await UserContextMiddleware()(wrapped, _update(), data)
        await session.commit()
        return result


async def _last_seen(factory: Factory) -> datetime:
    async with factory() as session:
        value = await session.scalar(select(User.last_seen_at).where(User.telegram_id == TG_ID))
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@pytest.mark.asyncio
async def test_a_job_committing_mid_request_does_not_lock_the_last_seen_write(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _url = two_sessions
    await _seed(factory)

    async def handler(session: AsyncSession, data: dict[str, Any]) -> str:
        await _scheduler_commits(factory)
        await session.flush()  # the handler's first flush carried the UPDATE users
        return str(data["db_user"].first_name)

    assert await _run(factory, handler) == "Ali"
    assert datetime.now(UTC) - await _last_seen(factory) < timedelta(minutes=1)


@pytest.mark.asyncio
async def test_the_handler_can_write_after_a_job_committed(
    two_sessions: tuple[Factory, str],
) -> None:
    """The middleware's read must not leave a stale snapshot for the handler's write."""
    factory, _url = two_sessions
    await _seed(factory)

    async def handler(session: AsyncSession, data: dict[str, Any]) -> None:
        await _scheduler_commits(factory)
        data["db_user"].username = "ali_new"
        await session.flush()

    await _run(factory, handler)
    async with factory() as check:
        assert await check.scalar(select(User.username).where(User.telegram_id == TG_ID)) == (
            "ali_new"
        )


@pytest.mark.asyncio
async def test_a_commit_landing_between_the_read_and_the_touch_costs_only_the_touch(
    two_sessions: tuple[Factory, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The narrowest race: the tap is still served, with a usable ``db_user``."""
    factory, _url = two_sessions
    await _seed(factory)
    real_get = user_context.get_user_by_telegram_id
    raced: list[bool] = []

    async def get_then_race(session: AsyncSession, telegram_id: int) -> Any:
        user = await real_get(session, telegram_id)
        if not raced:
            raced.append(True)
            await _scheduler_commits(factory)
        return user

    monkeypatch.setattr(user_context, "get_user_by_telegram_id", get_then_race)

    async def handler(session: AsyncSession, data: dict[str, Any]) -> str:  # noqa: ARG001
        return str(data["db_user"].first_name)

    assert await _run(factory, handler) == "Ali"

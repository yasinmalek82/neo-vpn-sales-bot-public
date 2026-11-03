"""Live finding: «🔗 وصل کردن گروه» lost every topic it had just created.

The attach read ``report_settings`` (a WAL read snapshot), then spent 19
seconds on Telegram calls (flood pacing), then wrote. A scheduler job committed
meanwhile, so SQLite refused the write at once (SQLITE_BUSY_SNAPSHOT, «database
is locked»; ``busy_timeout`` does not apply). The owner got «یه مشکلی پیش اومد»,
the eight new topics were never saved, and the next attempt made eight more.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import User
from database.models.report import REPORT_SETTINGS_ROW_ID, TOPIC_KEYS, ReportSettings
from services import report_group
from services.report_group import ReportGroupError, run_full_setup, verify_topic

CHAT_ID = -1001234567890
Factory = async_sessionmaker[AsyncSession]


async def _seed(factory: Factory) -> None:
    async with factory() as session:
        session.add(User(telegram_id=9, first_name="Other", referral_code="SNAPRG01"))
        await session.commit()


async def _scheduler_commits(factory: Factory) -> None:
    """What ``run_report_flush`` / the broadcast worker do: commit on another connection."""
    async with factory() as other:
        row = await other.scalar(select(User).where(User.telegram_id == 9))
        row.first_name = f"Other {datetime.now(UTC).timestamp()}"
        await other.commit()


class RacingBot:
    """A forum group where every Telegram call takes long enough for a job to commit."""

    def __init__(self, during_call: Callable[[], Awaitable[None]]) -> None:
        self._during_call = during_call
        self.created: list[str] = []
        self.edited: list[int] = []
        self.sent: list[int] = []
        self._next_thread_id = 100

    async def get_chat(self, chat_id: int) -> Any:
        await self._during_call()
        return SimpleNamespace(id=chat_id, type="supergroup", is_forum=True, title="گزارش‌ها")

    async def get_me(self) -> Any:
        return SimpleNamespace(id=777)

    async def get_chat_member(self, *_ids: int) -> Any:
        return SimpleNamespace(status="administrator", can_manage_topics=True)

    async def create_forum_topic(self, **kwargs: Any) -> Any:
        await self._during_call()
        self._next_thread_id += 1
        self.created.append(kwargs["name"])
        return SimpleNamespace(message_thread_id=self._next_thread_id, name=kwargs["name"])

    async def edit_forum_topic(self, **kwargs: Any) -> bool:
        await self._during_call()
        self.edited.append(int(kwargs["message_thread_id"]))
        return True

    async def send_message(self, **kwargs: Any) -> Any:
        await self._during_call()
        self.sent.append(int(kwargs["message_thread_id"]))
        return SimpleNamespace(message_id=len(self.sent))


async def _saved(factory: Factory) -> ReportSettings:
    async with factory() as check:
        row = await check.get(ReportSettings, REPORT_SETTINGS_ROW_ID)
        assert row is not None
        return row


@pytest.mark.asyncio
async def test_a_job_committing_during_the_telegram_calls_does_not_lose_the_topics(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _url = two_sessions
    await _seed(factory)
    bot = RacingBot(lambda: _scheduler_commits(factory))

    async with factory() as session:
        result = await run_full_setup(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
        await session.commit()

    assert result.ok
    row = await _saved(factory)
    assert set(row.topics) == set(TOPIC_KEYS)
    assert set(row.verified) == set(TOPIC_KEYS)
    assert row.enabled


@pytest.mark.asyncio
async def test_a_setup_that_dies_halfway_keeps_the_topics_it_created_and_a_retry_reuses_them(
    two_sessions: tuple[Factory, str],
) -> None:
    """No orphan duplicates: the second attempt renames the saved topics, it creates only the rest."""
    factory, _url = two_sessions
    await _seed(factory)

    async def nothing() -> None:
        return None

    bot = RacingBot(nothing)

    class Died(BaseException):
        """Not an ``Exception``: the per-topic handler must not swallow it."""

    async with factory() as session:
        real_create = bot.create_forum_topic

        async def create_then_die(**kwargs: Any) -> Any:
            if len(bot.created) >= 3:
                raise Died
            return await real_create(**kwargs)

        bot.create_forum_topic = create_then_die  # type: ignore[method-assign]
        with pytest.raises(Died):
            await run_full_setup(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
        await session.rollback()  # what ``DbSessionMiddleware`` does on an error

    row = await _saved(factory)
    assert len(row.topics) == 3, "the three topics Telegram created must be on file"
    first_three = sorted(int(v) for v in row.topics.values())

    bot.create_forum_topic = real_create  # type: ignore[method-assign]
    async with factory() as session:
        result = await run_full_setup(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
        await session.commit()

    assert result.ok
    assert sorted(bot.edited) == first_three, "saved topics are renamed in place, not recreated"
    assert len(bot.created) == len(TOPIC_KEYS), "one topic per route across both attempts"


@pytest.mark.asyncio
async def test_a_job_committing_during_the_single_topic_test_does_not_lose_the_proof(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _url = two_sessions
    await _seed(factory)

    async def nothing() -> None:
        return None

    async with factory() as session:
        await run_full_setup(session, RacingBot(nothing), chat_id=CHAT_ID)  # type: ignore[arg-type]
        row = await session.get(ReportSettings, REPORT_SETTINGS_ROW_ID)
        assert row is not None
        row.verified = {}
        await session.commit()

    bot = RacingBot(lambda: _scheduler_commits(factory))
    async with factory() as session:
        readiness = await verify_topic(session, bot, topic_key="sales")  # type: ignore[arg-type]
        await session.commit()

    assert next(t for t in readiness.topics if t.key == "sales").ready
    assert "sales" in (await _saved(factory)).verified


@pytest.mark.asyncio
async def test_a_job_committing_during_a_failed_test_still_records_the_loss_of_proof(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _url = two_sessions
    await _seed(factory)

    async def nothing() -> None:
        return None

    async with factory() as session:
        await run_full_setup(session, RacingBot(nothing), chat_id=CHAT_ID)  # type: ignore[arg-type]
        await session.commit()

    async def commit_then_fail() -> None:
        await _scheduler_commits(factory)
        raise RuntimeError("message thread not found")

    async with factory() as session:
        with pytest.raises(ReportGroupError):
            await verify_topic(
                session, RacingBot(commit_then_fail), topic_key="sales"  # type: ignore[arg-type]
            )
        await session.commit()  # what the handler does on a failed test

    assert "sales" not in (await _saved(factory)).verified


@pytest.mark.asyncio
async def test_a_commit_landing_between_the_reread_and_the_write_is_retried_once(
    two_sessions: tuple[Factory, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The narrowest race: the save re-reads the row, a job commits, the save writes."""
    factory, _url = two_sessions
    await _seed(factory)

    async def nothing() -> None:
        return None

    real_fresh = report_group._fresh_settings
    raced: list[bool] = []

    async def reread_then_race(session: AsyncSession) -> ReportSettings:
        row = await real_fresh(session)
        if not raced:
            raced.append(True)
            await _scheduler_commits(factory)
        return row

    async with factory() as session:
        await report_group.attach_group(
            session, RacingBot(nothing), chat_id=CHAT_ID  # type: ignore[arg-type]
        )
        await session.commit()
        monkeypatch.setattr(report_group, "_fresh_settings", reread_then_race)
        await report_group.reconcile_topics(session, RacingBot(nothing))  # type: ignore[arg-type]
        await session.commit()

    assert raced
    assert set((await _saved(factory)).topics) == set(TOPIC_KEYS)


@pytest.mark.asyncio
async def test_no_transaction_is_open_while_telegram_is_being_called(
    two_sessions: tuple[Factory, str],
) -> None:
    """The boundary itself, not the retry that would paper over it: an open
    read transaction across a 19-second setup is what went stale."""
    factory, _url = two_sessions
    await _seed(factory)
    open_during_call: list[bool] = []

    async with factory() as session:

        async def note() -> None:
            open_during_call.append(session.in_transaction())

        # The handler arrives having read something, as any handler may.
        await session.scalar(select(User).where(User.telegram_id == 9))
        bot = RacingBot(note)
        await run_full_setup(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
        await run_full_setup(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
        await verify_topic(session, bot, topic_key="sales")  # type: ignore[arg-type]
        await session.commit()

    assert bot.created and bot.edited and bot.sent
    assert open_during_call and not any(open_during_call)

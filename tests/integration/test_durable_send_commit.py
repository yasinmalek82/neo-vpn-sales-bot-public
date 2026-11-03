"""A delivery worker's own commit must survive the outcome it already persisted.

SH-09 writes each terminal outcome on a second connection before the batch
commit, so a crash after Telegram accepted a send cannot replay it. On a real
SQLite file (WAL) the batch session still holds a read snapshot from before
that write, and its commit then rewrote the same row: SQLite refuses a writer
whose snapshot is stale, instantly, whatever ``busy_timeout`` says. Every flush
that sent a report logged «database is locked» — first seen in the owner's
live smoke, §3. The SH-09 tests replace ``session.commit`` with a crash, so the
real commit never ran against a file.

The broadcast worker had a worse variant: a recipient whose send raised but
stayed pending was persisted only by that failing commit, so its attempt
counter never moved and it could be retried for ever.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import User
from database.models.broadcast import BroadcastAudience, BroadcastTarget, DeliveryState
from database.models.report import (
    REPORT_SETTINGS_ROW_ID,
    OutboxState,
    ReportOutbox,
    ReportSettings,
)
from services.broadcast import create_broadcast, run_pending_broadcasts
from services.report_outbox import enqueue_report, flush_reports


async def _customers(session: AsyncSession, n: int) -> None:
    session.add_all(
        User(
            telegram_id=20_000 + i,
            username=f"d{i}",
            first_name=f"D{i}",
            language_code="fa",
            referral_code=f"DC{i:06d}",
            is_admin=False,
        )
        for i in range(n)
    )
    await session.commit()


@pytest.mark.asyncio
async def test_a_report_flush_commits_after_persisting_its_sends(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _url = two_sessions
    async with factory() as session:
        session.add(
            ReportSettings(
                id=REPORT_SETTINGS_ROW_ID,
                chat_id=-100,
                chat_title="reports",
                topics={"sales": 11},
                verified={},
                enabled=True,
            )
        )
        await session.commit()
        await enqueue_report(session, event_key="e1", topic_key="sales", body="یک")
        await enqueue_report(session, event_key="e2", topic_key="sales", body="دو")
        await session.commit()

    sent: list[str] = []

    class _Bot:
        async def send_message(
            self, *, chat_id: int, text: str, message_thread_id: int | None = None
        ) -> SimpleNamespace:
            _ = (chat_id, message_thread_id)
            sent.append(text)
            return SimpleNamespace(message_id=len(sent))

    async with factory() as session:
        assert await flush_reports(session, _Bot()) == (2, 0)  # type: ignore[arg-type]

    async with factory() as session:
        states = (await session.execute(select(ReportOutbox.state))).scalars().all()
    assert sorted(sent) == ["دو", "یک"]
    assert states == [OutboxState.sent, OutboxState.sent]


@pytest.mark.asyncio
async def test_a_broadcast_batch_commits_after_persisting_its_sends(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, _url = two_sessions
    monkeypatch.setattr("services.broadcast.SEND_INTERVAL_SECONDS", 0)
    async with factory() as session:
        await _customers(session, 2)
        await create_broadcast(
            session, body="سلام", audience=BroadcastAudience.all_customers, admin_telegram_id=1
        )
        await session.commit()

    class _Bot:
        async def send_message(self, *, chat_id: int, text: str) -> SimpleNamespace:
            _ = (chat_id, text)
            return SimpleNamespace(message_id=1)

    async with factory() as session:
        await run_pending_broadcasts(session, _Bot(), batch=40)  # type: ignore[arg-type]

    async with factory() as session:
        states = (await session.execute(select(BroadcastTarget.state))).scalars().all()
    assert states == [DeliveryState.sent, DeliveryState.sent]


@pytest.mark.asyncio
async def test_a_failing_recipient_counts_its_attempts_beside_a_sent_one(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three passes must retire a recipient that always fails, not loop on it."""
    factory, _url = two_sessions
    monkeypatch.setattr("services.broadcast.SEND_INTERVAL_SECONDS", 0)
    async with factory() as session:
        await _customers(session, 2)
        await create_broadcast(
            session, body="سلام", audience=BroadcastAudience.all_customers, admin_telegram_id=1
        )
        await session.commit()

    class _Bot:
        async def send_message(self, *, chat_id: int, text: str) -> SimpleNamespace:
            _ = text
            if chat_id == 20_001:
                raise RuntimeError("network hiccup")
            return SimpleNamespace(message_id=1)

    for _ in range(3):
        async with factory() as session:
            try:
                await run_pending_broadcasts(session, _Bot(), batch=40)  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001 - the job wrapper swallows and logs this
                await session.rollback()

    async with factory() as session:
        rows = (
            (await session.execute(select(BroadcastTarget).order_by(BroadcastTarget.telegram_id)))
            .scalars()
            .all()
        )
    assert [(r.state, r.attempts) for r in rows] == [
        (DeliveryState.sent, 1),
        (DeliveryState.failed, 3),
    ]

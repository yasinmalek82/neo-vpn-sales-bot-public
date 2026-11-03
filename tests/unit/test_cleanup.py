"""Pruning append-only tables without losing anything that still matters.

The outbox and broadcast tables are append-only by design — that is what makes
delivery exactly-once and resumable — so they grow forever unless pruned. The
danger is pruning too much: deleting a pending report loses it, and deleting a
pending broadcast target re-sends a campaign to someone who already got it.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from database.base import Base
from database.models import User
from database.models.broadcast import (
    Broadcast,
    BroadcastAudience,
    BroadcastState,
    BroadcastTarget,
    DeliveryState,
)
from database.models.report import OutboxState, ReportOutbox
from scheduler.jobs.cleanup import prune_old_rows

RETENTION = 90
OLD = datetime.now(UTC) - timedelta(days=RETENTION + 10)
RECENT = datetime.now(UTC) - timedelta(days=1)

_ids = iter(range(1000, 9999))


def _next_telegram_id() -> int:
    return next(_ids)


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _report(state: OutboxState, sent_at: datetime | None, key: str) -> ReportOutbox:
    return ReportOutbox(event_key=key, topic_key="sales", body="x", state=state, sent_at=sent_at)


async def _campaign(
    session: AsyncSession,
    *,
    state: BroadcastState,
    finished_at: datetime | None,
    recipients: int = 2,
) -> Broadcast:
    broadcast = Broadcast(
        body="x",
        audience=BroadcastAudience.all_customers,
        state=state,
        finished_at=finished_at,
    )
    session.add(broadcast)
    await session.flush()
    # One user per recipient: (broadcast_id, user_id) is unique, which is what
    # stops a campaign reaching the same person twice.
    for _ in range(recipients):
        user = User(telegram_id=_next_telegram_id(), referral_code=f"R{_next_telegram_id()}")
        session.add(user)
        await session.flush()
        session.add(
            BroadcastTarget(
                broadcast_id=broadcast.id,
                user_id=user.id,
                telegram_id=int(user.telegram_id),
                state=DeliveryState.sent,
            )
        )
    await session.flush()
    return broadcast


async def _reports(session: AsyncSession) -> list[ReportOutbox]:
    return list((await session.execute(select(ReportOutbox))).scalars().all())


@pytest.mark.asyncio
async def test_old_delivered_reports_are_pruned(session: AsyncSession) -> None:
    session.add(_report(OutboxState.sent, OLD, "old"))
    await session.commit()

    removed = await prune_old_rows(session, retention_days=RETENTION)
    assert removed["reports"] == 1
    assert await _reports(session) == []


@pytest.mark.asyncio
async def test_a_pending_report_is_never_pruned(session: AsyncSession) -> None:
    """Deleting it would silently lose a report that never went out."""
    session.add(_report(OutboxState.pending, None, "waiting"))
    await session.commit()

    await prune_old_rows(session, retention_days=RETENTION)
    assert [r.event_key for r in await _reports(session)] == ["waiting"]


@pytest.mark.asyncio
async def test_failed_reports_are_kept_as_the_record_of_what_never_arrived(
    session: AsyncSession,
) -> None:
    session.add(_report(OutboxState.failed, OLD, "lost"))
    await session.commit()

    await prune_old_rows(session, retention_days=RETENTION)
    assert [r.event_key for r in await _reports(session)] == ["lost"]


@pytest.mark.asyncio
async def test_recently_delivered_reports_are_kept(session: AsyncSession) -> None:
    session.add(_report(OutboxState.sent, RECENT, "fresh"))
    await session.commit()

    await prune_old_rows(session, retention_days=RETENTION)
    assert [r.event_key for r in await _reports(session)] == ["fresh"]


@pytest.mark.asyncio
async def test_recipients_of_an_old_finished_campaign_are_pruned(
    session: AsyncSession,
) -> None:
    """The per-recipient rows are the bulk; the campaign row is the audit trail."""
    broadcast = await _campaign(session, state=BroadcastState.done, finished_at=OLD)
    await session.commit()

    removed = await prune_old_rows(session, retention_days=RETENTION)
    assert removed["broadcast_targets"] == 2
    assert (await session.execute(select(BroadcastTarget))).scalars().all() == []
    assert await session.get(Broadcast, broadcast.id) is not None


@pytest.mark.asyncio
async def test_a_running_campaign_is_untouchable(session: AsyncSession) -> None:
    """Pruning a pending target would re-send to someone who already got it."""
    await _campaign(session, state=BroadcastState.sending, finished_at=None)
    await session.commit()

    await prune_old_rows(session, retention_days=RETENTION)
    rows = (await session.execute(select(BroadcastTarget))).scalars().all()
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_a_recently_finished_campaign_is_kept(session: AsyncSession) -> None:
    await _campaign(session, state=BroadcastState.done, finished_at=RECENT)
    await session.commit()

    await prune_old_rows(session, retention_days=RETENTION)
    rows = (await session.execute(select(BroadcastTarget))).scalars().all()
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_a_cancelled_campaign_is_pruned_like_a_finished_one(
    session: AsyncSession,
) -> None:
    await _campaign(session, state=BroadcastState.cancelled, finished_at=OLD)
    await session.commit()

    removed = await prune_old_rows(session, retention_days=RETENTION)
    assert removed["broadcast_targets"] == 2


@pytest.mark.asyncio
async def test_zero_retention_disables_pruning(session: AsyncSession) -> None:
    """A misconfigured 0 must mean «keep everything», not «delete everything»."""
    session.add(_report(OutboxState.sent, OLD, "old"))
    await _campaign(session, state=BroadcastState.done, finished_at=OLD)
    await session.commit()

    assert await prune_old_rows(session, retention_days=0) == {}
    assert len(await _reports(session)) == 1
    assert len((await session.execute(select(BroadcastTarget))).scalars().all()) == 2


# --------------------------------------------------------------------------- #
# The shop's change log
# --------------------------------------------------------------------------- #


def _version(number, status, *, age_days: float = 0.0):
    """`pricing_document` is NOT NULL; the real code always sets it."""
    from database.models.store import StoreVersion

    stamp = datetime.now(UTC) - timedelta(days=age_days)
    return StoreVersion(
        version=number,
        status=status,
        pricing_document={},
        published_at=stamp,
        updated_at=stamp,
    )


@pytest.mark.asyncio
async def test_entries_inside_the_window_are_kept(session: AsyncSession) -> None:
    from database.models.store import StoreVersion, StoreVersionStatus
    from scheduler.jobs.cleanup import STORE_CHANGE_RETENTION_DAYS

    for i in range(1, 16):
        session.add(_version(i, StoreVersionStatus.archived, age_days=1))
    session.add(_version(16, StoreVersionStatus.published, age_days=0))
    await session.commit()

    removed = await prune_old_rows(session, retention_days=90)
    assert (
        removed["store_versions"] == 0
    ), f"nothing is older than {STORE_CHANGE_RETENTION_DAYS} days yet"
    assert len((await session.execute(select(StoreVersion))).scalars().all()) == 16


@pytest.mark.asyncio
async def test_old_entries_go_but_the_live_catalog_stays(session: AsyncSession) -> None:
    from database.models.store import StoreVersion, StoreVersionStatus
    from scheduler.jobs.cleanup import KEEP_ARCHIVED_STORE_VERSIONS

    for i in range(1, 11):  # ten stale entries
        session.add(_version(i, StoreVersionStatus.archived, age_days=30))
    session.add(_version(11, StoreVersionStatus.published, age_days=30))
    session.add(_version(12, StoreVersionStatus.draft, age_days=30))
    await session.commit()

    removed = await prune_old_rows(session, retention_days=90)
    assert removed["store_versions"] == 10 - KEEP_ARCHIVED_STORE_VERSIONS

    rows = (await session.execute(select(StoreVersion))).scalars().all()
    by_status: dict[StoreVersionStatus, list[int]] = {}
    for row in rows:
        by_status.setdefault(row.status, []).append(row.version)
    assert by_status[StoreVersionStatus.published] == [11]
    assert by_status[StoreVersionStatus.draft] == [12]
    kept = sorted(by_status[StoreVersionStatus.archived])
    assert len(kept) == KEEP_ARCHIVED_STORE_VERSIONS
    assert min(kept) == 10 - KEEP_ARCHIVED_STORE_VERSIONS + 1, "oldest must go first"


@pytest.mark.asyncio
async def test_a_quiet_month_does_not_empty_the_log(session: AsyncSession) -> None:
    """Age alone would wipe every entry and take undo with it.

    Nobody edits the shop for a month; every entry is now «old». Without the
    floor the owner opens «تغییرات فروشگاه» and finds nothing to take back.
    """
    from database.models.store import StoreVersion, StoreVersionStatus
    from scheduler.jobs.cleanup import KEEP_ARCHIVED_STORE_VERSIONS

    for i in range(1, 4):
        session.add(_version(i, StoreVersionStatus.archived, age_days=40))
    session.add(_version(4, StoreVersionStatus.published, age_days=40))
    await session.commit()

    removed = await prune_old_rows(session, retention_days=90)
    assert removed["store_versions"] == 0

    archived = (
        (
            await session.execute(
                select(StoreVersion).where(StoreVersion.status == StoreVersionStatus.archived)
            )
        )
        .scalars()
        .all()
    )
    assert len(archived) == 3
    assert len(archived) <= KEEP_ARCHIVED_STORE_VERSIONS


@pytest.mark.asyncio
async def test_pruning_a_version_takes_its_catalog_rows_with_it(
    session: AsyncSession,
) -> None:
    """Otherwise the rows survive as orphans and the slug index keeps growing."""
    from database.models.store import (
        StoreCategory,
        StorePackage,
        StorePackageKind,
        StoreVersionStatus,
    )
    from scheduler.jobs.cleanup import KEEP_ARCHIVED_STORE_VERSIONS

    doomed = _version(1, StoreVersionStatus.archived, age_days=30)
    session.add(doomed)
    await session.flush()
    cat = StoreCategory(store_version_id=doomed.id, slug="volume", title_fa="حجمی")
    session.add(cat)
    await session.flush()
    session.add(
        StorePackage(
            store_version_id=doomed.id,
            category_id=cat.id,
            slug="volume-plan-1",
            kind=StorePackageKind.ready,
            title_fa="۵۰ گیگ",
            fixed_price_toman=150_000,
        )
    )
    for i in range(2, KEEP_ARCHIVED_STORE_VERSIONS + 2):
        session.add(_version(i, StoreVersionStatus.archived, age_days=30))
    await session.commit()

    await prune_old_rows(session, retention_days=90)

    assert (await session.execute(select(StoreCategory))).scalars().all() == []
    assert (await session.execute(select(StorePackage))).scalars().all() == []

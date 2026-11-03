"""Prune rows that have served their purpose.

The outbox and broadcast tables are append-only by design — that is what makes
delivery exactly-once and resumable — so without pruning they grow forever.
Only rows in a terminal state and older than the retention window are removed;
anything still pending or sending is untouchable, because deleting it would
either lose a report or re-send a campaign.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models.broadcast import Broadcast, BroadcastState, BroadcastTarget
from database.models.report import OutboxState, ReportOutbox
from database.models.store import StoreVersion, StoreVersionStatus

logger = logging.getLogger(__name__)

# The shop's change log is «what I did this week, and can I take it back» —
# so it is kept by age, which is how the owner thinks about it.
STORE_CHANGE_RETENTION_DAYS = 7

# …but with a floor, because age alone has a hole: leave the shop untouched for
# two weeks and a pure 7-day rule empties the log, taking the ability to undo
# the most recent change with it. Whichever rule keeps more rows wins.
KEEP_ARCHIVED_STORE_VERSIONS = 5


async def prune_old_rows(session: AsyncSession, *, retention_days: int) -> dict[str, int]:
    """Delete delivered reports and finished campaigns past the window."""
    if retention_days <= 0:
        return {}
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    removed: dict[str, int] = {}

    reports = await session.execute(
        delete(ReportOutbox).where(
            # `failed` rows are kept: they are the record of what never arrived.
            ReportOutbox.state == OutboxState.sent,
            ReportOutbox.sent_at.is_not(None),
            ReportOutbox.sent_at < cutoff,
        )
    )
    removed["reports"] = int(reports.rowcount or 0)

    finished = (
        (
            await session.execute(
                select(Broadcast.id).where(
                    Broadcast.state.in_((BroadcastState.done, BroadcastState.cancelled)),
                    Broadcast.finished_at.is_not(None),
                    Broadcast.finished_at < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    if finished:
        # Targets go first: the campaign row is the audit trail worth keeping
        # longest, while the per-recipient rows are the bulk.
        targets = await session.execute(
            delete(BroadcastTarget).where(BroadcastTarget.broadcast_id.in_(finished))
        )
        removed["broadcast_targets"] = int(targets.rowcount or 0)

    removed["store_versions"] = await _prune_archived_store_versions(session)

    await session.commit()
    return removed


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; Postgres does not. Same pattern as
    `services/ux_format.py`."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _prune_archived_store_versions(session: AsyncSession) -> int:
    """Trim the shop's change log.

    Every confirmed catalog edit publishes, which archives the version it
    replaced — so this table grows once per price change rather than once per
    release, and each archived row is one undoable entry in «تغییرات فروشگاه».

    A row is dropped only when it is **both** older than
    `STORE_CHANGE_RETENTION_DAYS` **and** outside the newest
    `KEEP_ARCHIVED_STORE_VERSIONS`. The published version and any draft are
    never candidates. Categories and packages go with a deleted version through
    the relationship cascade.
    """
    cutoff = datetime.now(UTC) - timedelta(days=STORE_CHANGE_RETENTION_DAYS)
    archived = list(
        (
            await session.execute(
                select(StoreVersion)
                .where(StoreVersion.status == StoreVersionStatus.archived)
                .order_by(StoreVersion.version.desc())
            )
        )
        .scalars()
        .all()
    )
    doomed = [
        v.id
        for v in archived[KEEP_ARCHIVED_STORE_VERSIONS:]
        if _as_utc(v.published_at or v.updated_at) < cutoff
    ]
    if not doomed:
        return 0
    # Loaded as objects, not a bulk DELETE, so the ORM cascade removes the
    # child categories and packages with them.
    for version_id in doomed:
        version = await session.get(StoreVersion, version_id)
        if version is not None:
            await session.delete(version)
    return len(doomed)


async def run_cleanup(session_factory: async_sessionmaker, retention_days: int) -> None:
    async with session_factory() as session:
        try:
            removed = await prune_old_rows(session, retention_days=retention_days)
        except Exception:
            logger.exception("Cleanup failed")
            return
    if any(removed.values()):
        logger.info("Cleanup removed %s", removed)

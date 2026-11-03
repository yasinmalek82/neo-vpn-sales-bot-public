"""The list the owner approves by hand (and the preview he only looks at).

A run that reaches the hard stop deletes nothing and stores what it would have
deleted as a **batch**. Approving a batch deletes ONLY its ids, each re-read from
the panel and re-decided right before its delete (``services/lifecycle_run.py``).

* A batch is **void** when it is older than ``BATCH_VALID_HOURS`` or was made
  under other deletion numbers (``numbers_key``), or when a newer list replaced it.
* **Used exactly once**: ``claim_batch`` is one conditional UPDATE, so a second
  confirm (double tap, stale button) finds it used and deletes nothing.
* A preview is a batch of kind ``preview``: the same rows, never approvable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import PasarGuardAccount, User
from database.models.lifecycle import (
    BATCH_KIND_MANUAL,
    BATCH_KIND_PREVIEW,
    BATCH_KIND_STOP,
    BATCH_OPEN,
    BATCH_USED,
    BATCH_VOID,
    ITEM_DELETE,
    LifecycleBatch,
    LifecycleBatchItem,
)
from services.lifecycle_settings import LifecycleConfig
from services.ux_format import to_persian_digits

BATCH_VALID_HOURS = 48
APPROVABLE_KINDS = (BATCH_KIND_STOP, BATCH_KIND_MANUAL)

VOID_AGE = "age"
VOID_NUMBERS = "numbers"
VOID_REPLACED = "replaced"
VOID_USED = "used"
VOID_PREVIEW = "preview"


@dataclass(frozen=True)
class ItemData:
    account_id: int
    action: str
    reason_code: str
    reason_days: int | None = None
    after_warning: bool = False


@dataclass(frozen=True)
class BatchRow:
    """One service of a batch, ready to be a button."""

    account_id: int
    action: str
    reason_code: str
    reason_days: int | None
    after_warning: bool
    title: str
    owner: str
    deleted: bool


def reason_fa(code: str, days: int | None) -> str:
    """«۱۴ روز از انقضا», «۱۲ روز بدون حجم», «تست تمام‌شده»: why a service is listed."""
    from bot.texts import texts

    n = to_persian_digits(int(days or 0))
    if code == "limited":
        return texts.admin_life_reason_limited.format(n=n)
    if code == "trial":
        return texts.admin_life_reason_trial
    return texts.admin_life_reason_expired.format(n=n)


def numbers_key(config: LifecycleConfig) -> str:
    return (
        f"{config.delete_after_expiry_days}:{config.delete_after_limited_days}:"
        f"{config.delete_trial_after_days}:{config.delete_warn_days_before}"
    )


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def void_reason(batch: LifecycleBatch, config: LifecycleConfig, now: datetime) -> str | None:
    """Why this list cannot be approved, or None when it can."""
    if batch.kind not in APPROVABLE_KINDS:
        return VOID_PREVIEW
    if batch.status == BATCH_USED:
        return VOID_USED
    if batch.status == BATCH_VOID:
        return VOID_REPLACED
    created = _aware(batch.created_at)
    if created is None or now - created > timedelta(hours=BATCH_VALID_HOURS):
        return VOID_AGE
    if batch.numbers_key != numbers_key(config):
        return VOID_NUMBERS
    return None


async def create_batch(
    session: AsyncSession,
    *,
    kind: str,
    config: LifecycleConfig,
    items: Sequence[ItemData],
    now: datetime,
) -> LifecycleBatch:
    """Store a list. A new approvable list voids older open ones; a new preview
    replaces older previews. The caller commits."""
    if kind == BATCH_KIND_PREVIEW:
        await session.execute(delete(LifecycleBatch).where(LifecycleBatch.kind == kind))
    else:
        await session.execute(
            update(LifecycleBatch)
            .where(LifecycleBatch.kind.in_(APPROVABLE_KINDS), LifecycleBatch.status == BATCH_OPEN)
            .values(status=BATCH_VOID)
        )
    batch = LifecycleBatch(
        kind=kind, status=BATCH_OPEN, numbers_key=numbers_key(config), created_at=now
    )
    session.add(batch)
    await session.flush()
    for item in items:
        session.add(
            LifecycleBatchItem(
                batch_id=batch.id,
                account_id=item.account_id,
                action=item.action,
                reason_code=item.reason_code,
                reason_days=item.reason_days,
                after_warning=item.after_warning,
            )
        )
    await session.flush()
    return batch


async def current_batch(session: AsyncSession) -> LifecycleBatch | None:
    """The newest approvable list that is still open (it may be void by age or numbers)."""
    return (
        await session.execute(
            select(LifecycleBatch)
            .where(LifecycleBatch.kind.in_(APPROVABLE_KINDS), LifecycleBatch.status == BATCH_OPEN)
            .order_by(LifecycleBatch.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def same_open_list(
    session: AsyncSession,
    *,
    config: LifecycleConfig,
    account_ids: Sequence[int],
    now: datetime,
) -> LifecycleBatch | None:
    """The open, valid stop list holding exactly these services, if there is one."""
    batch = await current_batch(session)
    if batch is None or void_reason(batch, config, now) is not None:
        return None
    stored = set(
        (
            await session.execute(
                select(LifecycleBatchItem.account_id).where(LifecycleBatchItem.batch_id == batch.id)
            )
        )
        .scalars()
        .all()
    )
    return batch if stored == {int(a) for a in account_ids} else None


async def get_batch(session: AsyncSession, batch_id: int) -> LifecycleBatch | None:
    return await session.get(LifecycleBatch, int(batch_id))


async def item_count(session: AsyncSession, batch_id: int, *, action: str | None = None) -> int:
    statement = (
        select(func.count())
        .select_from(LifecycleBatchItem)
        .where(LifecycleBatchItem.batch_id == int(batch_id))
    )
    if action is not None:
        statement = statement.where(LifecycleBatchItem.action == action)
    return int(await session.scalar(statement) or 0)


async def batch_rows(
    session: AsyncSession, batch_id: int, *, offset: int = 0, limit: int | None = None
) -> list[BatchRow]:
    """Rows in list order: deletions first (oldest ended first), then warnings."""
    from services.lifecycle_facts import _owner_label, service_title

    statement = (
        select(LifecycleBatchItem, PasarGuardAccount, User)
        .join(PasarGuardAccount, PasarGuardAccount.id == LifecycleBatchItem.account_id)
        .join(User, User.id == PasarGuardAccount.user_id)
        .where(LifecycleBatchItem.batch_id == int(batch_id))
        .order_by(
            LifecycleBatchItem.action.asc(),  # delete before warn
            LifecycleBatchItem.reason_days.desc(),
            LifecycleBatchItem.account_id.asc(),
        )
        .offset(offset)
    )
    if limit is not None:
        statement = statement.limit(limit)
    return [
        BatchRow(
            account_id=int(item.account_id),
            action=item.action,
            reason_code=item.reason_code,
            reason_days=item.reason_days,
            after_warning=bool(item.after_warning),
            title=service_title(account),
            owner=_owner_label(user),
            deleted=account.deleted_at is not None,
        )
        for item, account, user in (await session.execute(statement)).all()
    ]


async def batch_account_ids(session: AsyncSession, batch_id: int) -> list[int]:
    return [
        int(a)
        for a in (
            await session.execute(
                select(LifecycleBatchItem.account_id)
                .where(
                    LifecycleBatchItem.batch_id == int(batch_id),
                    LifecycleBatchItem.action == ITEM_DELETE,
                )
                .order_by(LifecycleBatchItem.id)
            )
        )
        .scalars()
        .all()
    ]


async def batch_items(session: AsyncSession, batch_id: int) -> list[tuple[int, str]]:
    """``(account id, action)`` of every item: deletions are deleted, warnings are warned."""
    return [
        (int(a), str(action))
        for a, action in (
            await session.execute(
                select(LifecycleBatchItem.account_id, LifecycleBatchItem.action)
                .where(LifecycleBatchItem.batch_id == int(batch_id))
                .order_by(LifecycleBatchItem.id)
            )
        ).all()
    ]


async def claim_batch(session: AsyncSession, batch_id: int, *, now: datetime) -> bool:
    """Mark it used. True only for the one caller that did; the caller commits at once."""
    result = await session.execute(
        update(LifecycleBatch)
        .where(LifecycleBatch.id == int(batch_id), LifecycleBatch.status == BATCH_OPEN)
        .values(status=BATCH_USED, used_at=now)
    )
    return int(result.rowcount or 0) == 1  # type: ignore[attr-defined]

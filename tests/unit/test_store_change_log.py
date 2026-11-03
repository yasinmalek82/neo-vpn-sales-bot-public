"""«تغییرات فروشگاه» — the change log and its undo.

The log is built from the version chain rather than a separate audit table,
because each published version already *is* the state after one edit. A
parallel table would be a second source of truth free to disagree with the
catalog it claims to describe.

What these tests defend is the honesty of the screen: undo restores a whole
snapshot, so reverting an older change reverts everything after it too. The
count the screen shows must be right, or the button is lying.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from database.base import Base
from services.store_admin import (
    StoreAdminError,
    commit_and_activate,
    create_draft_category,
    create_draft_package,
    delete_draft_package,
    get_catalog_view,
    update_draft_package,
)
from services.store_strategy import list_store_changes, undo_store_change


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _plan(session: AsyncSession, category_id: int, title: str, price: int) -> None:
    await create_draft_package(
        session,
        category_id=category_id,
        title_fa=title,
        fixed_price_toman=price,
        options={"volume_gb": 50, "duration_days": 30},
    )


async def _shop_with_two_plans(session: AsyncSession) -> int:
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="گروه محصول ساخته شد")
    view = await get_catalog_view(session)
    cat_id = view.categories[0].id

    await _plan(session, cat_id, "۵۰ گیگ / ۳۰ روز", 150_000)
    await commit_and_activate(session, notes="پلن «۵۰ گیگ / ۳۰ روز» ساخته شد")
    await _plan(session, cat_id, "۱۰۰ گیگ / ۳۰ روز", 250_000)
    await commit_and_activate(session, notes="پلن «۱۰۰ گیگ / ۳۰ روز» ساخته شد")
    return cat_id


@pytest.mark.asyncio
async def test_the_log_reads_back_what_was_done(session: AsyncSession) -> None:
    await _shop_with_two_plans(session)

    entries = await list_store_changes(session)
    assert [e.description for e in entries] == [
        "پلن «۱۰۰ گیگ / ۳۰ روز» ساخته شد",
        "پلن «۵۰ گیگ / ۳۰ روز» ساخته شد",
        "گروه محصول ساخته شد",
    ], "newest first, in the owner's own words"
    assert entries[0].is_current is True
    assert all(not e.is_current for e in entries[1:])


@pytest.mark.asyncio
async def test_an_edit_with_no_description_still_gets_a_row(
    session: AsyncSession,
) -> None:
    """A call site that forgot its note must not produce a blank button."""
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session)

    entries = await list_store_changes(session)
    assert entries[0].description == "تغییر در فروشگاه"


@pytest.mark.asyncio
async def test_undoes_also_counts_the_changes_that_ride_along(
    session: AsyncSession,
) -> None:
    """This number is shown to the owner before they confirm.

    Undo restores a snapshot, so taking back the first change also takes back
    the two after it. If this count were wrong the confirmation would mislead.
    """
    await _shop_with_two_plans(session)
    entries = await list_store_changes(session)

    assert entries[0].undoes_also == 0, "the newest change takes nothing with it"
    assert entries[1].undoes_also == 1
    assert entries[2].undoes_also == 2


@pytest.mark.asyncio
async def test_undo_puts_the_catalog_back_and_is_live(session: AsyncSession) -> None:
    cat_id = await _shop_with_two_plans(session)
    view = await get_catalog_view(session)
    assert len(view.packages) == 2

    entries = await list_store_changes(session)
    await undo_store_change(session, entries[0].version_id)
    await commit_and_activate(session, notes=f"برگشت: {entries[0].description}")

    view = await get_catalog_view(session)
    assert view.is_draft is False, "undo must publish, not strand a draft"
    assert [p.title_fa for p in view.packages_of(cat_id)] == ["۵۰ گیگ / ۳۰ روز"]


@pytest.mark.asyncio
async def test_undo_is_itself_an_undoable_entry(session: AsyncSession) -> None:
    """Otherwise the log stops matching what the catalog has been through."""
    cat_id = await _shop_with_two_plans(session)
    entries = await list_store_changes(session)
    await undo_store_change(session, entries[0].version_id)
    await commit_and_activate(session, notes=f"برگشت: {entries[0].description}")

    entries = await list_store_changes(session)
    assert entries[0].description.startswith("برگشت:")

    # Undo the undo — the second plan comes back.
    await undo_store_change(session, entries[0].version_id)
    await commit_and_activate(session, notes=f"برگشت: {entries[0].description}")

    view = await get_catalog_view(session)
    titles = sorted(p.title_fa for p in view.packages_of(cat_id))
    assert titles == ["۱۰۰ گیگ / ۳۰ روز", "۵۰ گیگ / ۳۰ روز"]


@pytest.mark.asyncio
async def test_undoing_a_deletion_brings_the_plan_back(session: AsyncSession) -> None:
    """The reason the owner asked for this feature."""
    cat_id = await _shop_with_two_plans(session)
    view = await get_catalog_view(session)
    doomed = next(p for p in view.packages if p.title_fa == "۵۰ گیگ / ۳۰ روز")

    await delete_draft_package(session, doomed.id)
    await commit_and_activate(session, notes="پلن «۵۰ گیگ / ۳۰ روز» حذف شد")
    view = await get_catalog_view(session)
    assert [p.title_fa for p in view.packages_of(cat_id)] == ["۱۰۰ گیگ / ۳۰ روز"]

    entries = await list_store_changes(session)
    assert entries[0].description == "پلن «۵۰ گیگ / ۳۰ روز» حذف شد"
    await undo_store_change(session, entries[0].version_id)
    await commit_and_activate(session, notes="برگشت")

    view = await get_catalog_view(session)
    assert sorted(p.title_fa for p in view.packages_of(cat_id)) == [
        "۱۰۰ گیگ / ۳۰ روز",
        "۵۰ گیگ / ۳۰ روز",
    ]


@pytest.mark.asyncio
async def test_undoing_a_price_change_restores_the_old_price(
    session: AsyncSession,
) -> None:
    cat_id = await _shop_with_two_plans(session)
    view = await get_catalog_view(session)
    target = next(p for p in view.packages if p.title_fa == "۵۰ گیگ / ۳۰ روز")

    await update_draft_package(session, target.id, fixed_price_toman=180_000)
    await commit_and_activate(session, notes="پلن ویرایش شد")

    entries = await list_store_changes(session)
    await undo_store_change(session, entries[0].version_id)
    await commit_and_activate(session, notes="برگشت: پلن ویرایش شد")

    view = await get_catalog_view(session)
    prices = {p.title_fa: p.fixed_price_toman for p in view.packages_of(cat_id)}
    assert prices["۵۰ گیگ / ۳۰ روز"] == 150_000


@pytest.mark.asyncio
async def test_the_very_first_state_cannot_be_undone(session: AsyncSession) -> None:
    """There is nothing before it, and the message must say that plainly."""
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="گروه محصول ساخته شد")

    entries = await list_store_changes(session)
    assert len(entries) == 1
    with pytest.raises(StoreAdminError, match="اولین"):
        await undo_store_change(session, entries[0].version_id)


@pytest.mark.asyncio
async def test_undoing_a_change_that_was_pruned_away_is_refused(
    session: AsyncSession,
) -> None:
    await _shop_with_two_plans(session)
    entries = await list_store_changes(session)
    stale_id = entries[0].version_id + 9999

    with pytest.raises(StoreAdminError):
        await undo_store_change(session, stale_id)


@pytest.mark.asyncio
async def test_the_newest_change_is_undoable(session: AsyncSession) -> None:
    """The one the owner reaches for most.

    `can_undo` first read «not is_current or undoes_also > 0», which is exactly
    backwards: the newest entry *is* the current state, so the button that
    matters most was the one that never rendered.
    """
    await _shop_with_two_plans(session)
    entries = await list_store_changes(session)

    assert entries[0].is_current is True
    assert entries[0].can_undo is True, "undoing the last thing you did must be possible"
    assert all(e.can_undo for e in entries[:-1])
    assert entries[-1].can_undo is False, "nothing exists before the first state"


@pytest.mark.asyncio
async def test_the_oldest_surviving_entry_is_undoable_when_pruning_cut_deeper(
    session: AsyncSession,
) -> None:
    """With a limit or after pruning, the oldest *shown* is not the oldest *kept*."""
    await _shop_with_two_plans(session)
    page = await list_store_changes(session, limit=2)

    assert len(page) == 2
    assert all(
        e.can_undo for e in page
    ), "an older state still exists below this page, so every row can go back"

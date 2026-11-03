"""T-008: category duration admin + ویژه/اقتصادی switch write-path guards."""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database.base import Base
from database.models.store import StoreCategory, StorePackagePrice
from services.store_admin import (
    StoreAdminError,
    add_category_duration,
    commit_and_activate,
    create_draft_category,
    create_draft_package,
    get_draft_category,
    get_or_create_draft,
    remove_category_duration,
    set_category_premium_panel_groups,
    set_category_tiers_enabled,
)
from services.store_pricing import DEFAULT_TIER, available_durations, category_durations, get_price


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


_ASCII_LETTER = re.compile(r"[A-Za-z]")


def _assert_persian_notes(notes: str) -> None:
    assert notes and notes.strip()
    assert _ASCII_LETTER.search(notes) is None, notes


@pytest.mark.asyncio
async def test_adding_zero_months_refused_and_no_zero_in_durations(
    session: AsyncSession,
) -> None:
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    cat_id = int(cat.id)
    with pytest.raises(StoreAdminError):
        await add_category_duration(session, cat_id, months=0)
    await session.rollback()
    await get_or_create_draft(session)
    # Re-fetch after rollback: recreate clean draft category state
    # The raise happens before flush of a bad value; still assert globally.
    cats = (await session.execute(select(StoreCategory))).scalars().all()
    for c in cats:
        assert 0 not in category_durations(c), category_durations(c)
    # And a successful path never writes 0 either
    cat = await create_draft_category(session, title_fa="حجمی۲", kind="volume")
    await add_category_duration(session, int(cat.id), months=2)
    await commit_and_activate(session, notes="مدت ۲ ماهه به «حجمی۲» اضافه شد")
    cats = (await session.execute(select(StoreCategory))).scalars().all()
    for c in cats:
        assert 0 not in list((c.sales_flags or {}).get("durations") or []), c.sales_flags


@pytest.mark.asyncio
async def test_twenty_five_months_refused_twenty_four_accepted(
    session: AsyncSession,
) -> None:
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    cat_id = int(cat.id)
    with pytest.raises(StoreAdminError):
        await add_category_duration(session, cat_id, months=25)
    cat = await add_category_duration(session, cat_id, months=24)
    assert 720 in category_durations(cat)
    published = await commit_and_activate(session, notes="مدت ۲۴ ماهه به «حجمی» اضافه شد")
    assert published is not None
    _assert_persian_notes(published.notes or "")


@pytest.mark.asyncio
async def test_none_duration_adds_and_duplicate_refused(session: AsyncSession) -> None:
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    cat_id = int(cat.id)
    cat = await add_category_duration(session, cat_id, months=None)
    assert None in category_durations(cat)
    with pytest.raises(StoreAdminError):
        await add_category_duration(session, cat_id, months=None)
    published = await commit_and_activate(session, notes="مدت بدون انقضا به «حجمی» اضافه شد")
    _assert_persian_notes(published.notes or "")


@pytest.mark.asyncio
async def test_insertion_order_preserved_not_sorted(session: AsyncSession) -> None:
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    cat_id = int(cat.id)
    # default already has 30; remove path: start by adding 60 then 30 duplicate?
    # Default is [30]. Add 60 → [30, 60]. We need [60, 30]: clear via flags write
    # through add only — remove 30 first after adding another so we can rebuild.
    await add_category_duration(session, cat_id, months=2)  # 60
    await remove_category_duration(session, cat_id, duration_days=30)
    # remaining duration is 60 days only
    await add_category_duration(session, cat_id, months=1)  # 30
    cat = await get_draft_category(session, cat_id)
    assert category_durations(cat) == [60, 30]
    published = await commit_and_activate(session, notes="مدت ۱ ماهه به «حجمی» اضافه شد")
    _assert_persian_notes(published.notes or "")


@pytest.mark.asyncio
async def test_delete_duration_keeps_price_cells_readd_shows(
    session: AsyncSession,
) -> None:
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    cat_id = int(cat.id)
    await add_category_duration(session, cat_id, months=2)  # 60
    pkg = await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="۳۰ گیگ",
        fixed_price_toman=100_000,
        volume_gb=30,
        duration_days=30,
    )
    # Extra cell at 60 days (shelf tab), not the plan's own duration
    session.add(
        StorePackagePrice(
            store_version_id=int(pkg.store_version_id),
            package_slug=str(pkg.slug),
            duration_days=60,
            tier=DEFAULT_TIER,
            price_toman=200_000,
        )
    )
    await session.flush()
    version_id = int(pkg.store_version_id)
    slug = str(pkg.slug)

    cat = await remove_category_duration(session, cat_id, duration_days=60)
    assert 60 not in category_durations(cat)
    rows = (
        (
            await session.execute(
                select(StorePackagePrice).where(
                    StorePackagePrice.store_version_id == version_id,
                    StorePackagePrice.package_slug == slug,
                    StorePackagePrice.duration_days == 60,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert int(rows[0].price_toman) == 200_000

    prices = list(
        (
            await session.execute(
                select(StorePackagePrice).where(
                    StorePackagePrice.store_version_id == version_id,
                    StorePackagePrice.package_slug == slug,
                )
            )
        )
        .scalars()
        .all()
    )
    assert available_durations(prices, slug=slug, tier=DEFAULT_TIER, category=cat) == [30]

    cat = await add_category_duration(session, cat_id, months=2)
    assert available_durations(prices, slug=slug, tier=DEFAULT_TIER, category=cat) == [
        30,
        60,
    ]
    assert get_price(prices, slug=slug, duration_days=60, tier=DEFAULT_TIER) == 200_000
    published = await commit_and_activate(session, notes="مدت ۲ ماهه به «حجمی» اضافه شد")
    _assert_persian_notes(published.notes or "")


@pytest.mark.asyncio
async def test_delete_last_duration_refused(session: AsyncSession) -> None:
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    cat_id = int(cat.id)
    assert category_durations(cat) == [30]
    with pytest.raises(StoreAdminError):
        await remove_category_duration(session, cat_id, duration_days=30)
    cat = await get_draft_category(session, cat_id)
    assert category_durations(cat) == [30]


@pytest.mark.asyncio
async def test_tiers_on_refused_without_premium_groups(session: AsyncSession) -> None:
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    cat_id = int(cat.id)
    with pytest.raises(StoreAdminError) as ei:
        await set_category_tiers_enabled(session, cat_id, enabled=True)
    assert "گروه‌های پنل ویژه" in str(ei.value)
    cat = await get_draft_category(session, cat_id)
    assert not bool((cat.sales_flags or {}).get("tiers_enabled"))


@pytest.mark.asyncio
async def test_clearing_premium_groups_disables_tiers(session: AsyncSession) -> None:
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    cat_id = int(cat.id)
    cat, disabled = await set_category_premium_panel_groups(session, cat_id, "10,20")
    assert not disabled
    cat = await set_category_tiers_enabled(session, cat_id, enabled=True)
    assert bool((cat.sales_flags or {}).get("tiers_enabled"))
    cat, disabled = await set_category_premium_panel_groups(session, cat_id, "")
    assert disabled is True
    assert not bool((cat.sales_flags or {}).get("tiers_enabled"))
    assert list((cat.sales_flags or {}).get("premium_panel_group_ids") or []) == []
    published = await commit_and_activate(session, notes="گروه‌های پنل ویژه ویرایش شد")
    _assert_persian_notes(published.notes or "")


@pytest.mark.asyncio
async def test_commit_notes_are_persian_without_ascii(session: AsyncSession) -> None:
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    cat_id = int(cat.id)
    await add_category_duration(session, cat_id, months=3)
    from services.store_admin import duration_label_fa

    label = duration_label_fa(90)
    notes = f"مدت {label} به «حجمی» اضافه شد"
    published = await commit_and_activate(session, notes=notes)
    assert published is not None
    _assert_persian_notes(published.notes or "")
    await get_or_create_draft(session)
    await remove_category_duration(session, cat_id, duration_days=90)
    label = duration_label_fa(90)
    notes = f"مدت {label} از «حجمی» برداشته شد"
    published = await commit_and_activate(session, notes=notes)
    assert published is not None
    _assert_persian_notes(published.notes or "")

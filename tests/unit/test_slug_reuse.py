"""Slug reuse after delete must not inherit dead prices (money bug)."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database.base import Base
from database.models.store import StorePackagePrice
from database.models.user import User
from services.reseller import get_plan_price, set_plan_price
from services.store_admin import (
    commit_and_activate,
    create_draft_category,
    create_draft_package,
    delete_draft_category,
    delete_draft_package,
    get_or_create_draft,
    list_draft_categories,
    list_draft_packages,
)


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _cells_for(session: AsyncSession, *, version_id: int, slug: str) -> list[tuple]:
    rows = await session.execute(
        select(StorePackagePrice).where(
            StorePackagePrice.store_version_id == version_id,
            StorePackagePrice.package_slug == slug,
        )
    )
    return [(r.duration_days, r.tier, int(r.price_toman)) for r in rows.scalars().all()]


@pytest.mark.asyncio
async def test_a_new_plan_does_not_inherit_a_deleted_plans_slug_or_prices(
    session: AsyncSession,
) -> None:
    """Reproduce: delete ۴۰ گیگ, create ۷۰ گیگ — must not get volume-plan-2 + old cells."""
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    first = await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۳۰ گیگ",
        fixed_price_toman=200_000,
        volume_gb=30,
        duration_days=30,
    )
    second = await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۴۰ گیگ",
        fixed_price_toman=265_000,
        volume_gb=40,
        duration_days=30,
    )
    dead_slug = second.slug
    assert dead_slug != first.slug
    # Publish so the dead slug stays in store_packages history (real admin flow).
    await commit_and_activate(session, notes="seed two plans")

    await get_or_create_draft(session)
    doomed = next(p for p in await list_draft_packages(session) if p.slug == dead_slug)
    await delete_draft_package(session, doomed.id)

    cats = await list_draft_categories(session)
    cat_id = next(c.id for c in cats if c.slug == "volume")
    third = await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="۷۰ گیگ",
        fixed_price_toman=450_000,
        volume_gb=70,
        duration_days=60,
    )
    assert third.slug != dead_slug
    cells = await _cells_for(session, version_id=int(third.store_version_id), slug=third.slug)
    assert cells == [(60, "economy", 450_000)]
    assert not await _cells_for(session, version_id=int(third.store_version_id), slug=dead_slug)


@pytest.mark.asyncio
async def test_reseller_agreed_price_blocks_slug_reuse(session: AsyncSession) -> None:
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    cat_id = int(cat.id)
    pkg = await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="۴۰ گیگ",
        fixed_price_toman=265_000,
        volume_gb=40,
        duration_days=30,
    )
    dead_slug = pkg.slug
    shop = User(telegram_id=9001, referral_code="SHOP9001", is_reseller=True)
    session.add(shop)
    await session.flush()
    await set_plan_price(session, shop.id, dead_slug, 200_000, duration_days=30, tier="economy")

    await delete_draft_package(session, pkg.id)

    # First mint after delete — without the history-wide taken set this is handed
    # the dead slug and silently inherits the reseller's agreed price.
    newborn = await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="۷۰ گیگ",
        fixed_price_toman=450_000,
        volume_gb=70,
        duration_days=60,
    )
    assert newborn.slug != dead_slug
    assert (
        await get_plan_price(session, shop.id, newborn.slug, duration_days=30, tier="economy")
        is None
    )
    assert (
        await get_plan_price(session, shop.id, dead_slug, duration_days=30, tier="economy")
        == 200_000
    )


@pytest.mark.asyncio
async def test_deleting_a_draft_plan_does_not_erase_published_price_cells(
    session: AsyncSession,
) -> None:
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    pkg = await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۴۰ گیگ",
        fixed_price_toman=265_000,
        volume_gb=40,
        duration_days=30,
    )
    slug = pkg.slug
    published = await commit_and_activate(session, notes="publish before delete")
    assert published is not None
    published_id = int(published.id)
    before = await _cells_for(session, version_id=published_id, slug=slug)
    assert before == [(30, "economy", 265_000)]

    draft = await get_or_create_draft(session)
    draft_id = int(draft.id)
    doomed = next(p for p in await list_draft_packages(session) if p.slug == slug)
    await delete_draft_package(session, doomed.id)

    after = await _cells_for(session, version_id=published_id, slug=slug)
    assert after == before
    assert await _cells_for(session, version_id=draft_id, slug=slug) == []


@pytest.mark.asyncio
async def test_a_deleted_category_slug_is_not_recycled(session: AsyncSession) -> None:
    first = await create_draft_category(session, title_fa="حجمی", kind="volume")
    second = await create_draft_category(session, title_fa="حجمی ویژه", kind="volume")
    dead = second.slug
    assert dead != first.slug
    await commit_and_activate(session, notes="seed categories")

    await get_or_create_draft(session)
    doomed = next(c for c in await list_draft_categories(session) if c.slug == dead)
    await delete_draft_category(session, doomed.id)

    third = await create_draft_category(session, title_fa="حجمی جدید", kind="volume")
    assert third.slug != dead

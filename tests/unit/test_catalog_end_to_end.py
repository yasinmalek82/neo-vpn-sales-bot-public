"""The owner's actual scenario, end to end.

«حجمی» holding «ویژه» and «اقتصادی», plus a flat «نامحدود» — built, priced,
published, and then read back the way the customer purchase flow reads it.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from database.base import Base
from services.store_admin import (
    create_draft_category,
    create_draft_package,
    publish_draft,
    update_draft_category,
)
from services.store_config import get_published_store_config


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _build_owner_catalog(session: AsyncSession) -> dict[str, int]:
    volume = await create_draft_category(session, title_fa="حجمی", kind="volume")
    premium = await create_draft_category(
        session, title_fa="ویژه", kind="volume", parent_id=volume.id
    )
    economy = await create_draft_category(
        session, title_fa="اقتصادی", kind="volume", parent_id=volume.id
    )
    unlimited = await create_draft_category(session, title_fa="نامحدود", kind="unlimited")

    # Better servers are what makes «ویژه» worth more.
    await update_draft_category(session, premium.id, panel_group_ids="11")
    await update_draft_category(session, economy.id, panel_group_ids="22")

    await create_draft_package(
        session, category_id=premium.id, title_fa="۵۰ گیگ / ۱ ماه", fixed_price_toman=150_000
    )
    await create_draft_package(
        session, category_id=premium.id, title_fa="۱۰۰ گیگ / ۳ ماه", fixed_price_toman=390_000
    )
    await create_draft_package(
        session, category_id=economy.id, title_fa="۵۰ گیگ / ۱ ماه", fixed_price_toman=90_000
    )
    await create_draft_package(
        session, category_id=unlimited.id, title_fa="۱ کاربره / ۱ ماه", fixed_price_toman=250_000
    )
    await session.commit()
    return {
        "volume": volume.id,
        "premium": premium.id,
        "economy": economy.id,
        "unlimited": unlimited.id,
    }


def _sellable_children(cfg, parent_id: int | None) -> list:
    """Mirrors ``_published_sale_categories`` in the purchase handler."""
    return [
        c
        for c in sorted(cfg.categories, key=lambda c: (c.sort_order, c.id))
        if c.enabled
        and dict(c.sales_flags or {}).get("allow_new_sale", True)
        and c.parent_id == parent_id
    ]


@pytest.mark.asyncio
async def test_owner_builds_and_publishes_the_whole_catalog(
    session: AsyncSession,
) -> None:
    ids = await _build_owner_catalog(session)
    await publish_draft(session)
    await session.commit()

    cfg = await get_published_store_config(session)
    assert cfg is not None

    # Top level: only «حجمی» and «نامحدود» — children must not appear as peers.
    top = _sellable_children(cfg, None)
    assert [c.title_fa for c in top] == ["حجمی", "نامحدود"]

    # «حجمی» expands one level.
    volume_children = _sellable_children(cfg, ids["volume"])
    assert [c.title_fa for c in volume_children] == ["ویژه", "اقتصادی"]

    # «نامحدود» is flat, so the customer pays no extra tap for it.
    assert _sellable_children(cfg, ids["unlimited"]) == []


@pytest.mark.asyncio
async def test_published_prices_are_what_the_owner_typed(session: AsyncSession) -> None:
    ids = await _build_owner_catalog(session)
    await publish_draft(session)
    await session.commit()

    cfg = await get_published_store_config(session)
    assert cfg is not None
    by_cat: dict[int, list[int]] = {}
    for pkg in cfg.packages:
        by_cat.setdefault(pkg.category_id, []).append(int(pkg.fixed_price_toman or 0))

    assert sorted(by_cat[ids["premium"]]) == [150_000, 390_000]
    assert by_cat[ids["economy"]] == [90_000]
    assert by_cat[ids["unlimited"]] == [250_000]
    # The parent group holds child groups, so it carries no plans of its own.
    assert ids["volume"] not in by_cat


@pytest.mark.asyncio
async def test_a_price_change_reaches_customers_only_after_publish(
    session: AsyncSession,
) -> None:
    """The draft→publish boundary is the whole point of versioning."""
    from services.store_admin import (
        list_draft_categories,
        list_draft_packages,
        update_draft_package,
    )

    async def live_price(group_title: str) -> int:
        """Price a customer would see right now, looked up by group title.

        Ids are not stable across a publish — cloning a published version into a
        new draft creates fresh rows — so the whole test works by title.
        """
        cfg = await get_published_store_config(session)
        assert cfg is not None
        group = next(c for c in cfg.categories if c.title_fa == group_title)
        pkg = next(p for p in cfg.packages if p.category_id == group.id)
        return int(pkg.fixed_price_toman or 0)

    await _build_owner_catalog(session)
    await publish_draft(session)
    await session.commit()
    assert await live_price("اقتصادی") == 90_000

    draft_economy = next(c for c in await list_draft_categories(session) if c.title_fa == "اقتصادی")
    economy_plan = next(
        p for p in await list_draft_packages(session) if p.category_id == draft_economy.id
    )
    await update_draft_package(session, economy_plan.id, fixed_price_toman=95_000)
    await session.commit()

    assert await live_price("اقتصادی") == 90_000, "draft edits must not leak to customers"

    await publish_draft(session)
    await session.commit()
    assert await live_price("اقتصادی") == 95_000

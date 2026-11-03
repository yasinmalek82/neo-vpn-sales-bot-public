"""Price matrix: pure lookups, clone, and no leaked fixed_price reads."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from database.models.store import StorePackageKind
from services.store_admin import (
    commit_and_activate,
    create_draft_package,
    get_or_create_draft,
    update_draft_package,
)
from services.store_config import get_published_store_config
from services.store_pricing import (
    DEFAULT_TIER,
    PREMIUM_TIER,
    available_durations,
    category_tiers,
    get_price,
    is_buyable,
)


@pytest.fixture
async def session():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import database.models  # noqa: F401
    from database.base import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _price(**kwargs: Any) -> Any:
    return SimpleNamespace(**kwargs)


def test_get_price_exact_and_missing_are_none_not_falsy() -> None:
    rows = [
        _price(package_slug="a", duration_days=30, tier=DEFAULT_TIER, price_toman=0),
        _price(package_slug="a", duration_days=60, tier=DEFAULT_TIER, price_toman=100),
    ]
    assert get_price(rows, slug="a", duration_days=30, tier=DEFAULT_TIER) == 0
    assert get_price(rows, slug="a", duration_days=90, tier=DEFAULT_TIER) is None


def test_duration_none_is_distinct_from_zero() -> None:
    rows = [
        _price(package_slug="a", duration_days=None, tier=DEFAULT_TIER, price_toman=50),
    ]
    assert get_price(rows, slug="a", duration_days=None, tier=DEFAULT_TIER) == 50
    assert get_price(rows, slug="a", duration_days=0, tier=DEFAULT_TIER) is None


def test_category_tiers_respects_switch() -> None:
    assert category_tiers(SimpleNamespace(sales_flags={})) == [DEFAULT_TIER]
    assert category_tiers(SimpleNamespace(sales_flags={"tiers_enabled": False})) == [DEFAULT_TIER]
    assert category_tiers(SimpleNamespace(sales_flags={"tiers_enabled": True})) == [
        DEFAULT_TIER,
        PREMIUM_TIER,
    ]


def test_available_durations_follow_category_order_and_omit_empty() -> None:
    cat = SimpleNamespace(sales_flags={"durations": [60, 30, None]})
    rows = [
        _price(package_slug="a", duration_days=30, tier=DEFAULT_TIER, price_toman=1),
        _price(package_slug="a", duration_days=None, tier=DEFAULT_TIER, price_toman=2),
    ]
    assert available_durations(rows, slug="a", tier=DEFAULT_TIER, category=cat) == [30, None]


def test_is_buyable_false_when_only_switched_off_tier_has_price() -> None:
    pkg = SimpleNamespace(
        kind=StorePackageKind.ready,
        enabled=True,
        allow_new_sale=True,
        slug="a",
    )
    rows = [
        _price(package_slug="a", duration_days=30, tier=PREMIUM_TIER, price_toman=10),
    ]
    assert is_buyable(pkg, rows, tier=DEFAULT_TIER) is False
    assert is_buyable(pkg, rows, tier=PREMIUM_TIER) is True


@pytest.mark.asyncio
async def test_clone_copies_prices_and_leaves_prior_version_untouched(
    session: AsyncSession,
) -> None:
    from services.store_admin import create_draft_category

    await get_or_create_draft(session)
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    pkg = await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۵۰ گیگ / ۳۰ روز",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
    )
    await commit_and_activate(session, notes="seed price matrix clone test")

    pub = await get_published_store_config(session)
    assert pub is not None
    assert len(pub.prices) >= 1
    cell = get_price(pub.prices, slug=pkg.slug, duration_days=30, tier=DEFAULT_TIER)
    assert cell == 150_000
    first_version_id = pub.version.id
    first_prices = [
        (p.package_slug, p.duration_days, p.tier, int(p.price_toman)) for p in pub.prices
    ]

    # Edit and publish again — prior version rows must stay put.
    draft2 = await get_or_create_draft(session)
    # find package in new draft by slug
    pkg2 = next(p for p in draft2.packages if p.slug == pkg.slug)
    await update_draft_package(session, pkg2.id, fixed_price_toman=160_000)
    await commit_and_activate(session, notes="bump price")

    pub2 = await get_published_store_config(session)
    assert pub2 is not None
    assert pub2.version.id != first_version_id
    assert get_price(pub2.prices, slug=pkg.slug, duration_days=30, tier=DEFAULT_TIER) == 160_000

    # Reload first version prices via SQL
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from database.models.store import StoreVersion

    v1 = await session.scalar(
        select(StoreVersion)
        .where(StoreVersion.id == first_version_id)
        .options(selectinload(StoreVersion.package_prices))
    )
    assert v1 is not None
    still = [
        (p.package_slug, p.duration_days, p.tier, int(p.price_toman)) for p in v1.package_prices
    ]
    assert still == first_prices


def test_nothing_outside_store_pricing_reads_fixed_price() -> None:
    """Only services/store_admin.py (writes/clone) may touch the column attr."""
    roots = [Path("bot"), Path("services"), Path("scheduler")]
    allowed = {Path("services/store_admin.py").resolve()}
    offenders: list[str] = []
    for root in roots:
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "fixed_price_toman"
                    and path.resolve() not in allowed
                ):
                    offenders.append(f"{path}:{node.lineno}")
    assert offenders == []

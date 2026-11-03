"""SIMPLE catalog UX: volume time-limit filter + admin persistence."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

import database.models  # noqa: F401
from database.base import Base
from database.models.store import StorePackage, StorePackageKind, StoreVersion
from services.store_admin import (
    create_draft_category,
    get_or_create_draft,
    update_draft_category,
    update_draft_package,
)
from services.store_catalog import (
    category_kind,
    custom_purchase_mode,
    filter_ready_plans_by_time_limit,
    is_volume_like,
    package_has_time_limit,
    time_limit_filter_enabled,
)
from services.store_config import get_published_store_config, seed_store_from_pricing_json
from services.store_strategy import PURCHASE_STEP_CATALOG


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def pricing_path(tmp_path: Path) -> Path:
    src = Path("config/pricing.json")
    path = tmp_path / "pricing.json"
    path.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return path


async def _seed_published(session: AsyncSession, pricing_path: Path):
    seeded = await seed_store_from_pricing_json(session, pricing_path, publish=True)
    assert seeded is not None
    await session.commit()
    cfg = await get_published_store_config(session)
    assert cfg is not None
    return cfg


def _ready(
    *,
    title: str,
    has_tl: bool,
    category_id: int,
    store_version_id: int,
    sort: int = 10,
) -> StorePackage:
    return StorePackage(
        store_version_id=store_version_id,
        category_id=category_id,
        slug=f"ready-{sort}-{has_tl}",
        kind=StorePackageKind.ready,
        title_fa=title,
        enabled=True,
        sort_order=sort,
        fixed_price_toman=100_000,
        pricing_ref=None,
        options={"volume_gb": 50, "duration_days": 30, "has_time_limit": has_tl},
        forbid_extra_discount=False,
        allow_new_sale=True,
        allow_renew=True,
        sales_flags={"has_time_limit": has_tl},
    )


@pytest.mark.asyncio
async def test_seed_sets_kind_and_filter_default_off(
    session: AsyncSession, pricing_path: Path
) -> None:
    cfg = await _seed_published(session, pricing_path)
    vol = next(c for c in cfg.categories if c.slug == "volume")
    unl = next(c for c in cfg.categories if c.slug == "unlimited")
    assert category_kind(vol) == "volume"
    assert category_kind(unl) == "unlimited"
    assert vol.sales_flags.get("kind") == "volume"
    assert time_limit_filter_enabled(vol) is False
    assert time_limit_filter_enabled(unl) is False


@pytest.mark.asyncio
async def test_filter_on_filters_ready_plans(session: AsyncSession, pricing_path: Path) -> None:
    """T-006 removed want_time_limit; helper now returns all ready plans sorted."""
    cfg = await _seed_published(session, pricing_path)
    vol = next(c for c in cfg.categories if c.slug == "volume")
    a = _ready(
        title="با محدودیت",
        has_tl=True,
        category_id=vol.id,
        store_version_id=cfg.version.id,
        sort=1,
    )
    b = _ready(
        title="بدون محدودیت",
        has_tl=False,
        category_id=vol.id,
        store_version_id=cfg.version.id,
        sort=2,
    )
    session.add_all([a, b])
    await session.commit()

    pkgs = [p for p in (list(cfg.packages) + [a, b]) if p.category_id == vol.id]
    all_ready = filter_ready_plans_by_time_limit(pkgs, category=vol)
    assert {p.title_fa for p in all_ready} >= {"با محدودیت", "بدون محدودیت"}


@pytest.mark.asyncio
async def test_filter_off_skips_step(session: AsyncSession, pricing_path: Path) -> None:
    """Admin flag still persists; purchase no longer reads should_show_* (T-006)."""
    cfg = await _seed_published(session, pricing_path)
    vol = next(c for c in cfg.categories if c.slug == "volume")
    assert time_limit_filter_enabled(vol) is False

    draft = await get_or_create_draft(session)
    await session.commit()
    vol_d = next(c for c in draft.categories if category_kind(c) == "volume")
    await update_draft_category(session, vol_d.id, time_limit_filter_enabled=True)
    await session.commit()

    result = await session.execute(
        select(StoreVersion)
        .where(StoreVersion.id == draft.id)
        .options(selectinload(StoreVersion.categories))
    )
    draft_reload = result.scalar_one()
    vol_d2 = next(c for c in draft_reload.categories if c.id == vol_d.id)
    assert time_limit_filter_enabled(vol_d2) is True


@pytest.mark.asyncio
async def test_unlimited_has_no_filter(session: AsyncSession, pricing_path: Path) -> None:
    await _seed_published(session, pricing_path)
    draft = await get_or_create_draft(session)
    await session.commit()
    unl = next(c for c in draft.categories if category_kind(c) == "unlimited")
    await update_draft_category(session, unl.id, time_limit_filter_enabled=True)
    await session.commit()
    await session.refresh(unl)
    assert time_limit_filter_enabled(unl) is False
    assert is_volume_like(unl) is False


@pytest.mark.asyncio
async def test_admin_toggle_persistence(session: AsyncSession, pricing_path: Path) -> None:
    await _seed_published(session, pricing_path)
    draft = await get_or_create_draft(session)
    await session.commit()
    vol = next(c for c in draft.categories if category_kind(c) == "volume")
    await update_draft_category(session, vol.id, time_limit_filter_enabled=True)
    await session.commit()
    await session.refresh(vol)
    assert vol.sales_flags.get("time_limit_filter_enabled") is True
    assert vol.sales_flags.get("kind") == "volume"

    cat = await create_draft_category(session, title_fa="حجمی اقتصادی", kind="volume")
    await session.commit()
    assert cat.slug.startswith("volume")
    assert category_kind(cat) == "volume"
    assert time_limit_filter_enabled(cat) is False

    pkg = StorePackage(
        store_version_id=draft.id,
        category_id=vol.id,
        slug="eco-50",
        kind=StorePackageKind.ready,
        title_fa="۵۰ گیگ ۳۰ روز",
        enabled=True,
        sort_order=50,
        fixed_price_toman=200_000,
        options={"volume_gb": 50, "duration_days": 30},
        sales_flags={},
        forbid_extra_discount=False,
        allow_new_sale=True,
        allow_renew=True,
    )
    session.add(pkg)
    await session.flush()
    # Ensure draft.packages identity collection sees the new row
    await session.refresh(draft, attribute_names=["packages"])
    await update_draft_package(session, pkg.id, has_time_limit=True)
    await session.commit()
    await session.refresh(pkg)
    assert package_has_time_limit(pkg) is True
    assert pkg.sales_flags.get("has_time_limit") is True
    assert pkg.options.get("has_time_limit") is True


def test_volume_time_limit_in_step_catalog() -> None:
    ids = {b["id"] for b in PURCHASE_STEP_CATALOG}
    assert "volume_time_limit" in ids


def test_custom_purchase_defaults_to_everyone_and_reads_the_old_flag():
    assert custom_purchase_mode({"sales_flags": {}}) == "everyone"
    assert custom_purchase_mode({"sales_flags": {"allow_custom_purchase": True}}) == "everyone"
    assert custom_purchase_mode({"sales_flags": {"allow_custom_purchase": False}}) == "off"
    assert custom_purchase_mode(None) == "everyone"


def test_options_keyboard_single_column():
    from bot.keyboards.purchase import PurchaseCB, options_keyboard

    kb = options_keyboard(PurchaseCB.VOL_PREFIX, [30, 50, 100], label_for=lambda g: f"{g} گیگ")
    # each option alone on its row (plus nav row)
    option_rows = [r for r in kb.inline_keyboard if len(r) == 1 and "گیگ" in (r[0].text or "")]
    assert len(option_rows) == 3

"""STORE_CORE: seed, publish, DB pricing loader, owner ids."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from bot.config import Settings
from database.base import Base
from database.models.store import StoreVersion, StoreVersionStatus
from services.pricing import PlanType, calculate_price, load_pricing_config
from services.store_config import (
    get_published_store_config,
    is_store_owner,
    load_pricing_config_async,
    parse_store_owner_telegram_ids,
    seed_store_from_pricing_json,
)


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


@pytest.mark.asyncio
async def test_seed_idempotent_when_published(session: AsyncSession, pricing_path: Path) -> None:
    first = await seed_store_from_pricing_json(session, pricing_path)
    assert first is not None
    assert first.status == StoreVersionStatus.published
    await session.commit()

    second = await seed_store_from_pricing_json(session, pricing_path)
    assert second is None

    versions = (await session.execute(select(StoreVersion))).scalars().all()
    assert len(versions) == 1


@pytest.mark.asyncio
async def test_seed_creates_categories_and_packages(
    session: AsyncSession, pricing_path: Path
) -> None:
    version = await seed_store_from_pricing_json(session, pricing_path)
    assert version is not None
    await session.commit()

    cfg = await get_published_store_config(session)
    assert cfg is not None
    slugs = {c.slug for c in cfg.categories}
    assert slugs == {"volume", "unlimited"}
    pkg_slugs = {p.slug for p in cfg.packages}
    assert pkg_slugs == {"volume_custom", "unlimited_custom"}
    assert all(p.allow_new_sale and p.allow_renew for p in cfg.packages)
    assert cfg.sales_rules["discount_combine"] == "best_of_promo_vs_extra"
    assert cfg.sales_rules["payment_correctness_locked"] is True


@pytest.mark.asyncio
async def test_calculate_price_from_db_matches_json_seed(
    session: AsyncSession, pricing_path: Path
) -> None:
    file_cfg = load_pricing_config(pricing_path)
    await seed_store_from_pricing_json(session, pricing_path)
    await session.commit()

    db_cfg = await load_pricing_config_async(session, path=pricing_path)
    assert db_cfg["_store_config_source"] == "db"

    samples = [
        {"plan_type": PlanType.VOLUME, "volume_gb": 100, "duration_days": 90},
        {"plan_type": PlanType.VOLUME, "volume_gb": 30, "duration_days": 30},
        {"plan_type": PlanType.UNLIMITED, "concurrent_users": 2, "duration_months": 1},
        {"plan_type": PlanType.UNLIMITED, "concurrent_users": 1, "duration_months": 12},
    ]
    for kwargs in samples:
        # Compare engine math without sales_rules combine difference:
        # strip sales_rules from db cfg for apples-to-apples vs bare JSON seed.
        bare_db = {k: v for k, v in db_cfg.items() if not str(k).startswith("_")}
        bare_db.pop("sales_rules", None)
        file_price = calculate_price(file_cfg, **kwargs)
        db_price = calculate_price(bare_db, **kwargs)
        assert db_price.original_price == file_price.original_price
        assert db_price.discount_amount == file_price.discount_amount
        assert db_price.final_price == file_price.final_price


@pytest.mark.asyncio
async def test_no_file_fallback_after_publish(
    session: AsyncSession, pricing_path: Path, tmp_path: Path
) -> None:
    await seed_store_from_pricing_json(session, pricing_path)
    await session.commit()

    # Mutate then delete/rename the seed file — loader must still work from DB.
    mutated = tmp_path / "gone" / "pricing.json"
    # path no longer exists
    missing = tmp_path / "missing-pricing.json"
    assert not missing.exists()

    cfg = await load_pricing_config_async(session, path=missing)
    assert cfg["_store_config_source"] == "db"
    price = calculate_price(
        {k: v for k, v in cfg.items() if k != "sales_rules" and not str(k).startswith("_")},
        plan_type=PlanType.VOLUME,
        volume_gb=50,
        duration_days=30,
    )
    assert price.original_price == 50 * 12000
    assert mutated  # silence unused (documents rename/delete intent)


@pytest.mark.asyncio
async def test_file_bootstrap_before_publish(session: AsyncSession, pricing_path: Path) -> None:
    cfg = await load_pricing_config_async(session, path=pricing_path)
    assert cfg["_store_config_source"] == "file"
    assert "volume_plan" in cfg


def test_owner_ids_config_parse() -> None:
    assert parse_store_owner_telegram_ids("") == []
    assert parse_store_owner_telegram_ids("111, 222") == [111, 222]
    assert parse_store_owner_telegram_ids([3, 4]) == [3, 4]

    settings = Settings(
        BOT_TOKEN="t",
        STORE_OWNER_TELEGRAM_IDS="10,20",
        ADMIN_TELEGRAM_IDS="99",
    )
    assert settings.store_owner_telegram_ids == [10, 20]
    assert is_store_owner(settings, 10) is True
    assert is_store_owner(settings, 99) is False  # admin ≠ owner unless listed
    assert is_store_owner(settings, None) is False


@pytest.mark.asyncio
async def test_best_of_promo_vs_extra_from_seeded_rules(
    session: AsyncSession, pricing_path: Path
) -> None:
    await seed_store_from_pricing_json(session, pricing_path)
    await session.commit()
    cfg = await load_pricing_config_async(session, path=pricing_path)
    # duration 0 (30d), promo 10, extra 5 → best_of → 10 total (not 15)
    price = calculate_price(
        cfg,
        plan_type=PlanType.VOLUME,
        volume_gb=50,
        duration_days=30,
        promo_code="WELCOME10",
        extra_discount_percent=5,
    )
    assert price.meta["discount_combine"] == "best_of_promo_vs_extra"
    assert price.meta["total_discount_percent"] == "10"
    assert price.discount_amount == int(price.original_price * 0.10)

"""Dynamic plans are stamped with the groups of the store group they were bought in."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from bot.config import Settings
from database.base import Base
from services.orders import get_or_create_dynamic_plan
from services.panel_settings import resolve_groups_for_plan_type
from services.pricing import PlanType, PriceResult
from services.store_admin import create_draft_category, update_draft_category


def _settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        BOT_TOKEN="test-token",
        PASARGUARD_BASE_URL="https://env.example.com",
        PASARGUARD_API_KEY="pg_key_envvvvv-envv-4env-8env-envvenvvenvv",
        PASARGUARD_SUB_PATH="env-sub",
        PASARGUARD_TIMEOUT=30.0,
        DEFAULT_PANEL_GROUP_IDS=[1, 2],
        ADMIN_TELEGRAM_IDS=[],
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


@pytest.mark.asyncio
async def test_purchase_stamping_volume_vs_unlimited(session):
    """Each store group stamps its own panel group on the plan (volume != unlimited)."""
    settings = _settings()
    vol_cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    unl_cat = await create_draft_category(session, title_fa="نامحدود", kind="unlimited")
    await update_draft_category(session, vol_cat.id, panel_group_ids="7")
    await update_draft_category(session, unl_cat.id, panel_group_ids="1")
    await session.commit()

    vol_ids = await resolve_groups_for_plan_type(
        session, settings, PlanType.VOLUME, store_category_id=vol_cat.id
    )
    unl_ids = await resolve_groups_for_plan_type(
        session, settings, PlanType.UNLIMITED, store_category_id=unl_cat.id
    )
    assert vol_ids == [7]
    assert unl_ids == [1]

    vol_price = PriceResult(
        original_price=1000,
        discount_amount=0,
        final_price=1000,
        currency="IRT",
        meta={"volume_gb": 50, "duration_days": 30, "plan_type": "volume"},
    )
    unl_price = PriceResult(
        original_price=2000,
        discount_amount=0,
        final_price=2000,
        currency="IRT",
        meta={"concurrent_users": 2, "duration_months": 1, "plan_type": "unlimited"},
    )
    vol_plan = await get_or_create_dynamic_plan(
        session, plan_type=PlanType.VOLUME, price=vol_price, group_ids=vol_ids, hwid_limit=1
    )
    unl_plan = await get_or_create_dynamic_plan(
        session, plan_type=PlanType.UNLIMITED, price=unl_price, group_ids=unl_ids, hwid_limit=2
    )
    await session.commit()
    assert vol_plan.group_ids == [7]
    assert unl_plan.group_ids == [1]
    assert vol_plan.name.startswith("volume-")
    assert unl_plan.name.startswith("unlimited-")

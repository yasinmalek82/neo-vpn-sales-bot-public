"""The category → panel group link, and the rule that no saved toggle is inert.

«ویژه» versus «اقتصادی» is only a real difference to a customer if the two
groups provision onto different panel groups. The category editor wrote
``panel_group_ids`` long before any runtime code read it.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from database.base import Base
from services.panel_settings import (
    resolve_groups_for_plan_type,
)
from services.pricing import PlanType
from services.store_admin import create_draft_category, update_draft_category


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        pasar_guard_base_url="https://panel.example",
        pasar_guard_api_key="pg_key_test",
        pasar_guard_sub_path="sub",
        pasar_guard_timeout=10.0,
        default_panel_group_ids=[900],
    )


@pytest.mark.asyncio
async def test_category_groups_are_the_answer(session: AsyncSession) -> None:
    premium = await create_draft_category(session, title_fa="ویژه", kind="volume")
    await update_draft_category(session, premium.id, panel_group_ids="11,12")
    await session.commit()

    groups = await resolve_groups_for_plan_type(
        session, _settings(), PlanType.VOLUME, store_category_id=premium.id
    )
    assert groups == [11, 12]


@pytest.mark.asyncio
async def test_two_sibling_groups_can_sell_different_servers(
    session: AsyncSession,
) -> None:
    parent = await create_draft_category(session, title_fa="حجمی", kind="volume")
    premium = await create_draft_category(
        session, title_fa="ویژه", kind="volume", parent_id=parent.id
    )
    economy = await create_draft_category(
        session, title_fa="اقتصادی", kind="volume", parent_id=parent.id
    )
    await update_draft_category(session, premium.id, panel_group_ids="11")
    await update_draft_category(session, economy.id, panel_group_ids="22")
    await session.commit()

    settings = _settings()
    assert await resolve_groups_for_plan_type(
        session, settings, PlanType.VOLUME, store_category_id=premium.id
    ) == [11]
    assert await resolve_groups_for_plan_type(
        session, settings, PlanType.VOLUME, store_category_id=economy.id
    ) == [22]


@pytest.mark.asyncio
async def test_a_child_inherits_its_parents_groups(session: AsyncSession) -> None:
    parent = await create_draft_category(session, title_fa="حجمی", kind="volume")
    child = await create_draft_category(
        session, title_fa="ویژه", kind="volume", parent_id=parent.id
    )
    await update_draft_category(session, parent.id, panel_group_ids="7,8")
    await session.commit()

    groups = await resolve_groups_for_plan_type(
        session, _settings(), PlanType.VOLUME, store_category_id=child.id
    )
    assert groups == [7, 8]


@pytest.mark.asyncio
async def test_an_unset_category_resolves_to_nothing(session: AsyncSession) -> None:
    """No product-map or default rescue: the group is simply not for sale."""
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    await session.commit()

    groups = await resolve_groups_for_plan_type(
        session, _settings(), PlanType.VOLUME, store_category_id=cat.id
    )
    assert groups == []


@pytest.mark.asyncio
async def test_no_category_resolves_to_nothing(session: AsyncSession) -> None:
    """A caller with no store group gets [] — the env default is not consulted."""
    groups = await resolve_groups_for_plan_type(session, _settings(), PlanType.VOLUME)
    assert groups == []


@pytest.mark.asyncio
async def test_a_missing_category_resolves_to_nothing(session: AsyncSession) -> None:
    groups = await resolve_groups_for_plan_type(
        session, _settings(), PlanType.VOLUME, store_category_id=999_999
    )
    assert groups == []

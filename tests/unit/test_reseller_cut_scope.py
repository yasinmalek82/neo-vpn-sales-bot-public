"""A reseller percent cut and its undo belong to the screen they were made on.

The undo snapshot is keyed on (duration, tier), and every group has a 30-day
tab. So «٪ از کل این تب کم کن» on one group used to cut the agreed prices of
every group at that duration, a cut on group B deleted group A's undo, and
«برگرداندن» on A then reverted B's cut. Each snapshot is now scoped by the
plans it touched: a cut reaches only the plans on screen, and undo finds the
newest snapshot made of those plans.

Ported from ``feat/plan-ladder`` (9468c9f), whose ladder was later rebuilt as
the flat list («فهرست ساده») on this branch.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm.attributes import flag_modified

import database.models  # noqa: F401
from bot.config import Settings
from bot.handlers import admin as admin_handlers
from bot.keyboards.admin import AdminCB
from database.base import Base
from database.models import User
from services.reseller import (
    apply_percent_cut,
    get_plan_price,
    set_plan_price,
    undo_flat_percent_cut,
    undo_percent_cut,
)
from services.store_admin import (
    commit_and_activate,
    create_draft_category,
    create_draft_package,
    get_catalog_view,
)
from services.store_pricing import DEFAULT_TIER

ADMIN_ID = 5252
T = DEFAULT_TIER


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _shop(session: AsyncSession) -> User:
    shop = User(telegram_id=9300, referral_code="SHOP9300", is_reseller=True)
    session.add(shop)
    await session.flush()
    return shop


async def _price(session: AsyncSession, user_id: int, slug: str, days: int | None) -> int | None:
    return await get_plan_price(session, user_id, slug, duration_days=days, tier=T)


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_cut_on_another_group_keeps_this_groups_undo(session: AsyncSession) -> None:
    shop = await _shop(session)
    await set_plan_price(session, shop.id, "a", 100_000, duration_days=30, tier=T)
    await set_plan_price(session, shop.id, "b", 200_000, duration_days=30, tier=T)
    await apply_percent_cut(
        session, shop.id, duration_days=30, tier=T, pct=10, changes=[("a", 100_000, 90_000)]
    )
    await apply_percent_cut(
        session, shop.id, duration_days=30, tier=T, pct=10, changes=[("b", 200_000, 180_000)]
    )

    restored = await undo_percent_cut(session, shop.id, duration_days=30, tier=T, slugs={"a"})
    assert restored == 1
    assert await _price(session, shop.id, "a", 30) == 100_000
    assert await _price(session, shop.id, "b", 30) == 180_000

    assert await undo_percent_cut(session, shop.id, duration_days=30, tier=T, slugs={"a"}) is None
    assert await undo_percent_cut(session, shop.id, duration_days=30, tier=T, slugs={"b"}) == 1
    assert await _price(session, shop.id, "b", 30) == 200_000


@pytest.mark.asyncio
async def test_a_second_cut_on_the_same_plans_replaces_their_undo(session: AsyncSession) -> None:
    """One step of undo per screen: the older snapshot would restore a price
    that the newer cut has already moved past."""
    shop = await _shop(session)
    await set_plan_price(session, shop.id, "a", 100_000, duration_days=30, tier=T)
    await apply_percent_cut(
        session, shop.id, duration_days=30, tier=T, pct=10, changes=[("a", 100_000, 90_000)]
    )
    await apply_percent_cut(
        session, shop.id, duration_days=30, tier=T, pct=10, changes=[("a", 90_000, 81_000)]
    )
    assert await undo_percent_cut(session, shop.id, duration_days=30, tier=T, slugs={"a"}) == 1
    assert await _price(session, shop.id, "a", 30) == 90_000
    assert await undo_percent_cut(session, shop.id, duration_days=30, tier=T, slugs={"a"}) is None


@pytest.mark.asyncio
async def test_flat_undo_does_not_revert_another_groups_later_cut(
    session: AsyncSession,
) -> None:
    shop = await _shop(session)
    await set_plan_price(session, shop.id, "rung", 100_000, duration_days=30, tier=T)
    await set_plan_price(session, shop.id, "other", 200_000, duration_days=30, tier=T)
    await apply_percent_cut(
        session, shop.id, duration_days=30, tier=T, pct=10, changes=[("rung", 100_000, 90_000)]
    )
    await apply_percent_cut(
        session, shop.id, duration_days=30, tier=T, pct=10, changes=[("other", 200_000, 180_000)]
    )

    restored = await undo_flat_percent_cut(session, shop.id, durations=[30], tier=T, slugs={"rung"})
    assert restored == 1
    assert await _price(session, shop.id, "rung", 30) == 100_000
    assert await _price(session, shop.id, "other", 30) == 180_000


@pytest.mark.asyncio
async def test_flat_undo_reverts_only_the_newest_cut(session: AsyncSession) -> None:
    """A flat cut writes one snapshot per duration. An older cut that the newer
    one did not touch (the 90-day plan had no agreed price then) is not part of
    it, so one tap of «برگرداندن» must not take both back."""
    shop = await _shop(session)
    await set_plan_price(session, shop.id, "m1", 100_000, duration_days=30, tier=T)
    await set_plan_price(session, shop.id, "m3", 300_000, duration_days=90, tier=T)
    first = datetime.now(UTC) - timedelta(minutes=5)
    await apply_percent_cut(
        session,
        shop.id,
        duration_days=90,
        tier=T,
        pct=10,
        changes=[("m3", 300_000, 270_000)],
        created_at=first,
    )
    await apply_percent_cut(
        session, shop.id, duration_days=30, tier=T, pct=10, changes=[("m1", 100_000, 90_000)]
    )

    restored = await undo_flat_percent_cut(
        session, shop.id, durations=[30, 90], tier=T, slugs={"m1", "m3"}
    )
    assert restored == 1
    assert await _price(session, shop.id, "m1", 30) == 100_000
    assert await _price(session, shop.id, "m3", 90) == 270_000


# --------------------------------------------------------------------------- #
# Handlers: two groups sharing a 30-day tab.
# --------------------------------------------------------------------------- #


def _settings() -> Settings:
    return Settings(_env_file=None, BOT_TOKEN="test-token", ADMIN_TELEGRAM_IDS=[ADMIN_ID])


def _message(text: str | None = None) -> MagicMock:
    message = MagicMock(spec=Message)
    message.edit_text = AsyncMock()
    message.answer = AsyncMock()
    message.from_user = SimpleNamespace(id=ADMIN_ID)
    message.text = text
    return message


def _callback(data: str) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.from_user = SimpleNamespace(id=ADMIN_ID)
    callback.answer = AsyncMock()
    callback.message = _message()
    return callback


async def _seed_two_groups(session: AsyncSession, *, flat_first: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, title in (("a", "حجمی"), ("b", "ویژه")):
        cat = await create_draft_category(session, title_fa=title, kind="volume")
        if key == "a" and flat_first:
            cat.sales_flags = {**dict(cat.sales_flags or {}), "flat_plan_list": True}
            flag_modified(cat, "sales_flags")
        pkg = await create_draft_package(
            session,
            category_id=cat.id,
            title_fa=f"{title} 20 گیگ / 30 روز",
            fixed_price_toman=120_000,
            volume_gb=20,
            duration_days=30,
        )
        out[key] = str(pkg.slug)
        out[f"{key}_cat_slug"] = str(cat.slug)
    await commit_and_activate(session, notes="seed two groups")
    view = await get_catalog_view(session)
    for key in ("a", "b"):
        out[f"{key}_cat"] = next(c.id for c in view.categories if c.slug == out[f"{key}_cat_slug"])
    return out


async def _open(session: AsyncSession, state: FSMContext, shop: User, cat_id: int) -> None:
    await state.update_data(rs_user_id=shop.id, rs_category_id=cat_id)
    await admin_handlers._show_reseller_matrix(_message(), session, state, shop.id)


async def _cut(session: AsyncSession, state: FSMContext, pct: str) -> str:
    await admin_handlers.admin_reseller_pct_ask(_callback(AdminCB.RS_PCT), state, _settings(), None)
    reply = _message(pct)
    await admin_handlers.admin_reseller_number_save(reply, state, session, _settings(), None)
    preview = str(reply.answer.await_args.args[0])
    await admin_handlers.admin_reseller_pct_confirm(
        _callback(AdminCB.RS_PCT_OK), state, session, _settings(), None
    )
    return preview


@pytest.mark.parametrize("flat_first", [False, True])
@pytest.mark.asyncio
async def test_cut_and_undo_stay_on_their_own_group(
    session: AsyncSession, flat_first: bool
) -> None:
    ids = await _seed_two_groups(session, flat_first=flat_first)
    shop = await _shop(session)
    await set_plan_price(session, shop.id, ids["a"], 100_000, duration_days=30, tier=T)
    await set_plan_price(session, shop.id, ids["b"], 200_000, duration_days=30, tier=T)
    await session.commit()
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))

    await _open(session, state, shop, ids["a_cat"])
    preview = await _cut(session, state, "10")
    assert "۱۰۰٬۰۰۰" in preview
    assert "۲۰۰٬۰۰۰" not in preview
    assert await _price(session, shop.id, ids["a"], 30) == 90_000
    assert await _price(session, shop.id, ids["b"], 30) == 200_000

    await _open(session, state, shop, ids["b_cat"])
    await _cut(session, state, "10")
    assert await _price(session, shop.id, ids["b"], 30) == 180_000

    await _open(session, state, shop, ids["a_cat"])
    await admin_handlers.admin_reseller_undo(
        _callback(AdminCB.RS_UNDO), state, session, _settings(), None
    )
    assert await _price(session, shop.id, ids["a"], 30) == 100_000
    assert await _price(session, shop.id, ids["b"], 30) == 180_000

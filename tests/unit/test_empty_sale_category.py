"""A group with nothing to sell is not offered to the customer.

«نامحدود» once sat on the purchase menu with no plans and the custom builder
switched off. Tapping it said «می‌تونی خرید سفارشی رو ادامه بدی» — a builder
that did not exist — under a legacy keyboard whose buttons fall through to
that same builder. The bot must not state a rule it does not keep.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from bot.config import Settings
from bot.handlers import purchase as purchase_handlers
from bot.keyboards.purchase import PurchaseCB
from bot.texts import texts
from database.base import Base
from services.store_admin import (
    commit_and_activate,
    create_draft_category,
    create_draft_package,
    get_or_create_draft,
    update_draft_category,
)
from services.store_config import get_published_store_config, load_pricing_config


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _category(session: AsyncSession, **kwargs: object):
    """Every group here has a panel group: what is under test is having nothing
    to sell, not having nowhere to provision (see ``test_single_group_source``)."""
    cat = await create_draft_category(session, **kwargs)
    await update_draft_category(session, cat.id, panel_group_ids="1")
    return cat


async def _seed(session: AsyncSession, *, custom_plan: bool = True) -> None:
    volume = await _category(session, title_fa="حجمی", kind="volume")
    await create_draft_package(
        session,
        category_id=volume.id,
        title_fa="20 گیگ / 30 روز",
        fixed_price_toman=120_000,
        volume_gb=20,
        duration_days=30,
    )
    empty = await _category(session, title_fa="نامحدود", kind="unlimited")
    await update_draft_category(session, empty.id, custom_purchase_mode="off")
    builder = await _category(session, title_fa="سفارشی", kind="volume")
    await update_draft_category(session, builder.id, custom_purchase_mode="everyone")
    # A parent whose only child is empty, and which has no builder of its own.
    parent = await _category(session, title_fa="ویژه", kind="volume")
    await update_draft_category(session, parent.id, custom_purchase_mode="off")
    hollow = await _category(session, title_fa="خالی", kind="volume", parent_id=parent.id)
    await update_draft_category(session, hollow.id, custom_purchase_mode="off")
    # A parent whose child sells.
    shelf = await _category(session, title_fa="اقتصادی‌ها", kind="volume")
    await update_draft_category(session, shelf.id, custom_purchase_mode="off")
    child = await _category(session, title_fa="اقتصادی", kind="volume", parent_id=shelf.id)
    await create_draft_package(
        session,
        category_id=child.id,
        title_fa="50 گیگ / 30 روز",
        fixed_price_toman=250_000,
        volume_gb=50,
        duration_days=30,
    )
    if custom_plan:
        # What the گیگ/ماه builder asks its questions from.
        draft = await get_or_create_draft(session)
        draft.pricing_document = load_pricing_config("config/pricing.example.json")
    await commit_and_activate(session, notes="seed")


@pytest.mark.asyncio
async def test_empty_group_is_hidden_and_the_rest_stay(session: AsyncSession) -> None:
    await _seed(session)
    shown = {c.title_fa for c in await purchase_handlers._published_sale_categories(session)}
    assert shown == {"حجمی", "سفارشی", "اقتصادی‌ها"}


@pytest.mark.asyncio
async def test_a_builder_only_group_is_hidden_when_the_store_has_no_custom_plan(
    session: AsyncSession,
) -> None:
    """A store built in the admin panel has an empty pricing document: the
    builder toggle is on by default, but there is nothing for it to ask."""
    await _seed(session, custom_plan=False)
    shown = {c.title_fa for c in await purchase_handlers._published_sale_categories(session)}
    assert shown == {"حجمی", "اقتصادی‌ها"}


@pytest.mark.asyncio
async def test_the_builder_says_so_instead_of_raising_without_a_custom_plan(
    session: AsyncSession,
) -> None:
    """A stale «سفارشی» button must not raise KeyError('volume_plan')."""
    await _seed(session, custom_plan=False)
    message = MagicMock()
    message.answer = AsyncMock()
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    await purchase_handlers._begin_custom_for_plan_type(
        message,
        state,
        session,
        Settings(_env_file=None, BOT_TOKEN="t"),
        purchase_handlers.PlanType.VOLUME,
        cat=None,
        db_user=None,
    )
    assert message.answer.await_args.args[0] == texts.purchase_no_ready_plans
    assert await state.get_state() == purchase_handlers.PurchaseStates.choosing_plan_type.state


@pytest.mark.asyncio
async def test_a_hidden_child_is_not_listed_under_its_parent(session: AsyncSession) -> None:
    await _seed(session)
    cfg = await get_published_store_config(session)
    assert cfg is not None
    parent = next(c for c in cfg.categories if c.title_fa == "ویژه")
    assert await purchase_handlers._published_sale_categories(session, parent_id=parent.id) == []


@pytest.mark.asyncio
async def test_fallback_promises_no_builder_and_offers_no_legacy_buttons(
    session: AsyncSession,
) -> None:
    """A stale button can still land on an empty group."""
    await _seed(session)
    cfg = await get_published_store_config(session)
    assert cfg is not None
    empty = next(c for c in cfg.categories if c.title_fa == "نامحدود")
    message = MagicMock()
    message.answer = AsyncMock()
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    await purchase_handlers._after_category_entry(
        message, state, session, Settings(_env_file=None, BOT_TOKEN="t"), cat=empty
    )
    call = message.answer.await_args
    body = call.args[0]
    markup = call.kwargs.get("reply_markup") or call.args[1]
    assert body == texts.purchase_no_ready_plans
    assert "سفارشی" not in body
    callbacks = [btn.callback_data or "" for row in markup.inline_keyboard for btn in row]
    assert PurchaseCB.TYPE_VOLUME not in callbacks
    assert PurchaseCB.TYPE_UNLIMITED not in callbacks
    assert any(c.startswith(PurchaseCB.CAT_PREFIX) for c in callbacks)


@pytest.mark.parametrize("custom_plan", [True, False])
@pytest.mark.asyncio
async def test_the_shelf_offers_the_builder_only_when_it_can_open(
    session: AsyncSession, custom_plan: bool
) -> None:
    """«حجمی» has a ready plan and the builder toggle at its default (on)."""
    await _seed(session, custom_plan=custom_plan)
    cfg = await get_published_store_config(session)
    assert cfg is not None
    volume = next(c for c in cfg.categories if c.title_fa == "حجمی")
    message = MagicMock()
    message.answer = AsyncMock()
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    await purchase_handlers._after_category_entry(
        message, state, session, Settings(_env_file=None, BOT_TOKEN="t"), cat=volume
    )
    markup = message.answer.await_args.kwargs["reply_markup"]
    callbacks = [btn.callback_data or "" for row in markup.inline_keyboard for btn in row]
    assert (PurchaseCB.CUSTOM in callbacks) is custom_plan

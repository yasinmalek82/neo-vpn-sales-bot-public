"""T-006 — purchase shelf duration/tier toggle."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
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
from bot.keyboards.purchase import (
    PurchaseCB,
    encode_duration_cb,
    parse_duration_cb,
    ready_plans_keyboard,
)
from bot.states import PurchaseStates
from bot.texts import texts
from database.base import Base
from database.models.store import StorePackagePrice
from services.panel_settings import resolve_groups_for_plan_type
from services.pricing import PlanType
from services.store_admin import (
    commit_and_activate,
    create_draft_category,
    create_draft_package,
    update_draft_category,
)
from services.store_pricing import DEFAULT_TIER, PREMIUM_TIER, get_price


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        BOT_TOKEN="test-token",
        PASARGUARD_BASE_URL="https://panel.example.com",
        PASARGUARD_API_KEY="pg_key_testvvvv-test-4est-8est-testtesttest",
        PASARGUARD_SUB_PATH="sub",
        PASARGUARD_TIMEOUT=10.0,
        DEFAULT_PANEL_GROUP_IDS=[900],
        ADMIN_TELEGRAM_IDS=[],
    )


async def _fsm() -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=1, user_id=1)
    return FSMContext(storage=storage, key=key)


def _cb(data: str, *, message: MagicMock | None = None) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.from_user = SimpleNamespace(id=1)
    callback.answer = AsyncMock()
    callback.message = message
    return callback


async def _seed_matrix(session: AsyncSession) -> tuple:
    """Category with durations [60, 30], two plans, cells for both durations on plan A."""
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    await update_draft_category(
        session,
        cat.id,
        # sales_flags via normalize — set durations on flags directly after
    )
    cat.sales_flags = {
        **dict(cat.sales_flags or {}),
        "durations": [60, 30],
        "tiers_enabled": False,
    }
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(cat, "sales_flags")
    a = await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۱۰ گیگ",
        fixed_price_toman=70_000,
        volume_gb=10,
        duration_days=30,
    )
    b = await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۱۵ گیگ",
        fixed_price_toman=105_000,
        volume_gb=15,
        duration_days=30,
    )
    # Add 60-day cells for plan A only; plan B stays 30-only.
    session.add(
        StorePackagePrice(
            store_version_id=a.store_version_id,
            package_slug=a.slug,
            duration_days=60,
            tier=DEFAULT_TIER,
            price_toman=120_000,
        )
    )
    await commit_and_activate(session, notes="seed toggle matrix")
    from services.store_config import get_published_store_config

    cfg = await get_published_store_config(session)
    assert cfg is not None
    pub_cat = next(c for c in cfg.categories if c.slug == cat.slug)
    return cfg, pub_cat, a.slug, b.slug


def test_parse_duration_cb_round_trips() -> None:
    assert encode_duration_cb(30) == "30"
    assert parse_duration_cb("30") == 30
    assert encode_duration_cb(None) == "n"
    assert parse_duration_cb("n") is None
    assert parse_duration_cb("0") == 0
    assert encode_duration_cb(0) == "0"


def test_shelf_hides_plans_without_a_cell_for_selected_duration() -> None:
    """Plan priced only at 60d is absent from the 30d tab."""
    pkg30 = SimpleNamespace(id=1, slug="a", title_fa="۱۰ گیگ", category_id=1)
    # Keyboard itself does not filter — callers pass already-filtered packages.
    markup = ready_plans_keyboard(
        [pkg30],
        include_custom=False,
        prices_by_slug={"a": 70_000},
        durations=[60, 30],
        tiers=[DEFAULT_TIER],
        selected_duration=30,
        selected_tier=DEFAULT_TIER,
    )
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    assert any("۱۰ گیگ" in x for x in labels)
    assert not any("۱۵ گیگ" in x for x in labels)


@pytest.mark.asyncio
async def test_default_selection_is_first_configured_duration(
    session: AsyncSession,
) -> None:
    cfg, cat, slug_a, slug_b = await _seed_matrix(session)
    from services.store_pricing import category_durations

    assert category_durations(cat) == [60, 30]
    state = await _fsm()
    message = MagicMock()
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    await purchase_handlers._after_category_entry(
        message, state, session, _settings(), cat=cat, db_user=None
    )
    data = await state.get_data()
    assert data["sel_duration_days"] == 60
    # First render used answer(); capture markup
    assert message.answer.await_count == 1
    markup = (
        message.answer.await_args.kwargs.get("reply_markup") or message.answer.await_args.args[1]
    )
    # Selected 60-day toggle carries ✅
    dur_labels = [
        btn.text
        for row in markup.inline_keyboard
        for btn in row
        if (btn.callback_data or "").startswith(PurchaseCB.DUR_PREFIX)
    ]
    assert any("✅" in lab and "۲" in lab for lab in dur_labels)  # ۲ ماهه


def test_tier_row_absent_unless_tiers_enabled() -> None:
    pkgs = [SimpleNamespace(id=1, slug="a", title_fa="۱۰ گیگ")]
    no_tier = ready_plans_keyboard(
        pkgs,
        include_custom=False,
        prices_by_slug={"a": 1},
        durations=[30],
        tiers=[DEFAULT_TIER],
        selected_duration=30,
        selected_tier=DEFAULT_TIER,
    )
    cbs = [btn.callback_data for row in no_tier.inline_keyboard for btn in row]
    assert not any((c or "").startswith(PurchaseCB.TIER_PREFIX) for c in cbs)

    with_tier = ready_plans_keyboard(
        pkgs,
        include_custom=False,
        prices_by_slug={"a": 1},
        durations=[30],
        tiers=[DEFAULT_TIER, PREMIUM_TIER],
        selected_duration=30,
        selected_tier=DEFAULT_TIER,
    )
    cbs2 = [btn.callback_data for row in with_tier.inline_keyboard for btn in row]
    tier_cbs = [c for c in cbs2 if (c or "").startswith(PurchaseCB.TIER_PREFIX)]
    assert len(tier_cbs) == 2


@pytest.mark.asyncio
async def test_tapping_a_plan_charges_the_selected_duration_cell(
    session: AsyncSession,
) -> None:
    """Money test: select 60d, tap plan that has 30 and 60 cells → pay 60d price."""
    cfg, cat, slug_a, _slug_b = await _seed_matrix(session)
    pkg = next(p for p in cfg.packages if p.slug == slug_a)
    state = await _fsm()
    await state.set_state(PurchaseStates.choosing_ready_plan)
    await state.update_data(
        store_category_id=cat.id,
        plan_type=PlanType.VOLUME.value,
        sel_duration_days=60,
        sel_tier=DEFAULT_TIER,
    )
    message = MagicMock()
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    # Bypass service-name ask by stubbing
    original_ask = purchase_handlers._ask_service_name
    asked: dict = {}

    async def _capture_ask(msg, st, db_user=None, session=None, settings=None):
        asked["data"] = await st.get_data()

    purchase_handlers._ask_service_name = _capture_ask  # type: ignore[assignment]
    try:
        callback = _cb(f"{PurchaseCB.READY_PREFIX}{pkg.id}", message=message)
        await purchase_handlers.choose_ready_plan(
            callback, state, session, _settings(), db_user=None
        )
    finally:
        purchase_handlers._ask_service_name = original_ask  # type: ignore[assignment]

    assert asked["data"]["fixed_price_toman"] == 120_000
    assert asked["data"]["duration_days"] == 60
    cell30 = get_price(list(cfg.prices), slug=slug_a, duration_days=30, tier=DEFAULT_TIER)
    assert cell30 == 70_000
    assert asked["data"]["fixed_price_toman"] != cell30


@pytest.mark.asyncio
async def test_a_missing_cell_at_tap_answers_plan_gone_and_creates_no_order(
    session: AsyncSession,
) -> None:
    cfg, cat, slug_a, slug_b = await _seed_matrix(session)
    # Plan B has only 30d; select 60d then tap B.
    pkg_b = next(p for p in cfg.packages if p.slug == slug_b)
    state = await _fsm()
    await state.set_state(PurchaseStates.choosing_ready_plan)
    await state.update_data(
        store_category_id=cat.id,
        plan_type=PlanType.VOLUME.value,
        sel_duration_days=60,
        sel_tier=DEFAULT_TIER,
    )
    message = MagicMock()
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    callback = _cb(f"{PurchaseCB.READY_PREFIX}{pkg_b.id}", message=message)
    await purchase_handlers.choose_ready_plan(callback, state, session, _settings(), db_user=None)
    callback.answer.assert_awaited()
    # show_alert with purchase_plan_gone
    args, kwargs = callback.answer.await_args
    assert texts.purchase_plan_gone in (args[0] if args else kwargs.get("text", ""))
    assert kwargs.get("show_alert") is True
    data = await state.get_data()
    assert "fixed_price_toman" not in data


@pytest.mark.asyncio
async def test_premium_uses_premium_panel_group_ids(session: AsyncSession) -> None:
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    await update_draft_category(session, cat.id, panel_group_ids="11,12")
    cat.sales_flags = {
        **dict(cat.sales_flags or {}),
        "tiers_enabled": True,
        "premium_panel_group_ids": [77, 88],
    }
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(cat, "sales_flags")
    await commit_and_activate(session, notes="tier groups")
    # Re-load published id
    from services.store_config import get_published_store_config

    cfg = await get_published_store_config(session)
    assert cfg is not None
    pub = cfg.categories[0]
    settings = SimpleNamespace(default_panel_group_ids=[900])
    eco = await resolve_groups_for_plan_type(
        session, settings, PlanType.VOLUME, store_category_id=pub.id, tier="economy"
    )
    prem = await resolve_groups_for_plan_type(
        session, settings, PlanType.VOLUME, store_category_id=pub.id, tier="premium"
    )
    assert eco == [11, 12]
    assert prem == [77, 88]


@pytest.mark.asyncio
async def test_retapping_selected_duration_does_not_edit(
    session: AsyncSession,
) -> None:
    cfg, cat, _a, _b = await _seed_matrix(session)
    state = await _fsm()
    await state.set_state(PurchaseStates.choosing_ready_plan)
    await state.update_data(
        store_category_id=cat.id,
        plan_type=PlanType.VOLUME.value,
        sel_duration_days=60,
        sel_tier=DEFAULT_TIER,
    )
    message = MagicMock()
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    callback = _cb(f"{PurchaseCB.DUR_PREFIX}{encode_duration_cb(60)}", message=message)
    await purchase_handlers.choose_duration_toggle(
        callback, state, session, _settings(), db_user=None
    )
    message.edit_text.assert_not_awaited()
    callback.answer.assert_awaited()


@pytest.mark.asyncio
async def test_plans_for_selection_filters_by_matrix_cell(
    session: AsyncSession,
) -> None:
    cfg, cat, slug_a, slug_b = await _seed_matrix(session)
    pkgs = [p for p in cfg.packages if int(p.category_id) == int(cat.id)]
    at_30 = purchase_handlers._plans_for_selection(
        pkgs, cfg, duration_days=30, tier=DEFAULT_TIER, category=cat
    )
    at_60 = purchase_handlers._plans_for_selection(
        pkgs, cfg, duration_days=60, tier=DEFAULT_TIER, category=cat
    )
    assert {p.slug for p in at_30} >= {slug_a, slug_b}
    assert {p.slug for p in at_60} == {slug_a}

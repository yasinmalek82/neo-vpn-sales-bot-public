"""Audit FLOW: handler-level logic bugs, each proved by a strict xfail."""

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
from sqlalchemy.orm.attributes import flag_modified

import database.models  # noqa: F401
from bot.config import Settings
from bot.handlers import purchase as purchase_handlers
from bot.keyboards.purchase import PurchaseCB
from bot.states import PurchaseStates
from database.base import Base
from services.pricing import PlanType
from services.store_admin import (
    commit_and_activate,
    create_draft_category,
    create_draft_package,
    get_or_create_draft,
)
from services.store_config import get_published_store_config
from services.store_pricing import DEFAULT_TIER


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
        ADMIN_TELEGRAM_IDS=[1],
        STORE_OWNER_TELEGRAM_IDS=[1],
    )


def _cb(data: str, message: MagicMock) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.from_user = SimpleNamespace(id=1)
    callback.answer = AsyncMock()
    callback.message = message
    return callback


@pytest.mark.asyncio
async def test_shelf_tap_survives_an_unrelated_catalog_edit(session: AsyncSession) -> None:
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    cat.sales_flags = {**dict(cat.sales_flags or {}), "durations": [30], "tiers_enabled": False}
    flag_modified(cat, "sales_flags")
    await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۵۰ گیگ",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
    )
    await commit_and_activate(session, notes="اولیه")
    cfg = await get_published_store_config(session)
    assert cfg is not None
    shelf_cat = next(c for c in cfg.categories if c.slug == cat.slug)
    shelf_pkg = next(p for p in cfg.packages if int(p.category_id) == int(shelf_cat.id))
    old_pkg_id = int(shelf_pkg.id)

    # The customer has the shelf open. The owner then edits something unrelated.
    await get_or_create_draft(session)
    await commit_and_activate(session, notes="ویرایش نامربوط")

    storage = MemoryStorage()
    state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=5, user_id=5))
    await state.set_state(PurchaseStates.choosing_ready_plan)
    await state.update_data(
        store_category_id=int(shelf_cat.id),
        plan_type=PlanType.VOLUME.value,
        sel_duration_days=30,
        sel_tier=DEFAULT_TIER,
    )
    message = MagicMock()
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    reached: dict = {}

    async def _capture(msg, st, db_user=None, session=None, settings=None):  # type: ignore[no-untyped-def]
        reached["data"] = await st.get_data()

    original = purchase_handlers._ask_service_name
    purchase_handlers._ask_service_name = _capture  # type: ignore[assignment]
    try:
        await purchase_handlers.choose_ready_plan(
            _cb(f"{PurchaseCB.READY_PREFIX}{old_pkg_id}", message),
            state,
            session,
            _settings(),
            db_user=None,
        )
    finally:
        purchase_handlers._ask_service_name = original  # type: ignore[assignment]

    assert reached.get("data", {}).get("fixed_price_toman") == 150_000


async def _user(session: AsyncSession, tg: int) -> int:
    from database.models import User

    user = User(
        telegram_id=tg,
        username=f"u{tg}",
        first_name="U",
        language_code="fa",
        referral_code=f"R{tg:07d}",
        is_admin=False,
    )
    session.add(user)
    await session.flush()
    return int(user.id)


@pytest.mark.asyncio
async def test_old_wallet_confirm_button_cannot_apply_another_customers_amount(
    session: AsyncSession,
) -> None:
    from bot.handlers import admin_wallet as admin_handlers
    from bot.keyboards.admin import AdminCB

    cust_a = await _user(session, 601)
    cust_b = await _user(session, 602)
    await session.commit()

    # The admin opened customer B's wallet prompt and typed +900000 (pending in FSM);
    # the confirm button still on screen from customer A's earlier prompt is tapped.
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.update_data(wallet_user_id=cust_b, wallet_amount=900_000, wallet_stamp="77")
    message = MagicMock()
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    callback = _cb(f"{AdminCB.WALLET_OK}{cust_a}", message)
    callback.message = None
    bot = MagicMock()
    bot.send_message = AsyncMock()

    await admin_handlers.admin_wallet_apply(
        callback, state, session, _settings(), db_user=None, bot=bot
    )

    from database.models import User

    a = await session.get(User, cust_a)
    assert int(a.wallet_balance_toman or 0) == 0

"""Flat reseller matrix: each plan priced at its own duration cell."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from bot.handlers.admin import (
    admin_reseller_matrix_cell,
    admin_reseller_number_save,
    admin_reseller_plan_clear,
    admin_reseller_section,
    admin_resellers_list,
)
from bot.handlers.purchase import _reseller_shelf_prices_flat
from bot.keyboards.admin import AdminCB, admin_home_keyboard
from bot.texts import texts
from database.base import Base
from database.models import User
from services.reseller import get_plan_price, set_plan_price
from services.store_admin import (
    commit_and_activate,
    create_draft_category,
    create_draft_package,
    get_catalog_view,
    set_category_flat_plan_list,
)
from services.store_pricing import DEFAULT_TIER

ADMIN_ID = 4242

FLAT_SPECS = [
    (10, 30, 60_000),
    (20, 30, 115_000),
    (30, 30, 165_000),
    (50, 60, 255_000),
    (100, 90, 430_000),
    (200, 90, 700_000),
]


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def shop(session: AsyncSession) -> User:
    user = User(telegram_id=700, first_name="موبایل ایران", referral_code="S1")
    session.add(user)
    await session.flush()
    return user


def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=ADMIN_ID, user_id=ADMIN_ID),
    )


def _settings() -> SimpleNamespace:
    return SimpleNamespace(admin_telegram_ids=[ADMIN_ID], store_owner_telegram_ids=[ADMIN_ID])


def _admin() -> SimpleNamespace:
    return SimpleNamespace(id=1, telegram_id=ADMIN_ID, is_admin=True, is_reseller=False)


def _callback(data: str) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=ADMIN_ID),
        answer=AsyncMock(),
        message=None,
    )


def _typed(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=ADMIN_ID),
        answer=AsyncMock(),
        edit_text=AsyncMock(),
    )


@pytest.fixture
def screen(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    msg = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr("bot.handlers.admin._tg_message", lambda _cb: msg)
    return msg


def _rendered(msg: SimpleNamespace) -> tuple[str, object]:
    call = msg.edit_text.await_args or msg.answer.await_args
    assert call is not None, "nothing was rendered"
    return call.args[0], call.kwargs.get("reply_markup")


def _labels(markup) -> list[str]:  # type: ignore[no-untyped-def]
    return [b.text for row in markup.inline_keyboard for b in row]


def _callbacks(markup) -> list[str]:  # type: ignore[no-untyped-def]
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def _seed_flat_ladder(session: AsyncSession) -> dict[str, object]:
    await create_draft_category(session, title_fa="حجمی نردبان", kind="volume")
    await commit_and_activate(session, notes="گروه")
    view = await get_catalog_view(session)
    cat_id = next(c.id for c in view.categories if c.title_fa == "حجمی نردبان")
    await set_category_flat_plan_list(session, cat_id, enabled=True)
    await commit_and_activate(session, notes="فهرست ساده")
    view = await get_catalog_view(session)
    cat_id = next(c.id for c in view.categories if c.title_fa == "حجمی نردبان")
    slugs_by_gb: dict[int, str] = {}
    for gb, days, price in FLAT_SPECS:
        await create_draft_package(
            session,
            category_id=cat_id,
            title_fa=f"{gb} گیگ",
            fixed_price_toman=price,
            volume_gb=gb,
            duration_days=days,
        )
        await commit_and_activate(session, notes="پلن")
        view = await get_catalog_view(session)
        cat_id = next(c.id for c in view.categories if c.title_fa == "حجمی نردبان")
        live = next(p for p in view.packages if p.title_fa == f"{gb} گیگ")
        slugs_by_gb[gb] = str(live.slug)
    view = await get_catalog_view(session)
    packages = [p for p in view.packages if p.title_fa.endswith(" گیگ")]
    packages.sort(key=lambda p: int(dict(p.options or {}).get("volume_gb") or 0))
    return {"slugs_by_gb": slugs_by_gb, "packages": packages, "cat_id": cat_id}


async def _open_plans(session: AsyncSession, shop: User) -> FSMContext:
    state = _state()
    await admin_reseller_section(
        _callback(f"{AdminCB.RS_SEC}{shop.id}:plans"),  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        _admin(),  # type: ignore[arg-type]
    )
    return state


@pytest.mark.asyncio
async def test_flat_matrix_lists_all_plans_without_duration_or_tier_tabs(
    session: AsyncSession, shop: User, screen: SimpleNamespace
) -> None:
    seeded = await _seed_flat_ladder(session)
    await _open_plans(session, shop)
    _, markup = _rendered(screen)
    cbs = _callbacks(markup)
    labels = _labels(markup)

    for slug in seeded["slugs_by_gb"].values():  # type: ignore[union-attr]
        assert any(c == f"{AdminCB.RS_CELL}{slug}" for c in cbs), slug
    assert not any(c.startswith(AdminCB.RS_DUR) for c in cbs)
    assert not any(c.startswith(AdminCB.RS_TIER) for c in cbs)
    assert texts.admin_reseller_pct_btn_flat in labels
    assert texts.admin_reseller_pct_btn not in labels


@pytest.mark.asyncio
async def test_flat_cell_write_hits_plan_own_duration(
    session: AsyncSession, shop: User, screen: SimpleNamespace
) -> None:
    seeded = await _seed_flat_ladder(session)
    slug100 = seeded["slugs_by_gb"][100]  # type: ignore[index]
    state = await _open_plans(session, shop)

    await admin_reseller_matrix_cell(
        _callback(f"{AdminCB.RS_CELL}{slug100}"),  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        _admin(),  # type: ignore[arg-type]
    )
    message = _typed("300000")
    await admin_reseller_number_save(
        message,  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        _admin(),  # type: ignore[arg-type]
    )
    assert (
        await get_plan_price(session, shop.id, slug100, duration_days=90, tier=DEFAULT_TIER)
        == 300_000
    )
    assert (
        await get_plan_price(session, shop.id, slug100, duration_days=30, tier=DEFAULT_TIER) is None
    )


@pytest.mark.asyncio
async def test_flat_clear_hits_plan_own_duration_not_the_tab(
    session: AsyncSession, shop: User, screen: SimpleNamespace
) -> None:
    seeded = await _seed_flat_ladder(session)
    slug100 = seeded["slugs_by_gb"][100]  # type: ignore[index]
    await set_plan_price(session, shop.id, slug100, 300_000, duration_days=90, tier=DEFAULT_TIER)
    await session.commit()
    state = _state()
    await state.update_data(rs_user_id=shop.id, rs_sel_duration_days=30, rs_sel_tier=DEFAULT_TIER)
    await admin_reseller_plan_clear(
        _callback(f"{AdminCB.RS_PLAN_CLEAR}{shop.id}:{slug100}"),  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        _admin(),  # type: ignore[arg-type]
    )
    assert (
        await get_plan_price(session, shop.id, slug100, duration_days=90, tier=DEFAULT_TIER) is None
    )


@pytest.mark.asyncio
async def test_flat_shelf_prices_map_agreed_only(
    session: AsyncSession, shop: User, screen: SimpleNamespace
) -> None:
    seeded = await _seed_flat_ladder(session)
    slug100 = seeded["slugs_by_gb"][100]  # type: ignore[index]
    packages = seeded["packages"]
    state = await _open_plans(session, shop)
    await admin_reseller_matrix_cell(
        _callback(f"{AdminCB.RS_CELL}{slug100}"),  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        _admin(),  # type: ignore[arg-type]
    )
    await admin_reseller_number_save(
        _typed("300000"),  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        _admin(),  # type: ignore[arg-type]
    )
    shop.is_reseller = True
    await session.flush()
    shelf = await _reseller_shelf_prices_flat(
        session, shop, packages, tier=DEFAULT_TIER  # type: ignore[arg-type]
    )
    assert shelf[slug100] == 300_000
    slug10 = seeded["slugs_by_gb"][10]  # type: ignore[index]
    assert slug10 not in shelf


@pytest.mark.asyncio
async def test_non_flat_matrix_still_has_duration_tabs(
    session: AsyncSession, shop: User, screen: SimpleNamespace
) -> None:
    await create_draft_category(session, title_fa="حجمی تب‌دار", kind="volume")
    await commit_and_activate(session, notes="گروه")
    view = await get_catalog_view(session)
    cat_id = next(c.id for c in view.categories if c.title_fa == "حجمی تب‌دار")
    for title, price in (("۱۰ گیگ", 60_000), ("۲۰ گیگ", 115_000)):
        await create_draft_package(
            session,
            category_id=cat_id,
            title_fa=title,
            fixed_price_toman=price,
            volume_gb=10 if "۱۰" in title else 20,
            duration_days=30,
        )
        await commit_and_activate(session, notes="پلن")
    await _open_plans(session, shop)
    _, markup = _rendered(screen)
    assert any(c.startswith(AdminCB.RS_DUR) for c in _callbacks(markup))


@pytest.mark.asyncio
async def test_admin_home_has_resellers_entry() -> None:
    from bot.keyboards.admin import admin_people_keyboard

    assert AdminCB.SEC_PEOPLE in _callbacks(admin_home_keyboard())
    assert AdminCB.RESELLERS in _callbacks(admin_people_keyboard())
    assert AdminCB.RESELLERS == "adm:rslist"


@pytest.mark.asyncio
async def test_resellers_list_empty_and_populated(
    session: AsyncSession, shop: User, screen: SimpleNamespace
) -> None:
    await admin_resellers_list(
        _callback(AdminCB.RESELLERS),  # type: ignore[arg-type]
        _state(),
        session,
        _settings(),  # type: ignore[arg-type]
        _admin(),  # type: ignore[arg-type]
    )
    body, markup = _rendered(screen)
    assert body == texts.admin_resellers_empty
    assert markup is not None and markup.inline_keyboard

    shop.is_reseller = True
    await session.flush()
    await admin_resellers_list(
        _callback(AdminCB.RESELLERS),  # type: ignore[arg-type]
        _state(),
        session,
        _settings(),  # type: ignore[arg-type]
        _admin(),  # type: ignore[arg-type]
    )
    body, markup = _rendered(screen)
    assert "نمایندگان" in body
    assert f"{AdminCB.RS_PANEL}{shop.id}" in _callbacks(markup)

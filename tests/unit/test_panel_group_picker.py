"""The panel-group setting is a picker of the panel's own groups, several allowed.

The owner typed one id from memory and could not give «حجمی ویژه» two panel
groups (2026-09-30: «این محدودیت بیجا هست»). The screen now lists the panel's
groups as toggles; typing ids stays as the fallback when the panel is down.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from bot.config import Settings
from bot.handlers import store_admin as ha
from bot.keyboards.store_admin import StoreCB, store_panel_group_picker_keyboard
from bot.keyboards.style import PRIMARY
from database.base import Base
from services.store_admin import (
    MAX_PANEL_GROUPS,
    StoreAdminError,
    commit_and_activate,
    create_draft_category,
    get_catalog_view,
    parse_panel_group_ids,
)

OWNER_ID = 4242
SETTINGS = Settings(
    _env_file=None,  # type: ignore[call-arg]
    BOT_TOKEN="test-token",
    ADMIN_TELEGRAM_IDS=[OWNER_ID],
    STORE_OWNER_TELEGRAM_IDS=[OWNER_ID],
)
PANEL = [(11, "tunnel-a", False), (12, "tunnel-b", False), (22, "direct", True)]


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _buttons(markup: Any) -> list[Any]:
    return [b for row in markup.inline_keyboard for b in row]


def test_typed_ids_accept_several_and_refuse_junk() -> None:
    assert parse_panel_group_ids("11,12") == [11, 12]
    assert parse_panel_group_ids(" 11، 12 11 ") == [11, 12]
    assert parse_panel_group_ids("") == []
    for bad in ("x", "0", "11,-2"):
        with pytest.raises(StoreAdminError):
            parse_panel_group_ids(bad)
    with pytest.raises(StoreAdminError):
        parse_panel_group_ids(",".join(str(i) for i in range(1, MAX_PANEL_GROUPS + 2)))


def test_picker_lists_the_panel_groups_and_marks_the_chosen() -> None:
    markup = store_panel_group_picker_keyboard(5, PANEL, [12, 99])
    texts_ = [b.text for b in _buttons(markup)]
    assert any(t.startswith("✅") and "tunnel-b" in t for t in texts_)
    assert any(t.startswith("▫️") and "tunnel-a" in t for t in texts_)
    assert any("direct" in t and "⛔" in t for t in texts_), "a disabled group is marked"
    # A chosen id the panel no longer lists still shows, so it can be removed.
    assert any("#99" in t and t.startswith("✅") for t in texts_)
    picks = [
        b.callback_data for b in _buttons(markup) if b.callback_data.startswith(StoreCB.GROUP_PICK)
    ]
    assert f"{StoreCB.GROUP_PICK}5:11" in picks
    assert all(len(c.encode()) <= 64 for c in picks)
    primaries = [b for b in _buttons(markup) if b.style == PRIMARY]
    assert [b.callback_data for b in primaries] == [f"{StoreCB.GROUP_PICK_SAVE}5"]


def _cb(data: str, screen: Any) -> SimpleNamespace:
    return SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=OWNER_ID), answer=AsyncMock(), message=screen
    )


async def _category(session: AsyncSession) -> int:
    cat = await create_draft_category(session, title_fa="حجمی ویژه", kind="volume")
    await commit_and_activate(session, notes="x")
    view = await get_catalog_view(session)
    return next(c.id for c in view.categories if c.slug == cat.slug)


@pytest.mark.asyncio
async def test_two_panel_groups_are_picked_and_saved(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    cat_id = await _category(session)
    fetch = AsyncMock(return_value=(PANEL, None))
    monkeypatch.setattr(ha, "_fetch_live_groups", fetch)
    screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(ha, "_tg_message", lambda _cb: screen)
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=OWNER_ID, user_id=OWNER_ID)
    )

    await ha.store_cat_edit_groups(  # type: ignore[arg-type]
        _cb(f"{StoreCB.CAT_GROUPS}{cat_id}", screen), SETTINGS, None, session, state
    )
    shown = screen.edit_text.await_args
    assert "tunnel-a" in str(shown.kwargs["reply_markup"])
    for gid in (11, 12, 22, 22):  # 22 on and off again
        await ha.store_group_pick_toggle(  # type: ignore[arg-type]
            _cb(f"{StoreCB.GROUP_PICK}{cat_id}:{gid}", screen), SETTINGS, None, state
        )
    fetch.assert_awaited_once()  # the list is fetched on open, not per toggle
    await ha.store_group_pick_save(  # type: ignore[arg-type]
        _cb(f"{StoreCB.GROUP_PICK_SAVE}{cat_id}", screen), SETTINGS, None, session, state
    )
    live = (await get_catalog_view(session)).category(cat_id)
    assert live is not None
    assert sorted(live.panel_group_ids) == [11, 12]
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_a_stale_toggle_does_not_write(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    cat_id = await _category(session)
    screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(ha, "_tg_message", lambda _cb: screen)
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=OWNER_ID, user_id=OWNER_ID)
    )
    cb = _cb(f"{StoreCB.GROUP_PICK_SAVE}{cat_id}", screen)
    await ha.store_group_pick_save(cb, SETTINGS, None, session, state)  # type: ignore[arg-type]
    assert cb.answer.await_args.kwargs.get("show_alert") is True
    live = (await get_catalog_view(session)).category(cat_id)
    assert live is not None and not live.panel_group_ids

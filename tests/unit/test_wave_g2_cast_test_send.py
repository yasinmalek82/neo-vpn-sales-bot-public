"""Wave G2: «🧪 اول برای خودم بفرست» on the broadcast confirm screen."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from bot.handlers.admin import admin_cast_test_send
from bot.keyboards.admin import AdminCB, broadcast_confirm_keyboard
from database.base import Base
from database.models.broadcast import Broadcast, BroadcastTarget

ADMIN_ID = 555
BODY = "سلام ۱\n─────\nنئو نتورک"


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def _state(body: str | None) -> FSMContext:
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=ADMIN_ID, user_id=ADMIN_ID)
    )
    if body is not None:
        await state.update_data(broadcast_body=body, broadcast_audience="all_customers")
    return state


def _callback() -> SimpleNamespace:
    return SimpleNamespace(from_user=SimpleNamespace(id=ADMIN_ID), answer=AsyncMock())


def test_the_confirm_screen_offers_the_test_send() -> None:
    data = [b.callback_data for row in broadcast_confirm_keyboard().inline_keyboard for b in row]
    assert AdminCB.CAST_TEST in data and len(AdminCB.CAST_TEST.encode()) <= 64


@pytest.mark.asyncio
async def test_the_exact_body_goes_to_the_tapping_admin_and_nothing_is_created(
    session: AsyncSession,
) -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    cb = _callback()
    await admin_cast_test_send(
        cb,  # type: ignore[arg-type]
        await _state(BODY),
        bot,  # type: ignore[arg-type]
        SimpleNamespace(admin_telegram_ids=[ADMIN_ID]),  # type: ignore[arg-type]
        None,
    )
    bot.send_message.assert_awaited_once_with(chat_id=ADMIN_ID, text=BODY)
    assert await session.scalar(select(func.count()).select_from(Broadcast)) == 0
    assert await session.scalar(select(func.count()).select_from(BroadcastTarget)) == 0


@pytest.mark.asyncio
async def test_a_non_admin_and_a_lost_draft_send_nothing() -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    stranger = _callback()
    stranger.from_user = SimpleNamespace(id=1)
    await admin_cast_test_send(
        stranger,  # type: ignore[arg-type]
        await _state(BODY),
        bot,  # type: ignore[arg-type]
        SimpleNamespace(admin_telegram_ids=[ADMIN_ID]),  # type: ignore[arg-type]
        None,
    )
    await admin_cast_test_send(
        _callback(),  # type: ignore[arg-type]
        await _state(None),
        bot,  # type: ignore[arg-type]
        SimpleNamespace(admin_telegram_ids=[ADMIN_ID]),  # type: ignore[arg-type]
        None,
    )
    bot.send_message.assert_not_awaited()

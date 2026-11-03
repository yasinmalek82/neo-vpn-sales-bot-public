"""«🔌 اتصال پنل»: owner-only writes, verified saves, edit in place."""

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
from api_client.models import GroupResponse
from bot.config import Settings
from bot.handlers import admin as ha
from bot.keyboards.admin import AdminCB, panel_cancel_keyboard, panel_settings_keyboard
from bot.keyboards.style import DANGER
from bot.states import AdminStates
from bot.texts import texts
from database.base import Base
from services.panel_settings import get_settings_row
from services.store_admin import (
    commit_and_activate,
    create_draft_category,
    update_draft_category,
)

OWNER_ID = 4242
STAFF_ID = 77
SETTINGS = Settings(
    _env_file=None,  # type: ignore[call-arg]
    BOT_TOKEN="test-token",
    ADMIN_TELEGRAM_IDS=[OWNER_ID, STAFF_ID],
    STORE_OWNER_TELEGRAM_IDS=[OWNER_ID],
)


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def _screen() -> SimpleNamespace:
    return SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock(), delete=AsyncMock())


def _cb(data: str, uid: int, screen: Any) -> SimpleNamespace:
    return SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=uid), answer=AsyncMock(), message=screen
    )


def _msg(text: str, uid: int) -> SimpleNamespace:
    return SimpleNamespace(
        text=text, from_user=SimpleNamespace(id=uid), answer=AsyncMock(), delete=AsyncMock()
    )


@pytest.fixture(autouse=True)
def _as_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ha, "_tg_message", lambda cb: cb.message)


def _callbacks(markup: Any) -> list[str]:
    return [b.callback_data or "" for row in markup.inline_keyboard for b in row]


# --- owner-only writes ------------------------------------------------------


def test_a_non_owner_sees_no_edit_or_reset_buttons() -> None:
    seen = _callbacks(panel_settings_keyboard(can_edit=False))
    assert not any(c.startswith(AdminCB.PANEL_EDIT) or c == AdminCB.PANEL_CLEAR for c in seen)
    assert AdminCB.PANEL_TEST in seen and AdminCB.PANEL_GROUPS_LIST in seen
    owner = _callbacks(panel_settings_keyboard(can_edit=True))
    assert f"{AdminCB.PANEL_EDIT}base_url" in owner and AdminCB.PANEL_CLEAR in owner


def test_reset_is_danger_on_its_own_row_and_nav_is_last() -> None:
    rows = panel_settings_keyboard().inline_keyboard
    reset = [r for r in rows if any(b.callback_data == AdminCB.PANEL_CLEAR for b in r)]
    assert len(reset) == 1 and len(reset[0]) == 1 and reset[0][0].style == DANGER
    assert rows[-1][0].callback_data == AdminCB.HOME


@pytest.mark.asyncio
async def test_a_non_owner_admin_cannot_open_an_edit_prompt_or_reset(
    session: AsyncSession,
) -> None:
    screen, state = _screen(), _state(STAFF_ID)
    cb = _cb(f"{AdminCB.PANEL_EDIT}api_key", STAFF_ID, screen)
    await ha.admin_panel_edit_field(cb, SETTINGS, None, state)  # type: ignore[arg-type]
    assert cb.answer.await_args.kwargs["show_alert"] is True
    assert await state.get_state() is None
    screen.edit_text.assert_not_awaited()

    for handler in (ha.admin_panel_clear_confirm, ha.admin_panel_clear):
        cb = _cb(AdminCB.PANEL_CLEAR, STAFF_ID, screen)
        await handler(cb, session, SETTINGS, None, state)  # type: ignore[arg-type]
        assert cb.answer.await_args.kwargs["show_alert"] is True
    screen.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_non_owner_message_in_a_panel_state_writes_nothing(session: AsyncSession) -> None:
    state = _state(STAFF_ID)
    await state.set_state(AdminStates.panel_waiting_base_url)
    await state.update_data(panel_field="base_url")
    msg = _msg("https://evil.example", STAFF_ID)
    await ha.admin_panel_save_field(msg, session, SETTINGS, state, None)  # type: ignore[arg-type]
    row = await get_settings_row(session)
    assert row is None or not row.base_url
    assert await state.get_state() is None
    assert texts.admin_store_owner_only in msg.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_a_non_owner_sees_the_screen_without_edit_buttons(session: AsyncSession) -> None:
    screen = _screen()
    await ha.admin_panel_settings(
        _cb(AdminCB.PANEL, STAFF_ID, screen), session, SETTINGS, None, _state(STAFF_ID)  # type: ignore[arg-type]
    )
    markup = screen.edit_text.await_args.kwargs["reply_markup"]
    assert not any(c.startswith(AdminCB.PANEL_EDIT) for c in _callbacks(markup))


# --- verified save ----------------------------------------------------------


async def _save(
    session: AsyncSession, field: str, value: str, check: AsyncMock, monkeypatch: Any
) -> SimpleNamespace:
    monkeypatch.setattr(ha, "check_panel_connection", check)
    state = _state(OWNER_ID)
    await state.set_state(AdminStates.panel_waiting_base_url)
    await state.update_data(panel_field=field)
    msg = _msg(value, OWNER_ID)
    await ha.admin_panel_save_field(msg, session, SETTINGS, state, None)  # type: ignore[arg-type]
    return msg


@pytest.mark.asyncio
async def test_a_save_runs_the_check_after_committing_and_says_ok(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str | None] = []

    async def check(sess: AsyncSession, _s: Settings) -> tuple[bool, str]:
        seen.append((await get_settings_row(sess)).base_url)
        return True, "ok"

    msg = await _save(
        session, "base_url", "https://panel.example", AsyncMock(side_effect=check), monkeypatch
    )
    assert seen == ["https://panel.example"]  # the check sees the saved value
    body = msg.answer.await_args.args[0]
    assert body.startswith(texts.admin_panel_saved_ok)
    assert msg.answer.await_count == 1


@pytest.mark.asyncio
async def test_a_failed_check_is_reported_but_the_value_stays_saved(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = AsyncMock(return_value=(False, "HTTP 401"))
    msg = await _save(session, "base_url", "https://panel.example", check, monkeypatch)
    body = msg.answer.await_args.args[0]
    assert body.startswith("⚠️ ذخیره شد ولی اتصال برقرار نیست: HTTP 401")
    assert (await get_settings_row(session)).base_url == "https://panel.example"


@pytest.mark.asyncio
async def test_a_trial_device_save_skips_the_check(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = AsyncMock(return_value=(True, "ok"))
    msg = await _save(session, "trial_hwid", "2", check, monkeypatch)
    check.assert_not_awaited()
    assert msg.answer.await_args.args[0].startswith(texts.admin_panel_saved)
    assert texts.admin_panel_saved_ok not in msg.answer.await_args.args[0]


# --- edit in place ----------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_test_and_groups_edit_the_screen_instead_of_sending(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ha, "check_panel_connection", AsyncMock(return_value=(True, "ok")))
    monkeypatch.setattr(
        ha, "_fetch_panel_groups", AsyncMock(return_value=([GroupResponse(id=11, name="a")], None))
    )
    state = _state(OWNER_ID)

    screen = _screen()
    await ha.admin_panel_edit_field(
        _cb(f"{AdminCB.PANEL_EDIT}timeout", OWNER_ID, screen), SETTINGS, None, state  # type: ignore[arg-type]
    )
    assert screen.edit_text.await_count == 1 and screen.answer.await_count == 0
    assert _callbacks(screen.edit_text.await_args.kwargs["reply_markup"]) == [AdminCB.PANEL]

    screen = _screen()
    cb = _cb(AdminCB.PANEL_TEST, OWNER_ID, screen)
    await ha.admin_panel_test(cb, session, SETTINGS, None, state)  # type: ignore[arg-type]
    assert screen.edit_text.await_count == 1 and screen.answer.await_count == 0
    assert screen.edit_text.await_args.args[0].startswith(texts.admin_panel_test_ok)
    assert cb.answer.await_args.kwargs.get("show_alert") is None  # no alert duplicate

    screen = _screen()
    await ha.admin_panel_groups_list(
        _cb(AdminCB.PANEL_GROUPS_LIST, OWNER_ID, screen), session, SETTINGS, None, state  # type: ignore[arg-type]
    )
    assert screen.edit_text.await_count == 1 and screen.answer.await_count == 0
    assert AdminCB.PANEL in _callbacks(screen.edit_text.await_args.kwargs["reply_markup"])


@pytest.mark.asyncio
async def test_a_failed_test_shows_the_error_on_the_panel_screen(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ha, "check_panel_connection", AsyncMock(return_value=(False, "timeout")))
    screen = _screen()
    await ha.admin_panel_test(
        _cb(AdminCB.PANEL_TEST, OWNER_ID, screen), session, SETTINGS, None, _state(OWNER_ID)  # type: ignore[arg-type]
    )
    assert "اتصال برقرار نیست: timeout" in screen.edit_text.await_args.args[0]


def test_cancel_returns_to_the_panel_not_home() -> None:
    assert _callbacks(panel_cancel_keyboard()) == [AdminCB.PANEL]


@pytest.mark.asyncio
async def test_an_invalid_value_keeps_the_cancel_to_panel_button(session: AsyncSession) -> None:
    state = _state(OWNER_ID)
    await state.set_state(AdminStates.panel_waiting_timeout)
    await state.update_data(panel_field="timeout")
    msg = _msg("abc", OWNER_ID)
    await ha.admin_panel_save_field(msg, session, SETTINGS, state, None)  # type: ignore[arg-type]
    assert _callbacks(msg.answer.await_args.kwargs["reply_markup"]) == [AdminCB.PANEL]


# --- groups list line -------------------------------------------------------


def test_group_line_format_and_only_the_name_is_trimmed() -> None:
    g = GroupResponse(id=11, name="tunnel-a", total_users=123, inbound_tags=["a", "b", "c"])
    line = ha._panel_group_line(g, ["حجمی ویژه"])
    assert line == "#11 tunnel-a • ۱۲۳ کاربر • ۳ اینباند → حجمی ویژه"
    off = GroupResponse(id=2, name="x", is_disabled=True)
    assert ha._panel_group_line(off, []) == "#2 x • ۰ کاربر • ۰ اینباند ⛔"
    long = GroupResponse(id=5, name="n" * 80, total_users=1, inbound_tags=["a"], is_disabled=True)
    trimmed = ha._panel_group_line(long, ["حجمی"])
    assert "…" in trimmed and "n" * 25 not in trimmed
    assert trimmed.endswith("۱ اینباند ⛔ → حجمی") and len(trimmed) <= 60


@pytest.mark.asyncio
async def test_groups_list_names_the_store_group_that_sells_from_each(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    cat = await create_draft_category(session, title_fa="حجمی ویژه", kind="volume")
    await update_draft_category(session, cat.id, panel_group_ids=[11])
    await commit_and_activate(session, notes="x")
    groups = [
        GroupResponse(id=11, name="tunnel-a", total_users=3, inbound_tags=["t"]),
        GroupResponse(id=12, name="spare"),
    ]
    monkeypatch.setattr(ha, "_fetch_panel_groups", AsyncMock(return_value=(groups, None)))
    screen = _screen()
    await ha.admin_panel_groups_list(
        _cb(AdminCB.PANEL_GROUPS_LIST, OWNER_ID, screen), session, SETTINGS, None, _state(OWNER_ID)  # type: ignore[arg-type]
    )
    body = screen.edit_text.await_args.args[0]
    assert "#11 tunnel-a • ۳ کاربر • ۱ اینباند → حجمی ویژه" in body
    assert "#12 spare • ۰ کاربر • ۰ اینباند" in body and "#12 spare" in body.splitlines()[-1]
    assert "→" not in body.splitlines()[-1]

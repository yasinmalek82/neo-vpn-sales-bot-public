"""The plan wizard, driven through its real handlers.

Why this file exists: 365 tests were green while «ساخت پلن» was impossible. Every
existing test called the *service* (`create_draft_package`) and none touched the
handlers, so nothing noticed that the size the owner picked never reached the
duration step — `_ask_duration` rendered the prompt and dropped the number, and
the next tap answered «حالت گم شد».

So these tests walk the callbacks the owner actually taps. A service-level test
cannot catch a defect that lives in the FSM hand-off between two handlers.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from bot.handlers.store_admin import (
    store_pkg_duration_pick,
    store_pkg_duration_typed,
    store_pkg_size_pick,
    store_pkg_size_typed,
)
from bot.keyboards.store_admin import StoreCB
from bot.states import AdminStates
from bot.texts import texts

OWNER_ID = 4242


def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=OWNER_ID, user_id=OWNER_ID),
    )


def _settings() -> SimpleNamespace:
    return SimpleNamespace(admin_telegram_ids=[OWNER_ID])


def _screen() -> SimpleNamespace:
    """The message the wizard edits in place."""
    return SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())


def _callback(data: str) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=OWNER_ID),
        answer=AsyncMock(),
        message=None,
    )


def _typed(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=OWNER_ID),
        answer=AsyncMock(),
        edit_text=AsyncMock(),
    )


@pytest.fixture
def screen(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """`_tg_message` does an isinstance check a fake cannot satisfy."""
    msg = _screen()
    monkeypatch.setattr("bot.handlers.store_admin._tg_message", lambda _cb: msg)
    return msg


async def _at_size_step(*, kind: str = "volume") -> FSMContext:
    """State as the category picker leaves it."""
    state = _state()
    await state.update_data(new_pkg_category_id=1, new_pkg_kind=kind)
    await state.set_state(AdminStates.store_waiting_new_pkg_size)
    return state


# --------------------------------------------------------------------------- #
# The defect: size picked by button, duration picked by button
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_volume_wizard_reaches_the_price_step(screen: SimpleNamespace) -> None:
    state = await _at_size_step()

    await store_pkg_size_pick(
        _callback(f"{StoreCB.PKG_SIZE}50"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        state,
    )
    assert await state.get_state() == AdminStates.store_waiting_new_pkg_duration.state

    duration_cb = _callback(f"{StoreCB.PKG_DUR}30")
    await store_pkg_duration_pick(
        duration_cb,  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        None,  # creation path never touches the session
        state,
    )

    # Volume plans ask for device limit before price (T-035).
    alerts = [c.kwargs.get("show_alert") for c in duration_cb.answer.await_args_list]
    assert True not in alerts, "duration step raised an alert instead of advancing"
    assert await state.get_state() == AdminStates.store_waiting_new_pkg_users.state

    data = await state.get_data()
    assert data["new_pkg_size"] == 50
    assert data["new_pkg_duration"] == 30


@pytest.mark.asyncio
async def test_unlimited_wizard_reaches_the_price_step(screen: SimpleNamespace) -> None:
    state = await _at_size_step(kind="unlimited")

    await store_pkg_size_pick(
        _callback(f"{StoreCB.PKG_SIZE}2"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        state,
    )
    await store_pkg_duration_pick(
        _callback(f"{StoreCB.PKG_DUR}1"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        None,
        state,
    )

    assert await state.get_state() == AdminStates.store_waiting_new_pkg_price.state
    data = await state.get_data()
    assert data["new_pkg_size"] == 2
    assert data["new_pkg_duration"] == 1


@pytest.mark.asyncio
async def test_no_expiry_is_carried_as_none(screen: SimpleNamespace) -> None:
    """«بدون انقضا» is encoded as 0 and must survive as a real ``None``."""
    state = await _at_size_step()

    await store_pkg_size_pick(
        _callback(f"{StoreCB.PKG_SIZE}100"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        state,
    )
    await store_pkg_duration_pick(
        _callback(f"{StoreCB.PKG_DUR}0"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        None,
        state,
    )

    data = await state.get_data()
    assert data["new_pkg_size"] == 100
    assert data["new_pkg_duration"] is None


# --------------------------------------------------------------------------- #
# The typed variants take the same hand-off
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_typed_size_then_typed_duration(screen: SimpleNamespace) -> None:
    state = await _at_size_step()

    await store_pkg_size_typed(
        _typed("50"),  # type: ignore[arg-type]
        state,
        _settings(),  # type: ignore[arg-type]
        None,
    )
    assert await state.get_state() == AdminStates.store_waiting_new_pkg_duration.state

    message = _typed("45")
    await store_pkg_duration_typed(
        message,  # type: ignore[arg-type]
        state,
        None,
        _settings(),  # type: ignore[arg-type]
        None,
    )

    assert await state.get_state() == AdminStates.store_waiting_new_pkg_users.state
    data = await state.get_data()
    assert data["new_pkg_size"] == 50
    assert data["new_pkg_duration"] == 45


@pytest.mark.asyncio
async def test_typed_size_then_button_duration(screen: SimpleNamespace) -> None:
    """Mixing input styles is normal and must not lose the size."""
    state = await _at_size_step()

    await store_pkg_size_typed(
        _typed("15"),  # type: ignore[arg-type]
        state,
        _settings(),  # type: ignore[arg-type]
        None,
    )
    await store_pkg_duration_pick(
        _callback(f"{StoreCB.PKG_DUR}30"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        None,
        state,
    )

    data = await state.get_data()
    assert data["new_pkg_size"] == 15


# --------------------------------------------------------------------------- #
# Stale state must not masquerade as a fresh answer
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_duration_without_a_size_is_refused(screen: SimpleNamespace) -> None:
    """A stale button from an abandoned wizard has no size behind it."""
    state = _state()
    await state.update_data(new_pkg_category_id=1, new_pkg_kind="volume")
    await state.set_state(AdminStates.store_waiting_new_pkg_duration)

    callback = _callback(f"{StoreCB.PKG_DUR}30")
    await store_pkg_duration_pick(
        callback,  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        None,
        state,
    )

    callback.answer.assert_awaited_once_with(texts.admin_store_lost_context, show_alert=True)
    assert await state.get_state() == AdminStates.store_waiting_new_pkg_duration.state


@pytest.mark.asyncio
async def test_a_finished_wizard_leaves_no_size_for_the_next_one(
    screen: SimpleNamespace,
) -> None:
    """Otherwise an abandoned wizard silently donates its size to the next plan.

    This is almost certainly why the flow looked like it worked once: a leftover
    ``new_pkg_size`` from an earlier run made the duration step find a number it
    was never handed.
    """
    state = await _at_size_step()
    await store_pkg_size_pick(
        _callback(f"{StoreCB.PKG_SIZE}50"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        state,
    )
    await store_pkg_duration_pick(
        _callback(f"{StoreCB.PKG_DUR}30"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        None,
        state,
    )

    data = await state.get_data()
    assert data.get("new_pkg_size_pending") is None

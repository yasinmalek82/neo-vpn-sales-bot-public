"""«📊 وضعیت پنل»: the PRIMARY button, the status screen and the reconnect buttons."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from bot.config import Settings
from bot.handlers import admin as ha
from bot.keyboards.admin import AdminCB, panel_settings_keyboard, panel_status_keyboard
from bot.keyboards.style import PRIMARY
from services.panel_status import PanelStatusView, ReconnectableNode

OWNER_ID = 4242
STAFF_ID = 77
SETTINGS = Settings(
    _env_file=None,  # type: ignore[call-arg]
    BOT_TOKEN="test-token",
    ADMIN_TELEGRAM_IDS=[OWNER_ID, STAFF_ID],
    STORE_OWNER_TELEGRAM_IDS=[OWNER_ID],
)


def _buttons(markup: Any) -> list[Any]:
    return [b for row in markup.inline_keyboard for b in row]


def _cbs(markup: Any) -> list[str]:
    return [b.callback_data or "" for b in _buttons(markup)]


def _view(nodes: list[ReconnectableNode]) -> PanelStatusView:
    return PanelStatusView(
        panel="PANEL-BLOCK",
        users="USERS-BLOCK",
        traffic="TRAFFIC-BLOCK",
        nodes="NODES-BLOCK",
        reconnectable=nodes,
    )


class _Client:
    def __init__(self) -> None:
        self.aclose = AsyncMock()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> _Client:
    c = _Client()
    monkeypatch.setattr(ha, "get_pasarguard_client", AsyncMock(return_value=c))
    monkeypatch.setattr(ha, "_tg_message", lambda cb: cb.message)
    return c


def _cb(data: str, uid: int = STAFF_ID) -> SimpleNamespace:
    screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    return SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=uid), answer=AsyncMock(), message=screen
    )


@pytest.mark.parametrize("can_edit", [True, False])
def test_status_button_is_the_one_primary_and_first(can_edit: bool) -> None:
    markup = panel_settings_keyboard(can_edit=can_edit)
    first = markup.inline_keyboard[0]
    assert [b.callback_data for b in first] == [AdminCB.PANEL_STATUS]
    assert first[0].style == PRIMARY
    styled_primary = [b for b in _buttons(markup) if getattr(b, "style", None) == PRIMARY]
    assert len(styled_primary) == 1


def test_reconnect_buttons_only_for_reconnectable_nodes() -> None:
    markup = panel_status_keyboard([ReconnectableNode(id=2, name="tunnel-b")])
    cbs = _cbs(markup)
    assert cbs[0] == f"{AdminCB.PANEL_NODE_RECONNECT}2"
    assert cbs[1:3] == [AdminCB.PANEL_STATUS, AdminCB.PANEL]
    assert len(cbs) == 4  # reconnect, refresh, back, home
    none = _cbs(panel_status_keyboard([]))
    assert not any(c.startswith(AdminCB.PANEL_NODE_RECONNECT) for c in none)
    assert len(none) == 3


@pytest.mark.asyncio
async def test_screen_renders_service_text_and_closes_client(
    client: _Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    load = AsyncMock(return_value=_view([ReconnectableNode(id=5, name="n5")]))
    monkeypatch.setattr(ha, "load_panel_status", load)
    cb = _cb(AdminCB.PANEL_STATUS)
    await ha.admin_panel_status(cb, None, SETTINGS, None, SimpleNamespace(clear=AsyncMock()))  # type: ignore[arg-type]
    body = cb.message.edit_text.await_args.args[0]
    assert body == _view([]).text
    assert f"{AdminCB.PANEL_NODE_RECONNECT}5" in _cbs(
        cb.message.edit_text.await_args.kwargs["reply_markup"]
    )
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconnect_calls_once_and_rerenders_with_line(
    client: _Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconnect = AsyncMock(return_value=(True, "LINE-SENT"))
    monkeypatch.setattr(ha, "reconnect_node_result", reconnect)
    monkeypatch.setattr(ha, "load_panel_status", AsyncMock(return_value=_view([])))
    cb = _cb(f"{AdminCB.PANEL_NODE_RECONNECT}7")
    await ha.admin_panel_node_reconnect(cb, None, SETTINGS, None)  # type: ignore[arg-type]
    reconnect.assert_awaited_once_with(client, 7)
    body = cb.message.edit_text.await_args.args[0]
    assert body.startswith("LINE-SENT") and "NODES-BLOCK" in body


@pytest.mark.asyncio
async def test_non_admin_cannot_reconnect(client: _Client, monkeypatch: pytest.MonkeyPatch) -> None:
    reconnect = AsyncMock(return_value=(True, "x"))
    monkeypatch.setattr(ha, "reconnect_node_result", reconnect)
    cb = _cb(f"{AdminCB.PANEL_NODE_RECONNECT}7", uid=1)
    await ha.admin_panel_node_reconnect(cb, None, SETTINGS, None)  # type: ignore[arg-type]
    reconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_load_failure_and_missing_client_still_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ha, "_tg_message", lambda cb: cb.message)
    monkeypatch.setattr(ha, "get_pasarguard_client", AsyncMock(side_effect=RuntimeError("no key")))
    cb = _cb(AdminCB.PANEL_STATUS)
    await ha.admin_panel_status(cb, None, SETTINGS, None, SimpleNamespace(clear=AsyncMock()))  # type: ignore[arg-type]
    assert cb.message.edit_text.await_count == 1
    assert AdminCB.PANEL_STATUS in _cbs(cb.message.edit_text.await_args.kwargs["reply_markup"])

"""Service support tools (P2): devices, link/QR, revoke, the card's extra lines.

The card's three extra panel reads only decorate the screen, so the rule under
test is «omit on failure»; the two writes (reset, revoke) are money-adjacent
enough to be asserted call-by-call.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from api_client import PasarGuardClient
from api_client.exceptions import PasarGuardError
from api_client.models import UserHWIDListResponse, UserSubscriptionUpdateList
from bot.config import Settings
from bot.handlers import account as hacc
from bot.handlers import admin as ha
from bot.keyboards.admin import (
    AdminCB,
    service_devices_keyboard,
    service_ops_keyboard,
)
from database.base import Base
from database.models import PasarGuardAccount, User
from services.service_ops import (
    ServiceSnapshot,
    get_snapshot,
    get_support_extras,
    revoke_link,
)

ADMIN = 7
SETTINGS = Settings(
    _env_file=None,  # type: ignore[call-arg]
    BOT_TOKEN="test-token",
    ADMIN_TELEGRAM_IDS=[ADMIN],
)
NOW = datetime.now(UTC)
KEY = "pg_key_11111111-2222-4333-8444-555555555555"


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _owner_and_account(session: AsyncSession) -> tuple[User, PasarGuardAccount]:
    user = User(
        telegram_id=555, username="cust", first_name="C", language_code="fa", referral_code="R1"
    )
    session.add(user)
    await session.flush()
    account = PasarGuardAccount(
        user_id=user.id,
        panel_user_id=4242,
        panel_username="reza_a7f3",
        subscription_url="https://panel.example.com/sub/old",
    )
    session.add(account)
    await session.flush()
    return user, account


def _user_json(**kw: Any) -> dict[str, Any]:
    data = {
        "id": 4242,
        "username": "reza_a7f3",
        "status": "active",
        "used_traffic": 0,
        "created_at": NOW.isoformat(),
        "subscription_url": "https://panel.example.com/sub/new",
        "hwid_limit": 3,
        "online_at": (NOW - timedelta(hours=2)).isoformat(),
    }
    data.update(kw)
    return data


class FakePanel:
    """Just enough panel for the support screens; counts every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.devices = 2
        self.fail: set[str] = set()
        self.revoke_fails = False

    def _maybe_fail(self, what: str) -> None:
        if what in self.fail:
            raise PasarGuardError("down")

    async def get_user(self, username: str) -> Any:
        self.calls.append(("get_user", username))
        return SimpleNamespace(
            username=username,
            status=SimpleNamespace(value="active"),
            expire=None,
            data_limit=None,
            used_traffic=0,
            online_at=NOW - timedelta(hours=2),
            hwid_limit=3,
            subscription_url="https://panel.example.com/sub/new",
        )

    async def get_user_hwids(self, panel_user_id: int) -> UserHWIDListResponse:
        self.calls.append(("get_user_hwids", panel_user_id))
        self._maybe_fail("hwids")
        return UserHWIDListResponse.model_validate(
            {
                "count": self.devices,
                "hwids": [
                    {
                        "id": i,
                        "hwid": f"h{i}",
                        "device_os": "Android",
                        "os_version": "13",
                        "device_model": "Samsung SM-A515F",
                        "created_at": NOW.isoformat(),
                    }
                    for i in range(self.devices)
                ],
            }
        )

    async def reset_user_hwids(self, panel_user_id: int) -> None:
        self.calls.append(("reset_user_hwids", panel_user_id))
        self.devices = 0

    async def list_sub_updates(self, username: str, **_paging: Any) -> Any:
        self.calls.append(("list_sub_updates", username))
        self._maybe_fail("updates")
        return UserSubscriptionUpdateList.model_validate(
            {
                "count": 1,
                "updates": [
                    {
                        # Read the clock here, not at import: once the suite ran
                        # past a minute, the module-level NOW made this «۶ دقیقه».
                        "created_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
                        "user_agent": "v2rayNG/1.8.5 (Android)",
                    }
                ],
            }
        )

    async def revoke_user_subscription(self, username: str) -> Any:
        self.calls.append(("revoke", username))
        if self.revoke_fails:
            raise PasarGuardError("boom")
        return await self.get_user(username)

    @staticmethod
    def subscription_url_from_response(user: Any) -> str | None:
        return user.subscription_url

    async def aclose(self) -> None:
        return None


def _patch_screen(monkeypatch: pytest.MonkeyPatch, panel: FakePanel | None = None) -> Any:
    screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(ha, "_tg_message", lambda _cb: screen)
    if panel is not None:

        @asynccontextmanager
        async def _fake_panel(_s: Any, _st: Any) -> AsyncIterator[FakePanel]:
            yield panel

        monkeypatch.setattr(ha, "_panel", _fake_panel)
    return screen


def _call(data: str) -> Any:
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=ADMIN), answer=AsyncMock())


def _cbs(markup: Any) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# --------------------------------------------------------------------------- #
# Client: method, path, params
# --------------------------------------------------------------------------- #


async def _client_with(handler: Any) -> tuple[PasarGuardClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://panel.example.com"
    )
    return PasarGuardClient(base_url="https://panel.example.com", api_key=KEY, client=http), http


@pytest.mark.asyncio
async def test_client_hwid_and_sub_update_paths() -> None:
    seen: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/hwids/reset"):
            return httpx.Response(200, json={})
        if request.url.path.endswith("/hwids"):
            return httpx.Response(
                200,
                json={
                    "count": 1,
                    "hwids": [
                        {
                            "id": 9,
                            "hwid": "abc",
                            "device_os": "iOS",
                            "created_at": NOW.isoformat(),
                            "surprise": 1,
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "count": 1,
                "updates": [{"created_at": NOW.isoformat(), "user_agent": "Hiddify/2", "ip": None}],
            },
        )

    client, http = await _client_with(handler)
    async with http:
        listing = await client.get_user_hwids(4242)
        await client.reset_user_hwids(4242)
        updates = await client.list_sub_updates("reza_a7f3", limit=3)
    assert listing.count == 1 and listing.hwids[0].device_os == "iOS"
    assert updates.updates[0].user_agent == "Hiddify/2"
    assert seen == [
        ("GET", "/api/user/4242/hwids", {}),
        ("POST", "/api/user/4242/hwids/reset", {}),
        ("GET", "/api/user/reza_a7f3/sub_update", {"offset": "0", "limit": "3"}),
    ]


# --------------------------------------------------------------------------- #
# Snapshot + card lines
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_snapshot_carries_online_limit_and_panel_id(session: AsyncSession) -> None:
    _u, account = await _owner_and_account(session)
    snap = await get_snapshot(session, FakePanel(), account_id=account.id)  # type: ignore[arg-type]
    assert snap.hwid_limit == 3 and snap.panel_user_id == 4242 and snap.online_at is not None


def test_old_snapshot_constructor_still_works() -> None:
    snap = ServiceSnapshot("x", "active", None, None, 0)
    assert snap.online_at is None and snap.hwid_limit is None


def _snap(**kw: Any) -> ServiceSnapshot:
    base: dict[str, Any] = {
        "panel_username": "reza_a7f3",
        "status": "active",
        "expire_unix": None,
        "data_limit_bytes": None,
        "used_traffic_bytes": 0,
        "online_at": NOW - timedelta(hours=2),
        "hwid_limit": 3,
        "panel_user_id": 4242,
    }
    base.update(kw)
    return ServiceSnapshot(**base)


@pytest.mark.asyncio
async def test_card_shows_the_three_lines() -> None:
    panel = FakePanel()
    extras = await get_support_extras(panel, _snap())  # type: ignore[arg-type]
    text = ha._service_text(_snap(), name="reza", extras=extras)
    assert "آخرین اتصال: ۲ ساعت" in text
    assert "دستگاه‌ها: ۲ از ۳" in text
    assert "آخرین به‌روزرسانی لینک: v2rayNG • ۵ دقیقه" in text


@pytest.mark.asyncio
async def test_unlimited_device_limit_reads_unlimited() -> None:
    extras = await get_support_extras(FakePanel(), _snap(hwid_limit=0))  # type: ignore[arg-type]
    text = ha._service_text(_snap(hwid_limit=0), name="r", extras=extras)
    assert "بدون محدودیت" in text and "۲ از ۰" not in text


@pytest.mark.asyncio
async def test_card_omits_a_line_when_its_read_fails() -> None:
    panel = FakePanel()
    panel.fail = {"hwids", "updates"}
    extras = await get_support_extras(panel, _snap())  # type: ignore[arg-type]
    text = ha._service_text(_snap(online_at=None), name="reza", extras=extras)
    assert "دستگاه‌ها" not in text
    assert "آخرین به‌روزرسانی" not in text
    assert "آخرین اتصال" not in text
    assert "reza" in text  # the card itself still renders


@pytest.mark.asyncio
async def test_card_renders_when_the_whole_extras_read_explodes(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _u, account = await _owner_and_account(session)
    screen = _patch_screen(monkeypatch, FakePanel())
    monkeypatch.setattr(ha, "get_support_extras", AsyncMock(side_effect=RuntimeError("x")))
    await ha._render_service(screen, session, SETTINGS, account.id)  # type: ignore[arg-type]
    assert screen.edit_text.await_count == 1


def test_app_name_is_cut_at_the_slash_and_24_chars() -> None:
    from services.service_ops import _app_name

    assert _app_name("v2rayNG/1.8.5 (Android)") == "v2rayNG"
    assert len(_app_name("A" * 60 + "/1")) == 24


# --------------------------------------------------------------------------- #
# Keyboards
# --------------------------------------------------------------------------- #


def test_service_card_has_the_two_new_buttons_and_callbacks_are_safe() -> None:
    cbs = _cbs(service_ops_keyboard(7, 3, disabled=False))
    assert f"{AdminCB.SVC_DEVICES}7" in cbs and f"{AdminCB.SVC_LINK}7" in cbs
    new = [
        AdminCB.SVC_DEVICES,
        AdminCB.SVC_DEV_RESET,
        AdminCB.SVC_DEV_RESET_OK,
        AdminCB.SVC_LINK,
        AdminCB.SVC_LINK_SEND,
        AdminCB.SVC_LINK_QR,
        AdminCB.SVC_LINK_NEW,
        AdminCB.SVC_LINK_NEW_OK,
    ]
    others = [
        v
        for k, v in vars(AdminCB).items()
        if isinstance(v, str) and not k.startswith("_") and v not in new
    ]
    for n in new:
        assert len(f"{n}{2**40}".encode()) <= 64
        assert not any(o.startswith(n) or n.startswith(o) for o in others), n
        assert not any(m != n and (m.startswith(n) or n.startswith(m)) for m in new), n


def test_reset_button_only_when_there_are_devices() -> None:
    assert not any(
        c.startswith(AdminCB.SVC_DEV_RESET) for c in _cbs(service_devices_keyboard(1, count=0))
    )
    kb = service_devices_keyboard(1, count=2)
    assert f"{AdminCB.SVC_DEV_RESET}1" in _cbs(kb)
    row = next(r for r in kb.inline_keyboard if r[0].callback_data == f"{AdminCB.SVC_DEV_RESET}1")
    assert len(row) == 1 and row[0].style == "danger"


# --------------------------------------------------------------------------- #
# Devices: list, confirm, reset exactly once
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_devices_screen_lists_devices(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _u, account = await _owner_and_account(session)
    panel = FakePanel()
    screen = _patch_screen(monkeypatch, panel)
    monkeypatch.setattr(ha, "_panel", _panel_of(panel))
    await ha.admin_service_devices(
        _call(f"{AdminCB.SVC_DEVICES}{account.id}"), session, SETTINGS, None
    )
    body = screen.edit_text.await_args.args[0]
    assert "۱. Samsung SM-A515F • Android 13" in body
    assert ("get_user_hwids", 4242) in panel.calls  # path uses the panel user id


def _panel_of(panel: FakePanel) -> Any:
    @asynccontextmanager
    async def _fake(_s: Any, _st: Any) -> AsyncIterator[FakePanel]:
        yield panel

    return _fake


@pytest.mark.asyncio
async def test_reset_confirm_does_not_write_and_apply_writes_once(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _u, account = await _owner_and_account(session)
    panel = FakePanel()
    screen = _patch_screen(monkeypatch, panel)
    await ha.admin_service_devices_reset_confirm(
        _call(f"{AdminCB.SVC_DEV_RESET}{account.id}"), session, SETTINGS, None
    )
    assert not [c for c in panel.calls if c[0] == "reset_user_hwids"]
    assert f"{AdminCB.SVC_DEV_RESET_OK}{account.id}" in _cbs(
        screen.edit_text.await_args.kwargs["reply_markup"]
    )

    await ha.admin_service_devices_reset_apply(
        _call(f"{AdminCB.SVC_DEV_RESET_OK}{account.id}"), session, SETTINGS, None
    )
    assert [c for c in panel.calls if c[0] == "reset_user_hwids"] == [("reset_user_hwids", 4242)]
    body = screen.edit_text.await_args.args[0]
    assert "✅" in body and "هیچ دستگاهی" in body


# --------------------------------------------------------------------------- #
# Revoke: one shared function, both call sites
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_revoke_link_updates_subscription_url(session: AsyncSession) -> None:
    _u, account = await _owner_and_account(session)
    panel = FakePanel()
    assert await revoke_link(session, panel, account_id=account.id) == (  # type: ignore[arg-type]
        "https://panel.example.com/sub/new"
    )
    assert account.subscription_url == "https://panel.example.com/sub/new"


@pytest.mark.asyncio
async def test_revoke_link_recovers_by_reading_the_user_and_fails_cleanly(
    session: AsyncSession,
) -> None:
    _u, account = await _owner_and_account(session)
    panel = FakePanel()
    panel.revoke_fails = True
    assert await revoke_link(session, panel, account_id=account.id)  # type: ignore[arg-type]
    assert ("get_user", "reza_a7f3") in panel.calls

    async def _boom(_u: str) -> Any:
        raise PasarGuardError("down")

    panel.get_user = _boom  # type: ignore[method-assign]
    assert await revoke_link(session, panel, account_id=account.id) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_customer_revoke_path_goes_through_the_shared_function(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, account = await _owner_and_account(session)
    other = User(
        telegram_id=9, username="o", first_name="O", language_code="fa", referral_code="R9"
    )
    session.add(other)
    await session.flush()
    panel = FakePanel()
    monkeypatch.setattr(hacc, "get_pasarguard_client", AsyncMock(return_value=panel))
    url = await hacc._revoke_subscription_link(
        session, SETTINGS, user_id=user.id, account_id=account.id
    )
    assert url == "https://panel.example.com/sub/new"
    assert account.subscription_url == url
    # someone else's account is refused before the panel is touched
    panel.calls.clear()
    assert (
        await hacc._revoke_subscription_link(
            session, SETTINGS, user_id=other.id, account_id=account.id
        )
        is None
    )
    assert not [c for c in panel.calls if c[0] == "revoke"]


@pytest.mark.asyncio
async def test_admin_revoke_confirm_warns_then_apply_rotates(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _u, account = await _owner_and_account(session)
    panel = FakePanel()
    screen = _patch_screen(monkeypatch, panel)
    await ha.admin_service_link_revoke_confirm(
        _call(f"{AdminCB.SVC_LINK_NEW}{account.id}"), session, SETTINGS, None
    )
    assert "کار نمی‌کنه" in screen.edit_text.await_args.args[0]
    assert not [c for c in panel.calls if c[0] == "revoke"]
    await ha.admin_service_link_revoke_apply(
        _call(f"{AdminCB.SVC_LINK_NEW_OK}{account.id}"), session, SETTINGS, None
    )
    assert [c for c in panel.calls if c[0] == "revoke"] == [("revoke", "reza_a7f3")]
    body = screen.edit_text.await_args.args[0]
    assert "sub/new" in body
    assert f"{AdminCB.SVC_LINK_SEND}{account.id}" in _cbs(
        screen.edit_text.await_args.kwargs["reply_markup"]
    )


# --------------------------------------------------------------------------- #
# Link screen: send to the customer, QR to the admin
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_send_to_customer_targets_the_customers_chat(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, account = await _owner_and_account(session)
    deliver = AsyncMock(return_value=True)
    monkeypatch.setattr(ha, "deliver_provisioned_service_card", deliver)
    call = _call(f"{AdminCB.SVC_LINK_SEND}{account.id}")
    bot = SimpleNamespace()
    await ha.admin_service_link_send(call, session, SETTINGS, None, bot)  # type: ignore[arg-type]
    kw = deliver.await_args.kwargs
    assert kw["chat_id"] == 555 != ADMIN
    assert kw["user_id"] == user.id and kw["account_id"] == account.id
    assert kw["bot"] is bot
    assert kw["intro"] == ha.texts.notify_service_link_resent


@pytest.mark.asyncio
async def test_send_failure_is_reported_not_raised(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _u, account = await _owner_and_account(session)
    monkeypatch.setattr(ha, "deliver_provisioned_service_card", AsyncMock(side_effect=RuntimeError))
    call = _call(f"{AdminCB.SVC_LINK_SEND}{account.id}")
    await ha.admin_service_link_send(call, session, SETTINGS, None, SimpleNamespace())  # type: ignore[arg-type]
    assert call.answer.await_args.kwargs["show_alert"] is True


@pytest.mark.asyncio
async def test_qr_goes_to_the_admin_with_the_link_and_a_back_button(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _u, account = await _owner_and_account(session)
    monkeypatch.setattr(ha, "service_qr_png", AsyncMock(return_value=b"png"))
    bot = SimpleNamespace(send_photo=AsyncMock())
    await ha.admin_service_link_qr(
        _call(f"{AdminCB.SVC_LINK_QR}{account.id}"), session, SETTINGS, None, bot  # type: ignore[arg-type]
    )
    kw = bot.send_photo.await_args.kwargs
    assert kw["chat_id"] == ADMIN
    assert "sub/old" in kw["caption"]
    cbs = _cbs(kw["reply_markup"])
    assert f"{AdminCB.SVC_LINK}{account.id}" in cbs
    assert not any(c.startswith(AdminCB.CUSTOMER_PICK) for c in cbs)


def test_notify_copy_follows_the_brand_voice() -> None:
    text = ha.texts.notify_service_link_resent
    assert "شما" not in text
    assert sum(text.count(e) for e in ("🔗", "📱", "✅", "📅")) <= 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit", "line"), [(3, "دستگاه‌ها: ۲ از ۳"), (0, "دستگاه‌ها: ۲ (بدون محدودیت)")]
)
async def test_the_devices_line_ends_where_its_value_ends(limit: int, line: str) -> None:
    """Live: «دستگاه‌ها: ۰ از ۲ از», a dangling «از» from a template filled twice."""
    snap = _snap(hwid_limit=limit)
    extras = await get_support_extras(FakePanel(), snap)  # type: ignore[arg-type]
    text = ha._service_text(snap, name="reza", extras=extras)
    assert line in text.splitlines(), text

"""Who may open which panel path: admin, reseller, customer, and nobody else.

The signature decides. These tests build ``initData`` the documented way
(``web_panel_helpers.init_data``) and ask the real app; nothing about auth is
mocked.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bot.config import Settings
from tests.unit.web_panel_helpers import (
    DB_ADMIN,
    ENV_ADMIN,
    INIT_HEADER,
    STRANGER,
    add_user,
    init_data,
    panel_settings,
)
from tests.unit.web_personal_helpers import Factory, make_person, personal_client
from web.app import mount_web_panel
from web.auth import (
    HOME_PATH,
    PERSONAL_PREFIX,
    RESELLER_PREFIX,
    TEXT_FORBIDDEN,
    is_personal,
    is_public,
    is_reseller_path,
)
from web.nav import ADMIN_FIRST_SCREEN, ADMIN_NAV, CUSTOMER_NAV, RESELLER_NAV

CUSTOMER = 4001
RESELLER = 4002
Pair = tuple[Factory, str]


async def _get(client: TestClient, tg: int | None, path: str, **kwargs):  # type: ignore[no-untyped-def]
    headers = dict(kwargs.pop("headers", {}))
    if tg is not None:
        headers[INIT_HEADER] = init_data(tg)
    return await client.get(path, headers=headers, **kwargs)


def _panel_paths(app: web.Application) -> list[str]:
    return [
        r.canonical
        for r in app.router.resources()
        if r.canonical.startswith("/panel") and r.canonical != "/panel/static"
    ]


async def _seed_people(factory: Factory) -> None:
    await make_person(factory, CUSTOMER, name="مشتری")
    await make_person(factory, RESELLER, name="نماینده", reseller=True)


# --- the second switch --------------------------------------------------------


def test_the_public_switch_ships_off() -> None:
    assert Settings(BOT_TOKEN="test-token").web_panel_public_enabled is False


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [HOME_PATH, PERSONAL_PREFIX, RESELLER_PREFIX])
async def test_with_the_switch_off_a_non_admin_is_refused_on_the_personal_pages(
    two_sessions: Pair, path: str
) -> None:
    factory, _ = two_sessions
    await _seed_people(factory)
    async with personal_client(factory, public=False) as client:
        assert (await _get(client, CUSTOMER, path)).status == 403
        assert (await _get(client, RESELLER, path)).status == 403


@pytest.mark.asyncio
async def test_with_the_switch_on_each_role_opens_its_own_pages(two_sessions: Pair) -> None:
    factory, _ = two_sessions
    await _seed_people(factory)
    async with personal_client(factory, public=True) as client:
        for tg in (CUSTOMER, RESELLER):
            assert (await _get(client, tg, HOME_PATH)).status == 200
            assert (await _get(client, tg, PERSONAL_PREFIX)).status == 200
        assert (await _get(client, RESELLER, RESELLER_PREFIX)).status == 200


@pytest.mark.asyncio
async def test_an_admin_opens_the_personal_pages_with_the_switch_off(two_sessions: Pair) -> None:
    """An admin may look at their own account; the switch is for everyone else."""
    factory, _ = two_sessions
    await add_user(factory, ENV_ADMIN, is_admin=True)
    async with personal_client(factory, public=False) as client:
        assert (await _get(client, ENV_ADMIN, HOME_PATH)).status == 200
        assert (await _get(client, ENV_ADMIN, PERSONAL_PREFIX)).status == 200


@pytest.mark.asyncio
async def test_an_admin_with_no_row_has_a_home_but_no_personal_page(two_sessions: Pair) -> None:
    factory, _ = two_sessions
    async with personal_client(factory, public=False) as client:
        assert (await _get(client, ENV_ADMIN, HOME_PATH)).status == 200
        assert (await _get(client, ENV_ADMIN, PERSONAL_PREFIX)).status == 403
        assert (await _get(client, ENV_ADMIN, "/panel/finance")).status == 200


# --- who is refused -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_customer_is_refused_on_the_reseller_route(two_sessions: Pair) -> None:
    factory, _ = two_sessions
    await _seed_people(factory)
    async with personal_client(factory, public=True) as client:
        resp = await _get(client, CUSTOMER, RESELLER_PREFIX)
        assert resp.status == 403
        assert await resp.text() == TEXT_FORBIDDEN


@pytest.mark.asyncio
async def test_an_admin_who_is_not_a_reseller_is_refused_on_the_reseller_route(
    two_sessions: Pair,
) -> None:
    factory, _ = two_sessions
    await add_user(factory, DB_ADMIN, is_admin=True)
    async with personal_client(factory, public=True) as client:
        assert (await _get(client, DB_ADMIN, RESELLER_PREFIX)).status == 403


@pytest.mark.asyncio
async def test_a_non_admin_is_refused_on_every_admin_route(two_sessions: Pair) -> None:
    """Every registered path outside the personal prefix, found from the router."""
    factory, _ = two_sessions
    await _seed_people(factory)
    app = web.Application()
    mount_web_panel(app, panel_settings(WEB_PANEL_PUBLIC_ENABLED=True), factory)

    async def later(_request: web.Request) -> web.Response:
        return web.Response(text="admin only")

    app.router.add_get("/panel/added-later", later)
    admin_paths = [
        p for p in _panel_paths(app) if not is_personal(p) and not is_public(p) and p != "/panel"
    ]
    assert "/panel/finance" in admin_paths and "/panel/added-later" in admin_paths
    async with TestClient(TestServer(app)) as client:
        for path in admin_paths:
            for tg in (CUSTOMER, RESELLER, STRANGER):
                assert (await _get(client, tg, path)).status == 403, (tg, path)
            # An admin is let through to the route itself. A write route answers a
            # GET with 405, a path with a slug in it with 404, and an owner-only page
            # with its own 403 line: none of them is the login's refusal.
            let_in = await _get(client, ENV_ADMIN, path)
            assert let_in.status != 401, path
            assert (await let_in.text()) != TEXT_FORBIDDEN, path
        for path in ("/panel/finance", "/panel/added-later"):
            assert (await _get(client, ENV_ADMIN, path)).status == 200, path


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [HOME_PATH, PERSONAL_PREFIX, RESELLER_PREFIX, "/panel/finance"])
async def test_a_banned_person_is_refused_everywhere(two_sessions: Pair, path: str) -> None:
    factory, _ = two_sessions
    await make_person(factory, 4101, banned=True)
    await make_person(factory, 4102, banned=True, reseller=True)
    await add_user(factory, DB_ADMIN, is_admin=True, is_banned=True)
    async with personal_client(factory, public=True) as client:
        for tg in (4101, 4102, DB_ADMIN):
            assert (await _get(client, tg, path)).status == 403, tg


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [HOME_PATH, PERSONAL_PREFIX, RESELLER_PREFIX])
async def test_a_signed_user_with_no_row_is_refused_on_the_personal_pages(
    two_sessions: Pair, path: str
) -> None:
    factory, _ = two_sessions
    async with personal_client(factory, public=True) as client:
        assert (await _get(client, STRANGER, path)).status == 403


@pytest.mark.asyncio
async def test_an_unsigned_request_is_refused_on_the_personal_pages(two_sessions: Pair) -> None:
    factory, _ = two_sessions
    await _seed_people(factory)
    async with personal_client(factory, public=True) as client:
        for path in (HOME_PATH, PERSONAL_PREFIX, RESELLER_PREFIX):
            assert (await _get(client, None, path)).status == 401


# --- the prefix itself --------------------------------------------------------


def test_only_the_personal_prefix_is_open_to_a_non_admin() -> None:
    assert is_personal("/panel/me") and is_personal("/panel/me/home")
    assert is_personal("/panel/me/reseller")
    for other in (
        "/panel/finance",
        "/panel/mex",
        "/panel/me-admin",
        "/panel/meeting",
        "/panel/me/../finance",
        "/panel/store/me",
        "/panel",
    ):
        assert not is_personal(other), other
    assert is_reseller_path("/panel/me/reseller") and is_reseller_path("/panel/me/reseller/x")
    assert not is_reseller_path("/panel/me") and not is_reseller_path("/panel/me/resellers")


# --- the user id comes from the signature only ---------------------------------


@pytest.mark.asyncio
async def test_a_user_id_smuggled_in_the_request_changes_nothing(two_sessions: Pair) -> None:
    from tests.unit.web_personal_helpers import make_service

    factory, _ = two_sessions
    mine = await make_person(factory, CUSTOMER, name="خودم")
    other = await make_person(factory, 4003, name="دیگری")
    await make_service(factory, mine, username="mine_aaaa", label="سرویس-من-111")
    await make_service(factory, other, username="other_bbbb", label="سرویس-او-222")
    smuggled = {
        "user_id": str(other),
        "id": str(other),
        "telegram_id": "4003",
        "tg": "4003",
        "role": "admin",
    }
    async with personal_client(factory, public=True) as client:
        resp = await _get(
            client,
            CUSTOMER,
            PERSONAL_PREFIX,
            params=smuggled,
            headers={
                "X-User-Id": str(other),
                "X-Telegram-Id": "4003",
                "X-Telegram-User": '{"id": 4003}',
                "X-Forwarded-User": "4003",
                "Cookie": "user_id=4003; telegram_id=4003",
            },
        )
        assert resp.status == 200
        html = await resp.text()
        assert "سرویس-من-111" in html and "سرویس-او-222" not in html
        # Another person's signed data in a second header is not read either.
        both = await client.get(
            PERSONAL_PREFIX,
            headers={INIT_HEADER: init_data(CUSTOMER), "X-Init-Data-2": init_data(4003)},
        )
        assert "سرویس-او-222" not in await both.text()
        # A form body is not read: the routes answer GET only.
        posted = await client.post(
            PERSONAL_PREFIX,
            data={"user_id": str(other)},
            headers={INIT_HEADER: init_data(CUSTOMER)},
        )
        assert posted.status == 405
        # And a smuggled role does not turn a customer into a reseller or admin.
        assert (await _get(client, CUSTOMER, RESELLER_PREFIX, params=smuggled)).status == 403
        assert (await _get(client, CUSTOMER, "/panel/finance", params=smuggled)).status == 403


# --- /panel/me/home: each role gets its own navigation -------------------------


@pytest.mark.asyncio
async def test_home_gives_each_role_its_own_nav_and_first_screen(two_sessions: Pair) -> None:
    factory, _ = two_sessions
    await _seed_people(factory)
    await add_user(factory, ENV_ADMIN, is_admin=True)
    expected = {
        ENV_ADMIN: ADMIN_NAV,
        RESELLER: RESELLER_NAV,
        CUSTOMER: CUSTOMER_NAV,
    }
    async with personal_client(factory, public=True) as client:
        for tg, nav in expected.items():
            resp = await _get(client, tg, HOME_PATH)
            assert resp.status == 200
            html = await resp.text()
            for item in nav.items:
                assert f'hx-get="{item.url}"' in html and item.label in html
            assert f'<div hx-get="{nav.first_screen}" hx-trigger="load"' in html
            others = [n for n in expected.values() if n is not nav]
            for other in others:
                for item in other.items:
                    if item.url not in {i.url for i in nav.items}:
                        assert f'hx-get="{item.url}"' not in html, (tg, item.url)


@pytest.mark.asyncio
async def test_home_never_gives_a_non_admin_an_admin_link(two_sessions: Pair) -> None:
    factory, _ = two_sessions
    await _seed_people(factory)
    async with personal_client(factory, public=True) as client:
        for tg in (CUSTOMER, RESELLER):
            html = await (await _get(client, tg, HOME_PATH)).text()
            for item in ADMIN_NAV.items:
                assert item.url not in html, (tg, item.url)
            assert "/panel/finance" not in html


def test_the_admin_nav_lists_its_screens_and_its_first_screen_is_one_named_constant() -> None:
    assert [(i.label, i.url) for i in ADMIN_NAV.items] == [
        ("فروشگاه", "/panel/store"),
        ("مالی", "/panel/finance"),
        ("مشتری‌ها", "/panel/customers"),
        ("نماینده‌ها", "/panel/resellers"),
        ("کدهای تخفیف", "/panel/promo"),
        ("کمپین‌ها", "/panel/marketing"),
        ("تنظیمات", "/panel/settings"),
    ]
    # The owner's choice of 2026-10-10: the panel opens on the store.
    assert ADMIN_NAV.first_screen == ADMIN_FIRST_SCREEN == "/panel/store"
    assert ADMIN_FIRST_SCREEN in {i.url for i in ADMIN_NAV.items}
    # Whatever a role's first screen is, it is inside the paths that role may open.
    assert RESELLER_NAV.first_screen == RESELLER_PREFIX
    assert CUSTOMER_NAV.first_screen == PERSONAL_PREFIX

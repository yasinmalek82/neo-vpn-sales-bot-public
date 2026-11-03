"""Who may open the panel: signature, freshness, then the person.

``initData`` is built in ``web_panel_helpers`` from Telegram's published
algorithm; the verifier is not mocked.
"""

from __future__ import annotations

import logging

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.web_panel_helpers import (
    DB_ADMIN,
    ENV_ADMIN,
    INIT_HEADER,
    STRANGER,
    TOKEN,
    add_user,
    init_data,
    panel_client,
    panel_settings,
)
from web.app import mount_web_panel
from web.auth import TEXT_FORBIDDEN, TEXT_UNAUTHORIZED, is_public

Factory = tuple[async_sessionmaker[AsyncSession], str]


async def _get(client: TestClient, header: str | None, path: str = "/panel/finance"):
    headers = {INIT_HEADER: header} if header is not None else {}
    return await client.get(path, headers=headers)


@pytest.mark.asyncio
async def test_a_signed_env_admin_gets_the_page(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    async with panel_client(factory) as client:
        resp = await _get(client, init_data(ENV_ADMIN))
        assert resp.status == 200
        assert "no-store" in resp.headers["Cache-Control"]
        assert "گزارش مالی" in await resp.text()


@pytest.mark.asyncio
async def test_a_removed_admin_is_denied_without_a_bot_interaction(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    await add_user(factory, DB_ADMIN, is_admin=True)
    # A loaded configuration granting this ID works even without middleware.
    granted = panel_settings(ADMIN_TELEGRAM_IDS=f"{ENV_ADMIN},{DB_ADMIN}")
    async with panel_client(factory, granted) as client:
        assert (await _get(client, init_data(DB_ADMIN))).status == 200
    # After configuration removal, fresh signed credentials and a stale DB
    # flag cannot retain either read or mutation access.
    async with panel_client(factory) as client:
        assert (await _get(client, init_data(DB_ADMIN))).status == 403
        response = await client.post(
            "/panel/customers/1/wallet/confirm",
            headers={INIT_HEADER: init_data(DB_ADMIN)},
            data={"amount": "2000"},
        )
        assert response.status == 403


@pytest.mark.asyncio
async def test_no_header_is_refused(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    async with panel_client(factory) as client:
        resp = await _get(client, None)
        assert resp.status == 401
        assert (await _get(client, "")).status == 401


@pytest.mark.asyncio
async def test_a_forged_signature_is_refused(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    async with panel_client(factory) as client:
        signed_with_another_token = init_data(ENV_ADMIN, token="999999:SOMEONE-ELSES-BOT")
        assert (await _get(client, signed_with_another_token)).status == 401
        # A real signature for a stranger, with the payload edited to the admin's id.
        edited = init_data(STRANGER, tamper_user_id=ENV_ADMIN)
        assert (await _get(client, edited)).status == 401
        assert (await _get(client, "user=%7B%22id%22%3A111%7D&hash=00")).status == 401


@pytest.mark.asyncio
async def test_a_stale_auth_date_is_refused(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    async with panel_client(factory) as client:
        assert (await _get(client, init_data(ENV_ADMIN, age_seconds=59 * 60))).status == 200
        assert (await _get(client, init_data(ENV_ADMIN, age_seconds=61 * 60))).status == 401


@pytest.mark.asyncio
async def test_an_auth_date_in_the_future_is_refused(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    async with panel_client(factory) as client:
        assert (await _get(client, init_data(ENV_ADMIN, age_seconds=-30))).status == 200
        assert (await _get(client, init_data(ENV_ADMIN, age_seconds=-3600))).status == 401


@pytest.mark.asyncio
async def test_a_valid_signature_of_a_non_admin_is_refused(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    await add_user(factory, STRANGER)
    async with panel_client(factory) as client:
        assert (await _get(client, init_data(STRANGER))).status == 403
        assert (await _get(client, init_data(999_999))).status == 403, "and with no row at all"


@pytest.mark.asyncio
async def test_a_banned_admin_is_refused(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    await add_user(factory, DB_ADMIN, is_admin=True, is_banned=True)
    async with panel_client(factory) as client:
        assert (await _get(client, init_data(DB_ADMIN))).status == 403


@pytest.mark.asyncio
async def test_a_refusal_does_not_say_which_check_failed(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    await add_user(factory, STRANGER)
    async with panel_client(factory) as client:
        bodies_401 = {
            await (await _get(client, h)).text()
            for h in (
                None,
                init_data(ENV_ADMIN, token="1:other"),
                init_data(ENV_ADMIN, age_seconds=7200),
            )
        }
        bodies_403 = {await (await _get(client, init_data(STRANGER))).text()}
        assert bodies_401 == {TEXT_UNAUTHORIZED}
        assert bodies_403 == {TEXT_FORBIDDEN}
        refused = await _get(client, None)
        assert "no-store" in refused.headers["Cache-Control"]


@pytest.mark.asyncio
async def test_a_route_added_later_is_guarded_without_anyone_remembering(
    two_sessions: Factory,
) -> None:
    """Default-deny: the middleware guards every panel path except the listed public ones."""
    factory, _ = two_sessions

    async def secret(_request: web.Request) -> web.Response:
        return web.Response(text="secret")

    app = web.Application()
    mount_web_panel(app, panel_settings(), factory)
    app.router.add_get("/panel/secret", secret)
    async with TestClient(TestServer(app)) as client:
        assert (await _get(client, None, "/panel/secret")).status == 401
        assert (await _get(client, init_data(STRANGER), "/panel/secret")).status == 403
        assert (await _get(client, init_data(ENV_ADMIN), "/panel/secret")).status == 200


def test_only_the_shell_and_the_static_files_are_public() -> None:
    assert is_public("/panel/")
    assert is_public("/panel/static/panel.css")
    assert not is_public("/panel/finance")
    assert not is_public("/panel")
    assert not is_public("/panel/static")
    assert not is_public("/panel/../finance")
    assert not is_public("/panel/static/../finance")


@pytest.mark.asyncio
async def test_the_shell_and_static_files_need_no_header(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    async with panel_client(factory) as client:
        assert (await _get(client, None, "/panel/")).status == 200
        css = await _get(client, None, "/panel/static/panel.css")
        assert css.status == 200
        assert css.headers["Cache-Control"] == "no-cache", "a deploy must reach the phone"
        assert (await _get(client, None, "/panel/static/htmx.min.js")).status == 200


@pytest.mark.asyncio
async def test_neither_the_init_data_nor_the_token_reaches_the_log(
    two_sessions: Factory, caplog: pytest.LogCaptureFixture
) -> None:
    factory, _ = two_sessions
    caplog.set_level(logging.DEBUG)
    forged = init_data(ENV_ADMIN, token="1:other")
    stale = init_data(ENV_ADMIN, age_seconds=7200)
    async with panel_client(factory) as client:
        await _get(client, forged)
        await _get(client, stale)
        await _get(client, init_data(ENV_ADMIN))
    for secret in (forged, stale, TOKEN, "TEST-PANEL-TOKEN"):
        assert secret not in caplog.text

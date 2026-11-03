"""How the bot starts with the panel: refuses a bad URL, serves it in polling mode."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import bot.main as bot_main
from tests.unit.web_panel_helpers import panel_settings
from web.app import mount_web_panel, start_polling_panel


def test_the_bot_refuses_to_start_with_a_bad_panel_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """``main()`` must stop on the URL before it opens the database."""

    def reached_the_database(*_a: Any, **_k: Any) -> None:
        raise AssertionError("main() got past the panel URL check")

    monkeypatch.setattr(bot_main, "settings", panel_settings(WEB_PANEL_URL="http://x.example.com/"))
    monkeypatch.setattr(bot_main, "init_db", reached_the_database)
    with pytest.raises(RuntimeError, match="WEB_PANEL_URL"):
        asyncio.run(bot_main.main())


@pytest.mark.asyncio
async def test_polling_mode_serves_the_shell_when_the_switch_is_on(two_sessions) -> None:  # type: ignore[no-untyped-def]
    factory, _ = two_sessions
    runner = await start_polling_panel(panel_settings(WEBHOOK_PORT=0), factory)
    assert runner is not None
    try:
        port = runner.addresses[0][1]
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{port}/panel/") as resp:
                assert resp.status == 200
            async with http.get(f"http://127.0.0.1:{port}/panel/finance") as resp:
                assert resp.status == 401
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_the_panel_leaves_the_webhook_route_alone(two_sessions) -> None:  # type: ignore[no-untyped-def]
    """The auth middleware acts on /panel only; Telegram's webhook has its own secret."""
    factory, _ = two_sessions

    async def webhook(_request: web.Request) -> web.Response:
        return web.Response(text="webhook ok")

    app = web.Application()
    app.router.add_post("/webhook", webhook)
    mount_web_panel(app, panel_settings(), factory)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/webhook")
        assert resp.status == 200 and await resp.text() == "webhook ok"
        assert "Cache-Control" not in resp.headers

"""The panel ships off, and a bad URL stops the bot at startup."""

from __future__ import annotations

import pytest
from aiohttp import web

from tests.unit.web_panel_helpers import panel_client, panel_settings
from web.app import mount_web_panel, require_web_panel_url, start_polling_panel


def _paths(app: web.Application) -> list[str]:
    return [r.canonical for r in app.router.resources()]


def test_with_the_switch_off_no_panel_route_exists() -> None:
    app = web.Application()
    mounted = mount_web_panel(app, panel_settings(WEB_PANEL_ENABLED=False), None)  # type: ignore[arg-type]
    assert mounted is False
    assert not [p for p in _paths(app) if p.startswith("/panel")]
    assert len(app.router.resources()) == 0


def test_with_the_switch_on_the_shell_and_the_finance_page_are_mounted() -> None:
    app = web.Application()
    assert mount_web_panel(app, panel_settings(), None) is True  # type: ignore[arg-type]
    assert {"/panel/", "/panel/finance", "/panel/static"} <= set(_paths(app))


def test_with_the_switch_off_no_middleware_is_added() -> None:
    """The webhook app must be exactly what it is today."""
    app = web.Application()
    mount_web_panel(app, panel_settings(WEB_PANEL_ENABLED=False), None)  # type: ignore[arg-type]
    assert list(app.middlewares) == []


@pytest.mark.asyncio
async def test_a_host_header_with_a_port_is_served(two_sessions) -> None:  # type: ignore[no-untyped-def]
    """aiohttp 3.9 + current yarl: a sub-app 500s on «Host: x:8080». Plain routes do not."""
    factory, _ = two_sessions
    async with panel_client(factory) as client:
        resp = await client.get("/panel/", headers={"Host": "panel.example.com:8080"})
        assert resp.status == 200


@pytest.mark.asyncio
async def test_polling_mode_binds_no_server_while_the_switch_is_off() -> None:
    runner = await start_polling_panel(panel_settings(WEB_PANEL_ENABLED=False), None)  # type: ignore[arg-type]
    assert runner is None


@pytest.mark.parametrize(
    "url", ["", "   ", "http://panel.example.com/panel/", "https://", "panel.example.com"]
)
def test_enabled_with_an_empty_or_non_https_url_fails(url: str) -> None:
    with pytest.raises(RuntimeError, match="WEB_PANEL_URL"):
        require_web_panel_url(panel_settings(WEB_PANEL_URL=url))


def test_a_good_url_passes_and_a_disabled_panel_needs_none() -> None:
    assert (
        require_web_panel_url(panel_settings(WEB_PANEL_URL=" https://p.example.com/panel/ "))
        == "https://p.example.com/panel/"
    )
    assert require_web_panel_url(panel_settings(WEB_PANEL_ENABLED=False, WEB_PANEL_URL="")) == ""


def test_mount_itself_refuses_a_bad_url() -> None:
    with pytest.raises(RuntimeError, match="WEB_PANEL_URL"):
        mount_web_panel(web.Application(), panel_settings(WEB_PANEL_URL="http://x.example.com/"), None)  # type: ignore[arg-type]


def test_the_panel_ships_off_and_the_bot_button_link_follows_the_switch() -> None:
    from bot.config import Settings

    fresh = Settings(BOT_TOKEN="test-token")
    assert fresh.web_panel_enabled is False and fresh.web_panel_link == ""
    on = panel_settings(WEB_PANEL_URL=" https://p.example.com/panel/ ")
    assert on.web_panel_link == "https://p.example.com/panel/"
    off_with_url = panel_settings(WEB_PANEL_ENABLED=False)
    assert off_with_url.web_panel_link == "", "a URL with the switch off still gives no button"

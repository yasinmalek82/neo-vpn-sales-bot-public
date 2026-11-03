"""Attach the panel to the bot's aiohttp app.

``mount_web_panel`` is the only way a route gets onto the bot's server, and it
adds nothing (no route, no middleware) while ``WEB_PANEL_ENABLED`` is off. Its
two middlewares act on ``/panel`` paths only, so the webhook route is untouched.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from web.auth import panel_auth, response_headers
from web.bank_sms import mount_bank_sms
from web.keys import PANEL_PREFIX, SESSION_FACTORY, SETTINGS, TEMPLATES
from web.routes import customers, finance, marketing, me, promo, resellers, shell, store
from web.routes import settings as settings_screen
from web.templating import build_environment

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def require_web_panel_url(settings: Settings) -> str:
    """The stripped public URL, or fail closed when the panel is on without a usable one.

    Telegram opens a Mini App only over https, so an empty or plain-http URL
    would give a button that does nothing. Fail at startup, not on the phone.
    """
    url = (settings.web_panel_url or "").strip()
    if not settings.web_panel_enabled:
        return url
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc or any(c.isspace() for c in url):
        raise RuntimeError(
            "WEB_PANEL_URL must be a public https URL (https://host/panel/) "
            "when WEB_PANEL_ENABLED=true; Telegram opens Mini Apps only over https"
        )
    if not parts.path.rstrip("/").endswith(PANEL_PREFIX):
        logger.warning(
            "WEB_PANEL_URL does not end in %s/: the shell page lives there", PANEL_PREFIX
        )
    return url


def mount_web_panel(
    app: web.Application,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    """Add the panel under ``/panel`` when the switch is on. True when it did.

    Plain routes on the bot's app, not an ``add_subapp``: aiohttp 3.9 resolves
    a sub-app through ``request.url``, which the current yarl refuses for a
    ``Host`` header that carries a port (a 500 on every panel request).
    """
    if not settings.web_panel_enabled:
        return False
    require_web_panel_url(settings)
    app[SETTINGS] = settings
    app[SESSION_FACTORY] = session_factory
    app[TEMPLATES] = build_environment()
    app.middlewares.extend([response_headers, panel_auth])
    for screen in (
        shell,
        finance,
        store,
        customers,
        resellers,
        promo,
        marketing,
        settings_screen,
        me,
    ):
        app.router.add_routes(screen.routes)
    app.router.add_static(f"{PANEL_PREFIX}/static", STATIC_DIR, follow_symlinks=False)
    return True


async def start_polling_panel(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> web.AppRunner | None:
    """Polling mode has no HTTP server; with the switch on, start one for the panel.

    Returns the runner to clean up on exit, or ``None`` (and binds nothing)
    while the switch is off.
    """
    if not settings.web_panel_enabled and not settings.bank_sms_enabled:
        return None
    app = web.Application()
    mount_web_panel(app, settings, session_factory)
    mount_bank_sms(app, settings, session_factory)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", settings.webhook_port).start()
    except BaseException:
        await runner.cleanup()
        raise
    logger.info("Web panel server started on port %s", settings.webhook_port)
    return runner

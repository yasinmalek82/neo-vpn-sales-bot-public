"""Names shared by the app builder, the auth middleware and the routes."""

from __future__ import annotations

from aiohttp import web
from jinja2 import Environment
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings

# Every panel URL lives under this prefix; the middleware guards all of it.
PANEL_PREFIX = "/panel"

# A numeric path part bounded to 18 digits (always fits in a signed 64-bit integer, < 2^63).
NUMERIC_ID_PATTERN = r"\d{1,18}"


SETTINGS = web.AppKey("settings", Settings)
SESSION_FACTORY = web.AppKey("session_factory", async_sessionmaker[AsyncSession])
TEMPLATES = web.AppKey("templates", Environment)

"""Shared pieces for the Mini App panel tests.

``init_data`` signs the way Telegram documents it
(https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app):
secret = HMAC-SHA256(key="WebAppData", msg=bot token), hash = HMAC-SHA256(key=secret,
msg="k=v" lines sorted by key). It is written out here, not imported from aiogram,
so the verifier under test is checked against the specification and not against
itself.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from database.models import User
from web.app import mount_web_panel

TOKEN = "123456:TEST-PANEL-TOKEN"
ENV_ADMIN = 111
DB_ADMIN = 333
STRANGER = 222
INIT_HEADER = "X-Telegram-Init-Data"


def panel_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "BOT_TOKEN": TOKEN,
        "ADMIN_TELEGRAM_IDS": str(ENV_ADMIN),
        "WEB_PANEL_ENABLED": True,
        "WEB_PANEL_URL": "https://panel.example.com/panel/",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def init_data(
    user_id: int,
    *,
    token: str = TOKEN,
    age_seconds: int = 0,
    tamper_user_id: int | None = None,
) -> str:
    """Signed ``initData`` for ``user_id``, ``age_seconds`` old (negative: from the future)."""
    fields = {
        "auth_date": str(int(time.time()) - age_seconds),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if tamper_user_id is not None:  # change the payload after it was signed
        fields["user"] = json.dumps({"id": tamper_user_id, "first_name": "Test"})
    return urlencode(fields)


async def add_user(
    factory: async_sessionmaker[AsyncSession],
    telegram_id: int,
    *,
    is_admin: bool = False,
    is_banned: bool = False,
) -> None:
    async with factory() as session:
        session.add(
            User(
                telegram_id=telegram_id,
                username=f"u{telegram_id}",
                first_name="U",
                language_code="fa",
                referral_code=f"R{telegram_id}",
                is_admin=is_admin,
                is_banned=is_banned,
            )
        )
        await session.commit()


@asynccontextmanager
async def panel_client(
    factory: async_sessionmaker[AsyncSession], settings: Settings | None = None
) -> AsyncIterator[TestClient]:
    app = web.Application()
    assert mount_web_panel(app, settings or panel_settings(), factory)
    async with TestClient(TestServer(app)) as client:
        yield client

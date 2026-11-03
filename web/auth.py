"""Who is asking, from Telegram's signed ``initData``.

Telegram does not send ``initData`` to the server on the first GET, so the
shell page carries no data and the browser sends ``initData`` in a header on
every request after it. Both middlewares act only on paths under ``/panel``
and pass everything else (the webhook) straight through. ``panel_auth`` guards
all of ``/panel`` except the shell and the static files (``is_public``); a
route added later is guarded unless someone lists it there on purpose.

The order is: signature (aiogram's helper, the bot token), freshness, then the
person (``User`` row, banned, admin, reseller). A refusal says only that it
refused, never which check failed. ``initData`` and the token are never logged.

Who may open what is decided here and only here:

* every path under ``/panel`` is **admin only** unless it is listed public;
* ``/panel/me`` and what is under it is the one **personal** prefix. A signed,
  non-banned person with a ``User`` row may open it while
  ``WEB_PANEL_PUBLIC_ENABLED`` is on (an admin always may); what the pages show
  is keyed on the row id found here, and no id from the URL, query, form or
  another header is read;
* ``/panel/me/reseller`` additionally needs the reseller flag.

A route added anywhere else stays admin-only without anyone remembering.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aiogram.utils.web_app import WebAppUser, safe_parse_webapp_init_data
from aiohttp import web

from database.repositories.user import get_user_by_telegram_id
from services.access import is_admin
from services.reseller import is_reseller
from web.keys import PANEL_PREFIX, SESSION_FACTORY, SETTINGS

logger = logging.getLogger(__name__)

INIT_DATA_HEADER = "X-Telegram-Init-Data"
# Telegram's own advice is to treat old initData as stale; an hour is long
# enough for a session left open and short enough that a leaked header dies.
MAX_AUTH_AGE = timedelta(hours=1)
# A phone clock a little ahead of ours must not lock the owner out.
MAX_CLOCK_SKEW = timedelta(minutes=2)
MAX_INIT_DATA_CHARS = 4096

PUBLIC_PATHS = frozenset({f"{PANEL_PREFIX}/"})
PUBLIC_PREFIXES = (f"{PANEL_PREFIX}/static/",)

# The only prefix a non-admin may ever reach, and the reseller part of it.
PERSONAL_PREFIX = f"{PANEL_PREFIX}/me"
RESELLER_PREFIX = f"{PERSONAL_PREFIX}/reseller"
# The navigation and first screen of whoever is asking. An admin needs no row
# to see it; every other personal path reads a row.
HOME_PATH = f"{PERSONAL_PREFIX}/home"

ROLE_ADMIN = "admin"
ROLE_RESELLER = "reseller"
ROLE_CUSTOMER = "customer"

TEXT_UNAUTHORIZED = "ورود تأیید نشد. پنل را دوباره از ربات باز کن."
TEXT_FORBIDDEN = "این بخش برای تو باز نیست."

# Scripts: ours and Telegram's. htmx runs with eval off (see the shell's meta).
# Styles keep 'unsafe-inline' because Telegram's script styles elements itself.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' https://telegram.org; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'self'"
)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class AuthRefused(Exception):
    """A check failed. ``reason`` is for our log, never for the response body."""

    def __init__(self, reason: str, status: int = 401) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


def in_panel(path: str) -> bool:
    return path == PANEL_PREFIX or path.startswith(f"{PANEL_PREFIX}/")


def is_public(path: str) -> bool:
    if ".." in path.split("/"):
        return False
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def _under(path: str, prefix: str) -> bool:
    if ".." in path.split("/"):
        return False
    return path == prefix or path.startswith(f"{prefix}/")


def is_personal(path: str) -> bool:
    return _under(path, PERSONAL_PREFIX)


def is_reseller_path(path: str) -> bool:
    return _under(path, RESELLER_PREFIX)


def verify_init_data(token: str, raw: str | None, *, now: datetime | None = None) -> WebAppUser:
    """The signed user if the signature and the age hold, else ``AuthRefused``."""
    if not raw or len(raw) > MAX_INIT_DATA_CHARS:
        raise AuthRefused("missing")
    try:
        data = safe_parse_webapp_init_data(token, raw)
    except Exception as exc:  # bad signature, bad JSON, missing fields: all one answer
        raise AuthRefused("signature") from exc
    moment = now or datetime.now(UTC)
    auth_date = data.auth_date
    if auth_date.tzinfo is None:
        auth_date = auth_date.replace(tzinfo=UTC)
    if moment - auth_date > MAX_AUTH_AGE:
        raise AuthRefused("stale")
    if auth_date - moment > MAX_CLOCK_SKEW:
        raise AuthRefused("future")
    if data.user is None:
        raise AuthRefused("no_user")
    return data.user


def _refusal(status: int) -> web.Response:
    text = TEXT_UNAUTHORIZED if status == 401 else TEXT_FORBIDDEN
    return web.Response(text=text, status=status, content_type="text/plain", charset="utf-8")


@dataclass(frozen=True)
class Identity:
    """Who is asking, from the signature and the database alone."""

    telegram_id: int
    user_id: int | None  # the ``User`` row id; None when the person has no row
    role: str
    is_reseller: bool


async def identify(request: web.Request) -> Identity:
    """The signed person, or ``AuthRefused`` for a bad signature or a banned account."""
    settings = request.app[SETTINGS]
    user = verify_init_data(settings.bot_token, request.headers.get(INIT_DATA_HEADER))
    async with request.app[SESSION_FACTORY]() as session:
        db_user = await get_user_by_telegram_id(session, user.id)
        reseller = db_user is not None and await is_reseller(session, db_user.id)
    if db_user is not None and db_user.is_banned:
        raise AuthRefused("banned", 403)
    if is_admin(user.id, settings, db_user):
        role = ROLE_ADMIN
    elif reseller:
        role = ROLE_RESELLER
    else:
        role = ROLE_CUSTOMER
    return Identity(
        telegram_id=user.id,
        user_id=db_user.id if db_user is not None else None,
        role=role,
        is_reseller=reseller,
    )


def authorize(identity: Identity, path: str, *, public_enabled: bool) -> None:
    """Raise ``AuthRefused`` (403) unless this person may open ``path``."""
    if is_reseller_path(path) and not identity.is_reseller:
        raise AuthRefused("not_reseller", 403)
    if identity.role == ROLE_ADMIN:
        if is_personal(path) and path != HOME_PATH and identity.user_id is None:
            raise AuthRefused("no_row", 403)
        return
    if not is_personal(path):
        raise AuthRefused("not_admin", 403)
    if not public_enabled:
        raise AuthRefused("public_off", 403)
    if identity.user_id is None:
        raise AuthRefused("no_row", 403)


@web.middleware
async def panel_auth(request: web.Request, handler: Handler) -> web.StreamResponse:
    if not in_panel(request.path) or is_public(request.path):
        return await handler(request)
    try:
        identity = await identify(request)
        authorize(
            identity,
            request.path,
            public_enabled=request.app[SETTINGS].web_panel_public_enabled,
        )
    except AuthRefused as refused:
        logger.info("panel auth refused: %s (%s)", refused.reason, request.path)
        return _refusal(refused.status)
    request["telegram_id"] = identity.telegram_id
    request["user_id"] = identity.user_id
    request["role"] = identity.role
    return await handler(request)


@web.middleware
async def response_headers(request: web.Request, handler: Handler) -> web.StreamResponse:
    """No data response is cached by the phone or a proxy; a few hardening headers."""
    if not in_panel(request.path):
        return await handler(request)  # the webhook route, untouched
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        response = exc
    if request.path.startswith(f"{PANEL_PREFIX}/static/"):
        # Revalidate (ETag) so a deploy reaches the phone; the files carry no data.
        response.headers["Cache-Control"] = "no-cache"
    else:
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    if isinstance(response, web.HTTPException):
        raise response
    return response

"""The personal pages: ``/panel/me/...``, read-only.

These are the only routes a non-admin can reach (``web.auth``). Each one acts
for the person the signature identified: ``request["user_id"]`` and
``request["role"]`` are set by the middleware from the signed ``initData`` and
the database. No route here reads a user id, a role or a Telegram id from the
path, the query, a form or a header; the one query parameter is the reseller
list's ``offset``.
"""

from __future__ import annotations

from aiohttp import web

from services.personal_page import customer_page, reseller_dashboard
from web.keys import PANEL_PREFIX, SESSION_FACTORY, SETTINGS
from web.nav import nav_for
from web.templating import render

MAX_OFFSET = 100_000


async def home(request: web.Request) -> web.Response:
    """This person's navigation and a loader for their first screen. No data."""
    return render(request, "home.html", nav=nav_for(request["role"]))


async def customer(request: web.Request) -> web.Response:
    async with request.app[SESSION_FACTORY]() as session:
        page = await customer_page(session, request.app[SETTINGS], request["user_id"])
    return render(request, "me_customer.html", page=page)


def _offset(request: web.Request) -> int:
    raw = request.query.get("offset", "0")
    return min(int(raw), MAX_OFFSET) if raw.isascii() and raw.isdigit() else 0


async def reseller(request: web.Request) -> web.Response:
    async with request.app[SESSION_FACTORY]() as session:
        dash = await reseller_dashboard(
            session, request.app[SETTINGS], request["user_id"], offset=_offset(request)
        )
    return render(request, "me_reseller.html", dash=dash)


routes = [
    web.get(f"{PANEL_PREFIX}/me/home", home),
    web.get(f"{PANEL_PREFIX}/me", customer),
    web.get(f"{PANEL_PREFIX}/me/reseller", reseller),
]

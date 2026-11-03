"""The shell page: static HTML, no data, no database.

It is public (``web.auth.is_public``) because Telegram cannot send ``initData``
on the first GET. It loads the scripts, which then ask for each screen with the
signed ``initData`` in a header.
"""

from __future__ import annotations

from aiohttp import web

from web.keys import PANEL_PREFIX
from web.templating import render


async def shell(request: web.Request) -> web.Response:
    return render(request, "shell.html")


routes = [web.get(f"{PANEL_PREFIX}/", shell)]

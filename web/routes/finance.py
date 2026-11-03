"""«گزارش مالی»: the admin's finance report, read-only.

The same two service calls the bot's screen makes (``get_finance_report`` and
``dashboard_counts``), so the figures cannot differ between the two.
"""

from __future__ import annotations

from aiohttp import web

from services.admin import dashboard_counts
from services.reporting import get_finance_report
from web.keys import PANEL_PREFIX, SESSION_FACTORY
from web.templating import render


async def finance(request: web.Request) -> web.Response:
    async with request.app[SESSION_FACTORY]() as session:
        counts = await dashboard_counts(session)
        report = await get_finance_report(session)
    return render(request, "finance.html", report=report, counts=counts)


routes = [web.get(f"{PANEL_PREFIX}/finance", finance)]

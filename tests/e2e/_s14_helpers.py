"""Helpers for section ۱۴ (the panel screens and the node watcher).

Candidate for the shared harness: ``add_system_load`` (the fake panel's ``/api/system``
answers without CPU and memory, which the OpenAPI file makes optional).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select

from database.models.panel_node_state import PanelNodeState
from tests.e2e._s07b_helpers import tap_starting
from tests.e2e.harness import App
from tests.e2e.harness.actors import Actor
from tests.e2e.harness.settings import PANEL_KEY


def add_system_load(app: App, *, cpu: float, cores: int, mem_used: int, mem_total: int) -> None:
    """Make ``GET /api/system`` report CPU and memory too."""
    route = ("GET", "/api/system")
    original = app.panel._routes[route]  # noqa: SLF001

    def with_load(*args: Any) -> Any:
        payload = original(*args)
        payload.update(cpu_usage=cpu, cpu_cores=cores, mem_used=mem_used, mem_total=mem_total)
        return payload

    app.panel._routes[route] = with_load  # noqa: SLF001


async def open_panel_page(admin: Actor) -> None:
    """/admin → ⚙️ تنظیمات → 🔌 اتصال پنل."""
    await admin.send("/admin")
    await tap_starting(admin, "تنظیمات")
    await tap_starting(admin, "اتصال پنل")


async def set_api_key(admin: Actor, key: str) -> None:
    await tap_starting(admin, "🔑")
    await admin.send(key)


def right_key() -> str:
    return PANEL_KEY


async def system_texts(group: Any) -> list[str]:
    """What reached the «سیستم» topic, without the set-up test message."""
    return [t for t in await group.texts("system") if "تست مسیر" not in t]


async def watch(app: App, times: int = 1) -> None:
    """Run the node watcher ``times`` times, then deliver what it queued."""
    for _ in range(times):
        await app.run_job("panel_watch")
    await app.run_job("report_flush")


async def age_outage(app: App, minutes: int) -> None:
    """Make every unhealthy stretch the watcher remembers ``minutes`` older."""
    async with app.session() as session:
        for row in (await session.execute(select(PanelNodeState))).scalars():
            if row.reported_since is not None:
                row.reported_since -= timedelta(minutes=minutes)
        await session.commit()

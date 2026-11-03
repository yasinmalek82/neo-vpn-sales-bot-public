"""What the admin home's status block reports.

Database only. A live panel check on every /admin would add a second or more
to the screen the owner opens most, and «🔌 تست اتصال» already exists for that
(owner, 2026-09-30).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Order, OrderStatus
from database.models.panel_node_state import PANEL_NODE_ID
from services.panel_watch import list_down_rows
from services.report_group import get_readiness
from services.reporting import FinanceWindow, today_sales
from services.store_admin import get_catalog_view
from services.ux_format import STALE_PENDING_SECONDS


@dataclass(frozen=True)
class PanelDown:
    """A node (or the panel itself, ``is_panel``) the watcher has seen down twice running."""

    name: str
    since: datetime
    is_panel: bool = False


@dataclass(frozen=True)
class HomeStatus:
    pending: int
    stale: int
    today: FinanceWindow
    groups_without_panel: list[str]
    report_attached: bool
    report_unready: int
    panel_down: list[PanelDown]


async def get_home_status(session: AsyncSession, *, now: datetime | None = None) -> HomeStatus:
    moment = now or datetime.now(UTC)
    review = Order.status == OrderStatus.PAYMENT_REVIEW
    pending = int(await session.scalar(select(func.count()).select_from(Order).where(review)) or 0)
    cutoff = moment - timedelta(seconds=STALE_PENDING_SECONDS)
    stale = int(
        await session.scalar(
            select(func.count()).select_from(Order).where(review, Order.created_at <= cutoff)
        )
        or 0
    )
    # The same window «💰 گزارش مالی» calls «امروز», so the two never disagree.
    today = await today_sales(session, now=moment)
    view = await get_catalog_view(session)
    readiness = await get_readiness(session)
    down = [
        PanelDown(
            name=str(row.name),
            since=(
                row.status_since
                if row.status_since.tzinfo
                else row.status_since.replace(tzinfo=UTC)
            ),
            is_panel=int(row.node_id) == PANEL_NODE_ID,
        )
        for row in await list_down_rows(session)
    ]
    return HomeStatus(
        pending=pending,
        stale=stale,
        today=today,
        groups_without_panel=[str(c.title_fa) for c in view.on_sale_without_panel_group()],
        report_attached=readiness.attached,
        report_unready=len(readiness.topics) - readiness.ready_count,
        panel_down=down,
    )

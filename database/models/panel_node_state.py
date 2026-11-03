"""What the panel watcher last saw of each node, and of the panel API itself.

One row per PasarGuard node plus row 0 for the panel. Nothing else stores node
state, so without this table neither the alert debounce nor the admin home's
warning (which reads the database only) could know a server was down.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base

PANEL_NODE_ID = 0  # the panel API itself


class PanelNodeState(Base):
    __tablename__ = "panel_node_states"

    # PasarGuard's node id. 0 = the panel API itself, which no node can be.
    node_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # connected|connecting|error|disabled|limited|unreachable
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    status_since: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    last_checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    checks_in_status: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # The last status an alert went out for; cleared when «recovered» goes out.
    reported_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # When that unhealthy stretch began. A node usually recovers error ->
    # connecting -> connected, so status_since alone would measure only the
    # «connecting» part and understate the outage.
    reported_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

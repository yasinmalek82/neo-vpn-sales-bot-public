"""Watch PasarGuard's nodes and the panel itself, and report outages once.

The transition logic lives here and only touches the database: one
``panel_node_states`` row per node plus row 0 for the panel API. Alerts are
**enqueued** in the same transaction as the state change and delivered later by
the report outbox worker, never sent from here. The caller commits.

Statuses
    up        ``connected``
    down      ``error`` and ``unreachable`` (the panel API itself, row 0)
    warning   ``limited``
    silent    ``connecting`` (transient) and ``disabled`` (the owner did it)

Debounce: a down or warning status alerts only on its second consecutive check.
One failed read, or a node mid-reconnect, is not an outage. «Recovered» goes out
once a node we alerted on has been ``connected`` for two consecutive checks, so a
down, connected, down flap stays one outage and sends nothing new.

``event_key`` names the transition: ``panel_node:{node_id}:{status}:{since}``.
Re-running a check on unchanged state therefore queues nothing new, and the
same node going down twice in a day is two alerts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api_client.client import PasarGuardClient
from api_client.exceptions import PasarGuardAuthError
from api_client.models import PanelNode, PanelNodeSimple
from bot.config import Settings
from database.base import end_read_transaction
from database.models.panel_node_state import PANEL_NODE_ID, PanelNodeState
from services import report_events
from services.report_events import Report
from services.report_outbox import enqueue_report

logger = logging.getLogger(__name__)

STATUS_UNREACHABLE = "unreachable"
DOWN_STATUSES = frozenset({"error", STATUS_UNREACHABLE})
WARNING_STATUSES = frozenset({"limited"})
ALERT_STATUSES = DOWN_STATUSES | WARNING_STATUSES
# Consecutive checks in one status before an alert goes out.
ALERT_AFTER_CHECKS = 2

# A row older than this many watcher intervals is no proof the panel is still down.
STALE_AFTER_INTERVALS = 3

PANEL_NAME = "panel"

# One WARNING per process for a key that may not read nodes; the pass runs every 2 minutes.
_nodes_forbidden_logged = False


def _is_permission_refusal(exc: BaseException) -> bool:
    """HTTP 403 only: a missing permission. 401 is a bad key, which stays an outage."""
    return isinstance(exc, PasarGuardAuthError) and exc.status_code == 403


@dataclass(frozen=True)
class WatchResult:
    """What one check did. The job logs it; tests read it."""

    reports: list[Report]
    panel_reachable: bool
    # False when the key may not read the node list (403) but the panel answers.
    nodes_readable: bool = True


def _aware(moment: datetime) -> datetime:
    """SQLite hands back naive datetimes; every stored moment is UTC."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def is_down(row: PanelNodeState) -> bool:
    """Whether the admin home should warn about this row."""
    return row.status in DOWN_STATUSES and int(row.checks_in_status) >= ALERT_AFTER_CHECKS


def panel_watch_enabled(settings: Settings) -> bool:
    """Whether the periodic panel watch is enabled."""
    return int(settings.panel_watch_interval_seconds) > 0


async def is_panel_down(
    session: AsyncSession,
    settings: Settings,
    now: datetime | None = None,
) -> bool:
    """Whether the panel is confirmed down, so new sales must be refused.

    True only when the watcher is enabled, the panel's own row says down for at
    least ``ALERT_AFTER_CHECKS`` checks (the alert's own rule), and that row was
    written less than three watcher intervals ago. Anything else, a stale row
    included, is False: the shop stays open when the watcher cannot vouch.

    The row is read first and settings only after it says down, so a caller
    with no confirmed outage never needs them.
    """
    row = await session.get(PanelNodeState, PANEL_NODE_ID)
    if row is None or not is_down(row):
        return False
    if not panel_watch_enabled(settings):
        return False
    if row.last_checked_at is None:
        return False
    age = _aware(now or datetime.now(UTC)) - _aware(row.last_checked_at)
    return age < timedelta(
        seconds=STALE_AFTER_INTERVALS * int(settings.panel_watch_interval_seconds)
    )


async def _messages_for_alerts(client: PasarGuardClient) -> dict[int, str | None]:
    """The panel's own explanation for nodes about to alert. Best-effort.

    ``/api/nodes/simple`` has no message; the detailed list does. It is read only
    when a node is actually about to alert, so a healthy fleet costs one call.
    """
    try:
        detailed: list[PanelNode] = await client.list_nodes()
    except Exception as exc:
        logger.warning("Panel watch: node messages unavailable (%s)", type(exc).__name__)
        return {}
    return {int(n.id): n.message for n in detailed}


def _observe(
    row: PanelNodeState | None,
    *,
    node_id: int,
    name: str,
    status: str,
    now: datetime,
) -> tuple[PanelNodeState, str | None]:
    """Record one check. Returns (row, previous status) — previous is None when new."""
    if row is None:
        row = PanelNodeState(
            node_id=node_id,
            name=name,
            status=status,
            status_since=now,
            last_checked_at=now,
            checks_in_status=1,
            reported_status=None,
            reported_since=None,
        )
        return row, None
    previous = row.status
    same_stretch = previous == status or (
        # PasarGuard moves a dead node error -> connecting -> error between polls.
        # Still not serving, so it is the same outage: count the check, keep the
        # status and its start. Only «connected» ends it.
        status == "connecting"
        and (previous in DOWN_STATUSES or previous in WARNING_STATUSES)
    )
    if (
        previous == status
        and status not in DOWN_STATUSES
        and status not in WARNING_STATUSES
        and row.name == name
        and int(row.checks_in_status) >= ALERT_AFTER_CHECKS
    ):
        # No news from a node that is not in trouble. Its count is only ever
        # compared with ALERT_AFTER_CHECKS, so one more changes nothing; writing
        # it (and the time) made every pass over a healthy panel a commit, and
        # each commit fails any handler that read before it and writes after
        # («database is locked»). ``last_checked_at`` is therefore the last
        # check that recorded something, not the last pass. An outage keeps
        # counting every check, as before.
        return row, previous
    row.name = name
    row.last_checked_at = now
    if same_stretch:
        row.checks_in_status = int(row.checks_in_status) + 1
    else:
        row.status = status
        row.status_since = now
        row.checks_in_status = 1
    return row, previous


def _alert_unhealthy(
    row: PanelNodeState, *, node_id: int, message: str | None, since: datetime
) -> Report | None:
    status = row.status
    if int(row.checks_in_status) < ALERT_AFTER_CHECKS or row.reported_status == status:
        return None
    # An unhealthy stretch keeps its first start: error -> limited is one outage.
    if row.reported_status is None:
        row.reported_since = since
    row.reported_status = status
    if node_id == PANEL_NODE_ID:
        return report_events.panel_unreachable(since=since)
    if status == "limited":
        return report_events.panel_node_limited(
            node_id=node_id, name=row.name, since=since, message=message
        )
    return report_events.panel_node_down(
        node_id=node_id, name=row.name, status=status, since=since, message=message
    )


def _alert_recovered(row: PanelNodeState, *, node_id: int, since: datetime) -> Report:
    began = _aware(row.reported_since) if row.reported_since else since
    # The outage ended at the first connected check; the second only confirmed it.
    down_seconds = max(0, int((since - began).total_seconds()))
    row.reported_status = None
    row.reported_since = None
    if node_id == PANEL_NODE_ID:
        return report_events.panel_recovered(since=since, down_seconds=down_seconds)
    return report_events.panel_node_recovered(
        node_id=node_id, name=row.name, since=since, down_seconds=down_seconds
    )


def _alert_for(
    row: PanelNodeState,
    *,
    node_id: int,
    message: str | None,
) -> Report | None:
    """The report this check owes for ``row``, updating its ``reported_*`` columns."""
    since = _aware(row.status_since)
    if row.status in ALERT_STATUSES:
        return _alert_unhealthy(row, node_id=node_id, message=message, since=since)
    if (
        row.status == "connected"
        and row.reported_status in ALERT_STATUSES
        and int(row.checks_in_status) >= ALERT_AFTER_CHECKS
    ):
        return _alert_recovered(row, node_id=node_id, since=since)
    if row.status == "disabled":
        # The owner switched it off on purpose: nothing will «recover», and a
        # recovery message after a week of being disabled would be a false outage.
        row.reported_status = None
        row.reported_since = None
    return None


async def check_panel_and_nodes(
    session: AsyncSession, client: PasarGuardClient, now: datetime | None = None
) -> WatchResult:
    """One watcher pass. Flushes state and enqueues reports; the caller commits."""
    moment = _aware(now or datetime.now(UTC))
    rows = {
        int(r.node_id): r for r in (await session.execute(select(PanelNodeState))).scalars().all()
    }
    reports: list[Report] = []

    async def emit(report: Report | None) -> None:
        if report is None:
            return
        reports.append(report)
        await enqueue_report(
            session,
            event_key=report.event_key,
            topic_key=report.topic_key,
            body=report.body,
            link_user_id=report.link_user_id,
        )

    # No read snapshot across the panel call. Any other job's commit during it
    # made every write below fail («database is locked»), and the pass, with
    # its «one more check», was lost. Only this job writes these rows, and one
    # pass runs at a time, so the rows read above are still the latest.
    await end_read_transaction(session)
    nodes: list[PanelNodeSimple] | None = None
    try:
        nodes = await client.list_nodes_simple()
    except Exception as exc:
        reachable = False
        if _is_permission_refusal(exc):
            # A scoped admin key: the node list is refused but selling works. Probe
            # with a call the bot needs anyway before calling it an outage.
            try:
                await client.get_current_admin()
                reachable = True
            except Exception as probe_exc:
                logger.warning("Panel watch: admin probe failed (%s)", type(probe_exc).__name__)
        if reachable:
            global _nodes_forbidden_logged
            if not _nodes_forbidden_logged:
                _nodes_forbidden_logged = True
                logger.warning(
                    "Panel watch: API key may not read nodes (403); panel answers, "
                    "node watching is off until the admin gets node read access"
                )
        else:
            # Nothing is known about the nodes now, so their rows stay as they were.
            # A refused key counts too: the bot cannot sell while it cannot read.
            logger.warning("Panel watch: panel not readable (%s)", type(exc).__name__)
            row, _ = _observe(
                rows.get(PANEL_NODE_ID),
                node_id=PANEL_NODE_ID,
                name=PANEL_NAME,
                status=STATUS_UNREACHABLE,
                now=moment,
            )
            session.add(row)
            await emit(_alert_for(row, node_id=PANEL_NODE_ID, message=None))
            await session.flush()
            return WatchResult(reports=reports, panel_reachable=False)

    row, _ = _observe(
        rows.get(PANEL_NODE_ID),
        node_id=PANEL_NODE_ID,
        name=PANEL_NAME,
        status="connected",
        now=moment,
    )
    session.add(row)
    await emit(_alert_for(row, node_id=PANEL_NODE_ID, message=None))

    if nodes is None:
        # Panel answers but the node list is not ours to read: rows stay as they were.
        await session.flush()
        return WatchResult(reports=reports, panel_reachable=True, nodes_readable=False)

    seen: set[int] = set()
    touched: list[PanelNodeState] = []
    for node in nodes:
        node_id = int(node.id)
        if node_id == PANEL_NODE_ID:
            continue  # row 0 is the panel; a node can never claim it
        seen.add(node_id)
        node_row, _ = _observe(
            rows.get(node_id),
            node_id=node_id,
            name=node.name,
            status=str(node.status).lower(),
            now=moment,
        )
        session.add(node_row)
        touched.append(node_row)

    messages: dict[int, str | None] = {}
    if any(
        r.status in ALERT_STATUSES
        and int(r.checks_in_status) >= ALERT_AFTER_CHECKS
        and r.reported_status != r.status
        for r in touched
    ):
        messages = await _messages_for_alerts(client)
    for node_row in touched:
        await emit(
            _alert_for(
                node_row,
                node_id=int(node_row.node_id),
                message=messages.get(int(node_row.node_id)),
            )
        )

    # A node that left the panel's list is gone, not down: no row, no alert.
    for node_id, gone in rows.items():
        if node_id != PANEL_NODE_ID and node_id not in seen:
            await session.delete(gone)
    await session.flush()
    return WatchResult(reports=reports, panel_reachable=True)


async def list_down_rows(session: AsyncSession) -> list[PanelNodeState]:
    """Rows the admin home warns about: down for at least two checks. Panel first."""
    result = await session.execute(
        select(PanelNodeState)
        .where(PanelNodeState.status.in_(tuple(DOWN_STATUSES)))
        .where(PanelNodeState.checks_in_status >= ALERT_AFTER_CHECKS)
        .order_by(PanelNodeState.node_id)
    )
    return list(result.scalars().all())

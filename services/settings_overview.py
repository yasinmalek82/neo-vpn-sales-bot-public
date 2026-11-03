"""«⚙️ تنظیمات» for the Mini App: what each section says, read-only ones included.

Three of the bot's sections are shown here and never written from the web:

* **📡 گروه گزارش**: attaching a group and creating topics are Telegram calls
  and a chat id typed in the right chat.
* **🔌 اتصال پنل**: the address and the key decide where every order goes, and a
  test of them is a live panel call. The page holds **booleans and the watcher's
  last stored row, never a value**: the address may carry credentials, so it is
  not read into the page at all.
* **🎨 پس‌زمینهٔ QR**: an upload is a file-handling decision the owner has not made.

Everything here is a database read; reading never writes (a missing settings
row reads as «not set up», where the bot's screen creates the row).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.texts import texts
from database.models.panel_node_state import PANEL_NODE_ID, PanelNodeState
from services.lifecycle_settings import LifecycleConfig, load_config
from services.panel_settings import get_effective_pasarguard_config
from services.panel_watch import is_down, list_down_rows, panel_watch_enabled
from services.qr_brand import has_qr_background
from services.report_group import GroupReadiness, get_readiness
from services.ux_format import format_relative_age, to_persian_digits


@dataclass(frozen=True)
class TopicLine:
    mark: str
    title: str


@dataclass(frozen=True)
class ReportsView:
    attached: bool
    title: str
    state: str
    ready: str
    topics: tuple[TopicLine, ...]
    line: str  # the one-line state on the settings home


@dataclass(frozen=True)
class PanelView:
    configured: bool
    configured_line: str
    watch_line: str
    down_lines: tuple[str, ...]
    line: str


@dataclass(frozen=True)
class QrView:
    custom: bool
    state: str
    line: str


CONFIGURED = "تنظیم شده"
NOT_CONFIGURED = "تنظیم نشده"
WATCH_OFF = "پایش خودکار پنل خاموش است."
WATCH_NEVER = "هنوز بررسی نشده."
WATCH_UP = "پنل در دسترس است (آخرین بررسی: {since})."
WATCH_UNSURE = "وضعیت پنل: {status} (آخرین بررسی: {since})."
QR_CUSTOM = "عکس خود مالک"


def reports_view(readiness: GroupReadiness) -> ReportsView:
    if not readiness.attached:
        return ReportsView(
            attached=False,
            title="",
            state="",
            ready="",
            topics=(),
            line=texts.admin_reports_not_attached,
        )
    state = texts.admin_reports_state_on if readiness.enabled else texts.admin_reports_state_off
    ready = (
        f"{to_persian_digits(readiness.ready_count)} از {to_persian_digits(len(readiness.topics))}"
    )
    title = readiness.chat_title or str(readiness.chat_id)
    return ReportsView(
        attached=True,
        title=title,
        state=state,
        ready=ready,
        topics=tuple(
            TopicLine(mark="✅" if t.ready else ("🟡" if t.configured else "⚪️"), title=t.title_fa)
            for t in readiness.topics
        ),
        line=f"{title}: {state} • تاپیک آماده {ready}",
    )


async def load_reports(session: AsyncSession) -> ReportsView:
    return reports_view(await get_readiness(session, create=False))


def _watch_line(row: PanelNodeState | None, settings: Settings, now: datetime) -> str:
    if not panel_watch_enabled(settings):
        return WATCH_OFF
    if row is None:
        return WATCH_NEVER
    since = format_relative_age(row.last_checked_at, now=now, persian_digits=True)
    if is_down(row):
        return texts.admin_home_warn_panel.format(
            since=format_relative_age(row.status_since, now=now, persian_digits=True)
        )
    if row.status == "connected":
        return WATCH_UP.format(since=since)
    return WATCH_UNSURE.format(status=row.status, since=since)


async def load_panel(session: AsyncSession, settings: Settings, now: datetime) -> PanelView:
    """Configured or not, and the watcher's stored state. No panel call, no value."""
    cfg = await get_effective_pasarguard_config(session, settings)
    configured = bool(cfg.base_url and cfg.base_url.strip()) and cfg.uses_api_key
    row = await session.get(PanelNodeState, PANEL_NODE_ID)
    down = [
        texts.admin_home_warn_node.format(
            name=r.name, since=format_relative_age(r.status_since, now=now, persian_digits=True)
        )
        for r in await list_down_rows(session)
        if r.node_id != PANEL_NODE_ID
    ]
    watch = _watch_line(row, settings, now)
    state = CONFIGURED if configured else NOT_CONFIGURED
    return PanelView(
        configured=configured,
        configured_line=state,
        watch_line=watch,
        down_lines=tuple(down),
        line=f"{state} • {watch}",
    )


async def load_qr(session: AsyncSession) -> QrView:
    custom = await has_qr_background(session)
    state = texts.admin_qr_bg_current_custom if custom else texts.admin_qr_bg_current_template
    return QrView(custom=custom, state=state, line=state)


@dataclass(frozen=True)
class OverviewCard:
    key: str
    title: str
    line: str
    url: str


def lifecycle_line(config: LifecycleConfig) -> str:
    return f"حذف خودکار: {texts.admin_life_mode_names[config.delete_mode]}"


async def overview(
    session: AsyncSession,
    settings: Settings,
    *,
    base_url: str,
    now: datetime | None = None,
) -> list[OverviewCard]:
    """The four sections with their one-line state, in the bot's order."""
    moment = now or datetime.now(UTC)
    config = await load_config(session, seed_days=settings.renewal_reminder_days)
    panel = await load_panel(session, settings, moment)
    reports = await load_reports(session)
    qr = await load_qr(session)
    return [
        OverviewCard("panel", "🔌 اتصال پنل", panel.line, f"{base_url}/panel"),
        OverviewCard("reports", "📡 گروه گزارش", reports.line, f"{base_url}/reports"),
        OverviewCard("qr", "🎨 پس‌زمینهٔ QR", qr.line, f"{base_url}/qr"),
        OverviewCard(
            "lifecycle", texts.admin_life_title, lifecycle_line(config), f"{base_url}/lifecycle"
        ),
    ]

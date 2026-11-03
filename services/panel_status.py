"""Text blocks for «📊 وضعیت پنل» — pure formatting plus one best-effort loader.

No Telegram and no keyboard here: the screen only renders what
:func:`load_panel_status` returns. Every panel read is best-effort, so a read
the API key may not make (PasarGuard answers 403 per section) drops its own
block with one Persian line instead of breaking the screen.

Nothing in this module prints a raw exception: the panel's error text can carry
hostnames and ids, and ``str(exc)`` in a Persian screen is the leak CLAUDE.md
forbids.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from html import escape

from api_client.client import PasarGuardClient
from api_client.exceptions import PasarGuardAuthError
from api_client.models import PanelNode, PanelNodeSimple, SystemStats
from services.ux_format import to_persian_digits

logger = logging.getLogger(__name__)

__all__ = [
    "NODE_EMOJI",
    "NODE_MESSAGE_MAX",
    "PanelStatusView",
    "ReconnectableNode",
    "format_bytes_fa",
    "format_node_line",
    "format_uptime_fa",
    "load_panel_status",
    "nodes_block",
    "panel_block",
    "reconnect_node_result",
    "reconnectable_nodes",
    "traffic_block",
    "users_block",
]

TB = 1024**4
GB = 1024**3
MB = 1024**2

NODE_MESSAGE_MAX = 80
RECONNECT_LABEL_MAX = 40

NODE_EMOJI: dict[str, str] = {
    "connected": "🟢",
    "connecting": "🟡",
    "error": "🔴",
    "disabled": "⏸",
    "limited": "📉",
}
# error and limited are the two states where the panel's own message explains why.
_NODE_SHOWS_MESSAGE = frozenset({"error", "limited"})
_NODE_RECONNECTABLE = frozenset({"error", "connecting"})

PERMISSION_LINE = "کلید API اجازهٔ دیدن این بخش را ندارد"
NODES_PERMISSION_LINE = (
    "کلید API اجازهٔ دیدن سرورها را ندارد — در پنل به این ادمین دسترسی خواندن نودها بده"
)
READ_FAILED_LINE = "خواندن این بخش از پنل نشد"
RECONNECT_SENT = "✅ درخواست اتصال دوباره فرستاده شد"
RECONNECT_FAILED = "⚠️ درخواست اتصال دوباره انجام نشد"

_TITLE_PANEL = "🖥 <b>پنل</b>"
_TITLE_USERS = "👥 <b>کاربران</b>"
_TITLE_TRAFFIC = "📈 <b>ترافیک</b>"
_TITLE_NODES = "🌐 <b>سرورها</b>"

_DECIMAL_SEP = "٫"


def _fa(value: int) -> str:
    return to_persian_digits(int(value))


def _fa_decimal(value: float, places: int) -> str:
    """«۱٫۲» — Persian digits and the Persian decimal separator, trailing zeros dropped."""
    text = f"{value:.{places}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return to_persian_digits(text).replace(".", _DECIMAL_SEP)


def format_bytes_fa(value: int | None) -> str:
    """Bytes as «۱٫۲ ترابایت» / «۳۴٫۵ گیگ» / «۵۰۰ مگابایت»; None is a dash."""
    if value is None:
        return "—"
    size = max(int(value), 0)
    if size >= TB:
        return f"{_fa_decimal(size / TB, 2)} ترابایت"
    if size >= GB:
        return f"{_fa_decimal(size / GB, 1)} گیگ"
    return f"{_fa(size // MB)} مگابایت"


def format_uptime_fa(seconds: int | None) -> str:
    """«۳ روز و ۴ ساعت»; under a day only hours, under an hour only minutes."""
    if seconds is None:
        return "—"
    total = max(int(seconds), 0)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{_fa(days)} روز و {_fa(hours)} ساعت" if hours else f"{_fa(days)} روز"
    if hours:
        return f"{_fa(hours)} ساعت"
    return f"{_fa(minutes)} دقیقه"


def _trim(text: str, limit: int) -> str:
    cleaned = " ".join(str(text).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def format_node_line(node: PanelNode | PanelNodeSimple) -> str:
    """One node: emoji, escaped name, and for error/limited the escaped message (≤80)."""
    status = str(node.status or "").lower()
    emoji = NODE_EMOJI.get(status, "⚪")
    line = f"{emoji} {escape(node.name)}"
    if status not in NODE_EMOJI:
        line += f" ({escape(status)})" if status else ""
    message = getattr(node, "message", None)
    if status in _NODE_SHOWS_MESSAGE and message and message.strip():
        line += f" — {escape(_trim(message, NODE_MESSAGE_MAX))}"
    return line


def panel_block(stats: SystemStats) -> str:
    lines = [_TITLE_PANEL]
    version = (stats.version or "").strip()
    if version:
        lines.append(f"نسخه: {escape(version)}")
    lines.append(f"روشن‌بودن: {format_uptime_fa(stats.uptime_seconds)}")
    if stats.cpu_usage is not None:
        cores = f" ({_fa(stats.cpu_cores)} هسته)" if stats.cpu_cores else ""
        lines.append(f"پردازنده: {_fa_decimal(stats.cpu_usage, 0)}٪{cores}")
    if stats.mem_total:
        lines.append(f"رم: {format_bytes_fa(stats.mem_used)} از {format_bytes_fa(stats.mem_total)}")
    if stats.disk_total:
        lines.append(
            f"دیسک: {format_bytes_fa(stats.disk_used)} از {format_bytes_fa(stats.disk_total)}"
        )
    return "\n".join(lines)


def users_block(stats: SystemStats) -> str:
    rows = (
        ("کل", stats.total_user),
        ("فعال", stats.active_users),
        ("آنلاین الان", stats.online_users),
        ("منقضی", stats.expired_users),
        ("تمام‌شده‌حجم", stats.limited_users),
        ("غیرفعال", stats.disabled_users),
        ("در انتظار شروع", stats.on_hold_users),
    )
    return "\n".join([_TITLE_USERS, *(f"{label}: {_fa(n)}" for label, n in rows)])


def traffic_block(stats: SystemStats) -> str:
    return "\n".join(
        [
            _TITLE_TRAFFIC,
            f"دریافتی: {format_bytes_fa(stats.incoming_bandwidth)}",
            f"ارسالی: {format_bytes_fa(stats.outgoing_bandwidth)}",
        ]
    )


def nodes_block(nodes: Sequence[PanelNode | PanelNodeSimple]) -> str:
    if not nodes:
        return "\n".join([_TITLE_NODES, "هیچ سروری در پنل ثبت نشده"])
    return "\n".join([_TITLE_NODES, *(format_node_line(n) for n in nodes)])


@dataclass(frozen=True)
class ReconnectableNode:
    id: int
    name: str

    @property
    def button_label(self) -> str:
        """«🔄 وصل دوباره: {name}», the name trimmed so the button stays ≤40 characters."""
        prefix = "🔄 وصل دوباره: "
        return prefix + _trim(self.name, RECONNECT_LABEL_MAX - len(prefix))


def reconnectable_nodes(nodes: Sequence[PanelNode | PanelNodeSimple]) -> list[ReconnectableNode]:
    """Nodes that get a «وصل دوباره» button: error and connecting only."""
    return [
        ReconnectableNode(id=int(n.id), name=n.name)
        for n in nodes
        if str(n.status or "").lower() in _NODE_RECONNECTABLE
    ]


@dataclass(frozen=True)
class PanelStatusView:
    """What the status screen renders. Each block is text or its own failure line."""

    panel: str
    users: str
    traffic: str
    nodes: str
    reconnectable: list[ReconnectableNode] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join([self.panel, self.users, self.traffic, self.nodes])


def _failure_line(title: str, exc: BaseException) -> str:
    reason = PERMISSION_LINE if isinstance(exc, PasarGuardAuthError) else READ_FAILED_LINE
    return f"{title}\n{reason}"


async def load_panel_status(client: PasarGuardClient) -> PanelStatusView:
    """Read /api/system and the node list, each best-effort.

    The three system blocks share one read, so they drop together; the nodes
    block drops alone. If the detailed node list fails (it is the heavier read,
    and the one that carries the message) the lightweight list still gives the
    names and states. The caller closes the client.
    """
    try:
        stats: SystemStats | None = await client.get_system_stats()
        stats_error: BaseException | None = None
    except Exception as exc:
        logger.warning("Panel status: system read failed (%s)", type(exc).__name__)
        stats, stats_error = None, exc

    nodes: Sequence[PanelNode | PanelNodeSimple] | None
    try:
        nodes = await client.list_nodes()
        nodes_error: BaseException | None = None
    except Exception as exc:
        logger.warning("Panel status: node list failed (%s)", type(exc).__name__)
        nodes, nodes_error = None, exc
        try:
            nodes = await client.list_nodes_simple()
            nodes_error = None
        except Exception as exc2:
            logger.warning("Panel status: simple node list failed (%s)", type(exc2).__name__)

    if stats is not None:
        panel, users, traffic = panel_block(stats), users_block(stats), traffic_block(stats)
    else:
        assert stats_error is not None
        panel = _failure_line(_TITLE_PANEL, stats_error)
        users = _failure_line(_TITLE_USERS, stats_error)
        traffic = _failure_line(_TITLE_TRAFFIC, stats_error)

    if nodes is not None:
        nodes_text = nodes_block(nodes)
        reconnect = reconnectable_nodes(nodes)
    else:
        assert nodes_error is not None
        if isinstance(nodes_error, PasarGuardAuthError):
            nodes_text = f"{_TITLE_NODES}\n{NODES_PERMISSION_LINE}"
        else:
            nodes_text = _failure_line(_TITLE_NODES, nodes_error)
        reconnect = []
    return PanelStatusView(
        panel=panel, users=users, traffic=traffic, nodes=nodes_text, reconnectable=reconnect
    )


async def reconnect_node_result(client: PasarGuardClient, node_id: int) -> tuple[bool, str]:
    """POST the reconnect. Returns (ok, Persian result line for the re-rendered screen)."""
    try:
        await client.reconnect_node(int(node_id))
    except PasarGuardAuthError:
        return False, f"⚠️ {PERMISSION_LINE}"
    except Exception as exc:
        logger.warning("Panel node reconnect failed (%s)", type(exc).__name__)
        return False, RECONNECT_FAILED
    return True, RECONNECT_SENT

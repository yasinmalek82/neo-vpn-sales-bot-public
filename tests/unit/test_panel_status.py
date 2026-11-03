"""Panel status: client reads, node model, best-effort blocks and formatting."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from api_client import PasarGuardClient
from api_client.models import PanelNode, SystemStats
from services import panel_status as ps

SYSTEM = {
    "version": "1.2.3",
    "uptime_seconds": 3 * 86400 + 4 * 3600 + 120,
    "total_user": 120,
    "online_users": 7,
    "active_users": 90,
    "on_hold_users": 2,
    "disabled_users": 3,
    "expired_users": 20,
    "limited_users": 5,
    "incoming_bandwidth": 5 * 1024**4,
    "outgoing_bandwidth": 350 * 1024**3,
    "mem_total": 4 * 1024**3,
    "mem_used": 1_288_490_189,
    "disk_total": 80 * 1024**3,
    "disk_used": 20 * 1024**3,
    "cpu_cores": 2,
    "cpu_usage": 12.4,
    "extra_field": "ignored",
}


def _node(id_: int, name: str, status: str, message: str | None = None, **extra: Any) -> dict:
    return {
        "id": id_,
        "name": name,
        "status": status,
        "message": message,
        "xray_version": "25.1",
        "core_version": "1.0",
        "uplink": 10,
        "downlink": 20,
        "api_key": "SECRET-NODE-KEY",
        "server_ca": "-----BEGIN CERTIFICATE-----",
        "address": "10.0.0.1",
        **extra,
    }


NODES = [
    _node(1, "tunnel-a", "connected"),
    _node(2, "tunnel-b", "error", "connection refused <x>"),
    _node(3, "tunnel-c", "connecting"),
    _node(4, "tunnel-d", "disabled"),
    _node(5, "tunnel-e", "limited", "data limit reached"),
]


def _client(handler: Any) -> tuple[PasarGuardClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://panel.example.com"
    )
    return (
        PasarGuardClient(base_url="https://panel.example.com", api_key="pg_key_test", client=http),
        http,
    )


def _ok_handler(calls: list[httpx.Request] | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        path = request.url.path
        if path == "/api/system":
            return httpx.Response(200, json=SYSTEM)
        if path == "/api/nodes":
            return httpx.Response(200, json={"nodes": NODES, "total": len(NODES)})
        if path == "/api/nodes/simple":
            simple = [{"id": n["id"], "name": n["name"], "status": n["status"]} for n in NODES]
            return httpx.Response(200, json={"nodes": simple, "total": len(simple)})
        if request.method == "POST" and path.endswith("/reconnect"):
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"detail": "nope"})

    return handler


# ── client ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_client_paths_params_and_models() -> None:
    calls: list[httpx.Request] = []
    client, http = _client(_ok_handler(calls))
    async with http:
        stats = await client.get_system_stats()
        nodes = await client.list_nodes()
        simple = await client.list_nodes_simple()
        await client.reconnect_node(2)
    assert isinstance(stats, SystemStats) and stats.total_user == 120 and stats.cpu_usage == 12.4
    assert [n.id for n in nodes] == [1, 2, 3, 4, 5]
    assert [n.status for n in simple][:2] == ["connected", "error"]
    seen = [(r.method, r.url.path, dict(r.url.params)) for r in calls]
    assert seen == [
        ("GET", "/api/system", {}),
        ("GET", "/api/nodes", {"limit": "100"}),
        ("GET", "/api/nodes/simple", {"all": "true"}),
        ("POST", "/api/node/2/reconnect", {}),
    ]
    assert all(r.headers["X-Api-Key"] == "pg_key_test" for r in calls)


def test_panel_node_model_never_carries_node_secrets() -> None:
    node = PanelNode.model_validate(NODES[1])
    assert not hasattr(node, "api_key") and not hasattr(node, "server_ca")
    dumped = node.model_dump_json()
    assert "SECRET-NODE-KEY" not in dumped and "BEGIN CERTIFICATE" not in dumped
    assert "SECRET-NODE-KEY" not in repr(node)
    assert set(PanelNode.model_fields) == {
        "id",
        "name",
        "status",
        "message",
        "xray_version",
        "core_version",
        "uplink",
        "downlink",
    }


# ── formatting ────────────────────────────────────────────────────────────


def test_bytes_format() -> None:
    assert ps.format_bytes_fa(None) == "—"
    assert ps.format_bytes_fa(5 * 1024**4) == "۵ ترابایت"
    assert ps.format_bytes_fa(int(1.5 * 1024**4)) == "۱٫۵ ترابایت"
    assert ps.format_bytes_fa(350 * 1024**3) == "۳۵۰ گیگ"
    assert ps.format_bytes_fa(int(2.5 * 1024**3)) == "۲٫۵ گیگ"
    assert ps.format_bytes_fa(500 * 1024**2) == "۵۰۰ مگابایت"


def test_uptime_format() -> None:
    assert ps.format_uptime_fa(3 * 86400 + 4 * 3600) == "۳ روز و ۴ ساعت"
    assert ps.format_uptime_fa(2 * 86400) == "۲ روز"
    assert ps.format_uptime_fa(5 * 3600 + 59) == "۵ ساعت"
    assert ps.format_uptime_fa(20 * 60) == "۲۰ دقیقه"


def test_blocks_use_persian_digits() -> None:
    stats = SystemStats.model_validate(SYSTEM)
    panel = ps.panel_block(stats)
    assert "1.2.3" in panel and "۳ روز و ۴ ساعت" in panel
    assert "پردازنده: ۱۲٪ (۲ هسته)" in panel
    assert "رم: ۱٫۲ گیگ از ۴ گیگ" in panel and "دیسک: ۲۰ گیگ از ۸۰ گیگ" in panel
    users = ps.users_block(stats)
    assert "کل: ۱۲۰" in users and "آنلاین الان: ۷" in users and "در انتظار شروع: ۲" in users
    assert not any(ch in users for ch in "0123456789")
    traffic = ps.traffic_block(stats)
    assert "دریافتی: ۵ ترابایت" in traffic and "ارسالی: ۳۵۰ گیگ" in traffic


def test_node_lines_emoji_message_and_escape() -> None:
    by_id = {n["id"]: PanelNode.model_validate(n) for n in NODES}
    assert ps.format_node_line(by_id[1]) == "🟢 tunnel-a"
    assert ps.format_node_line(by_id[3]) == "🟡 tunnel-c"
    assert ps.format_node_line(by_id[4]) == "⏸ tunnel-d"
    assert ps.format_node_line(by_id[2]) == "🔴 tunnel-b — connection refused &lt;x&gt;"
    assert ps.format_node_line(by_id[5]) == "📉 tunnel-e — data limit reached"
    # connected/connecting/disabled never print a message, even when the panel sends one.
    chatty = PanelNode.model_validate(_node(9, "n", "connected", "hello"))
    assert "hello" not in ps.format_node_line(chatty)


def test_node_message_is_trimmed_to_80_visible_characters() -> None:
    node = PanelNode.model_validate(_node(1, "n", "error", "x" * 500))
    line = ps.format_node_line(node)
    message = line.split(" — ", 1)[1]
    assert len(message) == ps.NODE_MESSAGE_MAX == 80 and message.endswith("…")


def test_node_name_is_escaped() -> None:
    node = PanelNode.model_validate(_node(1, "<b>evil</b>", "connected"))
    assert "<b>" not in ps.format_node_line(node)


def test_reconnect_button_only_for_error_and_connecting() -> None:
    nodes = [PanelNode.model_validate(n) for n in NODES]
    got = ps.reconnectable_nodes(nodes)
    assert [(n.id, n.name) for n in got] == [(2, "tunnel-b"), (3, "tunnel-c")]
    assert got[0].button_label == "🔄 وصل دوباره: tunnel-b"
    long = ps.ReconnectableNode(id=1, name="n" * 100)
    assert len(long.button_label) <= 40


# ── best-effort loader ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_panel_status_all_blocks() -> None:
    client, http = _client(_ok_handler())
    async with http:
        view = await ps.load_panel_status(client)
    assert "1.2.3" in view.panel and "کل: ۱۲۰" in view.users
    assert "tunnel-b" in view.nodes and [n.id for n in view.reconnectable] == [2, 3]
    assert view.text.count("\n\n") == 3


@pytest.mark.asyncio
async def test_system_failure_drops_only_the_system_blocks() -> None:
    base = _ok_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/system":
            return httpx.Response(500, json={"detail": "boom secret host db.internal"})
        return base(request)

    client, http = _client(handler)
    async with http:
        view = await ps.load_panel_status(client)
    assert ps.READ_FAILED_LINE in view.panel and ps.READ_FAILED_LINE in view.users
    assert ps.READ_FAILED_LINE in view.traffic
    assert "tunnel-a" in view.nodes and view.reconnectable
    assert "db.internal" not in view.text and "500" not in view.text


@pytest.mark.asyncio
async def test_nodes_failure_drops_only_the_nodes_block() -> None:
    base = _ok_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/nodes"):
            return httpx.Response(500)
        return base(request)

    client, http = _client(handler)
    async with http:
        view = await ps.load_panel_status(client)
    assert "1.2.3" in view.panel and "کل: ۱۲۰" in view.users
    assert ps.READ_FAILED_LINE in view.nodes and view.reconnectable == []


@pytest.mark.asyncio
async def test_detailed_node_list_failing_falls_back_to_the_simple_list() -> None:
    base = _ok_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/nodes":
            return httpx.Response(500)
        return base(request)

    client, http = _client(handler)
    async with http:
        view = await ps.load_panel_status(client)
    assert "🔴 tunnel-b" in view.nodes and "connection refused" not in view.nodes
    assert [n.id for n in view.reconnectable] == [2, 3]


@pytest.mark.asyncio
async def test_403_shows_the_permission_line_per_block() -> None:
    base = _ok_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/system":
            return httpx.Response(403, json={"detail": "forbidden"})
        return base(request)

    client, http = _client(handler)
    async with http:
        view = await ps.load_panel_status(client)
    assert "کلید API اجازهٔ دیدن این بخش را ندارد" in view.panel
    assert "کلید API اجازهٔ دیدن این بخش را ندارد" in view.traffic
    assert "کلید API اجازهٔ دیدن این بخش را ندارد" not in view.nodes


@pytest.mark.asyncio
async def test_unreachable_panel_never_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("db.internal refused")

    client, http = _client(handler)
    async with http:
        view = await ps.load_panel_status(client)
    assert ps.READ_FAILED_LINE in view.panel and ps.READ_FAILED_LINE in view.nodes
    assert "db.internal" not in view.text


@pytest.mark.asyncio
async def test_reconnect_posts_once_and_reports() -> None:
    calls: list[httpx.Request] = []
    client, http = _client(_ok_handler(calls))
    async with http:
        ok, line = await ps.reconnect_node_result(client, 2)
    assert ok and line == "✅ درخواست اتصال دوباره فرستاده شد"
    assert [(c.method, c.url.path) for c in calls] == [("POST", "/api/node/2/reconnect")]


@pytest.mark.asyncio
async def test_reconnect_failure_has_no_raw_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "internal host 10.0.0.9"})

    client, http = _client(handler)
    async with http:
        ok, line = await ps.reconnect_node_result(client, 2)
    assert not ok and "10.0.0.9" not in line


@pytest.mark.asyncio
async def test_forbidden_node_list_says_how_to_fix_it() -> None:
    base = _ok_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/nodes"):
            return httpx.Response(403)
        return base(request)

    client, http = _client(handler)
    async with http:
        view = await ps.load_panel_status(client)
    assert ps.NODES_PERMISSION_LINE in view.nodes and "نودها" in view.nodes
    assert "1.2.3" in view.panel and view.reconnectable == []

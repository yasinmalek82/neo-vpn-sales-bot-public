"""Panel/node watcher: debounce, recovery, event keys, home lines and the job switch."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from api_client import PasarGuardClient
from api_client.exceptions import PasarGuardAuthError
from bot.admin_home import home_text
from bot.config import Settings
from database.base import Base
from database.models import PanelNodeState, ReportOutbox
from database.models.report import TOPIC_SYSTEM
from scheduler.jobs import panel_watch as job
from services.admin_home import get_home_status
from services.panel_watch import check_panel_and_nodes

T0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


class FakeClient:
    """Stands in for PasarGuardClient: a node table the test edits between checks."""

    def __init__(self) -> None:
        self.nodes: dict[int, tuple[str, str, str | None]] = {}
        self.panel_down = False
        self.auth_error = False
        self.auth_status = 403
        self.probe_ok = False
        self.probe_calls = 0
        self.detail_fails = False
        self.detail_calls = 0

    def set(self, node_id: int, name: str, status: str, message: str | None = None) -> None:
        self.nodes[node_id] = (name, status, message)

    async def list_nodes_simple(self) -> list[Any]:
        if self.auth_error:
            raise PasarGuardAuthError("unauthorized", status_code=self.auth_status)
        if self.panel_down:
            raise httpx.ConnectError("db.internal refused")
        return [SimpleNamespace(id=i, name=n, status=s) for i, (n, s, _m) in self.nodes.items()]

    async def get_current_admin(self) -> Any:
        self.probe_calls += 1
        if not self.probe_ok:
            raise PasarGuardAuthError("unauthorized", status_code=403)
        return SimpleNamespace(username="bot")

    async def list_nodes(self, **_kw: Any) -> list[Any]:
        self.detail_calls += 1
        if self.detail_fails:
            raise httpx.ConnectError("boom")
        return [
            SimpleNamespace(id=i, name=n, status=s, message=m)
            for i, (n, s, m) in self.nodes.items()
        ]


@pytest_asyncio.fixture
async def factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def tick(factory: Any, client: FakeClient, minute: int) -> list[Any]:
    """One watcher pass in its own committed transaction, like the job."""
    async with factory() as session:
        result = await check_panel_and_nodes(session, client, at(minute))  # type: ignore[arg-type]
        await session.commit()
    return result.reports


async def outbox(factory: Any) -> list[ReportOutbox]:
    async with factory() as session:
        return list((await session.scalars(select(ReportOutbox).order_by(ReportOutbox.id))).all())


async def state(factory: Any, node_id: int) -> PanelNodeState | None:
    async with factory() as session:
        return await session.get(PanelNodeState, node_id)


# ── debounce ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_down_check_is_silent_second_alerts_once(factory: Any) -> None:
    client = FakeClient()
    client.set(2, "tunnel-b", "error", "connection refused")
    assert await tick(factory, client, 0) == []
    assert await outbox(factory) == []
    second = await tick(factory, client, 2)
    assert len(second) == 1
    rows = await outbox(factory)
    assert len(rows) == 1 and rows[0].topic_key == TOPIC_SYSTEM
    assert "tunnel-b" in rows[0].body and "connection refused" in rows[0].body
    assert client.detail_calls == 1


@pytest.mark.asyncio
async def test_third_down_check_adds_nothing(factory: Any) -> None:
    client = FakeClient()
    client.set(2, "tunnel-b", "error")
    for minute in (0, 2, 4, 6):
        await tick(factory, client, minute)
    assert len(await outbox(factory)) == 1
    row = await state(factory, 2)
    assert row is not None and row.checks_in_status == 4


@pytest.mark.asyncio
async def test_healthy_fleet_never_reads_node_details(factory: Any) -> None:
    client = FakeClient()
    client.set(1, "a", "connected")
    for minute in (0, 2, 4):
        await tick(factory, client, minute)
    assert client.detail_calls == 0 and await outbox(factory) == []


@pytest.mark.asyncio
async def test_alert_survives_unavailable_detail_read(factory: Any) -> None:
    client = FakeClient()
    client.detail_fails = True
    client.set(2, "tunnel-b", "error", "x")
    await tick(factory, client, 0)
    await tick(factory, client, 2)
    rows = await outbox(factory)
    assert len(rows) == 1 and "tunnel-b" in rows[0].body


@pytest.mark.asyncio
async def test_flap_down_up_down_respects_debounce(factory: Any) -> None:
    client = FakeClient()
    client.set(2, "tunnel-b", "error")
    await tick(factory, client, 0)
    client.set(2, "tunnel-b", "connected")
    await tick(factory, client, 2)
    client.set(2, "tunnel-b", "error")
    await tick(factory, client, 4)
    assert await outbox(factory) == [], "two single failed checks are not an outage"
    await tick(factory, client, 6)
    assert len(await outbox(factory)) == 1


# ── recovery ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recovered_alerts_once_with_duration(factory: Any) -> None:
    client = FakeClient()
    client.set(2, "tunnel-b", "error")
    await tick(factory, client, 0)
    await tick(factory, client, 2)
    client.set(2, "tunnel-b", "connected")
    assert await tick(factory, client, 12) == [], "one connected check is not a recovery"
    back = await tick(factory, client, 14)
    assert len(back) == 1
    await tick(factory, client, 16)
    rows = await outbox(factory)
    assert len(rows) == 2
    assert "دوباره وصل شد" in rows[1].body and "۱۲ دقیقه" in rows[1].body
    row = await state(factory, 2)
    assert row is not None and row.reported_status is None and row.reported_since is None


@pytest.mark.asyncio
async def test_recovery_through_connecting_measures_the_whole_outage(factory: Any) -> None:
    client = FakeClient()
    client.set(2, "tunnel-b", "error")
    await tick(factory, client, 0)
    await tick(factory, client, 2)
    client.set(2, "tunnel-b", "connecting")
    assert await tick(factory, client, 10) == [], "connecting is neither recovery nor alert"
    client.set(2, "tunnel-b", "connected")
    await tick(factory, client, 12)
    await tick(factory, client, 14)
    rows = await outbox(factory)
    assert len(rows) == 2 and "۱۲ دقیقه" in rows[1].body


@pytest.mark.asyncio
async def test_node_never_reported_does_not_send_recovered(factory: Any) -> None:
    client = FakeClient()
    client.set(2, "tunnel-b", "error")
    await tick(factory, client, 0)
    client.set(2, "tunnel-b", "connected")
    await tick(factory, client, 2)
    assert await outbox(factory) == []


# ── silent and warning statuses ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_and_connecting_never_alert(factory: Any) -> None:
    client = FakeClient()
    client.set(1, "off", "disabled")
    client.set(2, "busy", "connecting")
    for minute in range(0, 12, 2):
        await tick(factory, client, minute)
    assert await outbox(factory) == []


@pytest.mark.asyncio
async def test_disabling_a_reported_node_clears_it_so_enabling_is_not_a_recovery(
    factory: Any,
) -> None:
    client = FakeClient()
    client.set(2, "tunnel-b", "error")
    await tick(factory, client, 0)
    await tick(factory, client, 2)
    client.set(2, "tunnel-b", "disabled")
    await tick(factory, client, 4)
    client.set(2, "tunnel-b", "connected")
    await tick(factory, client, 6)
    assert len(await outbox(factory)) == 1


@pytest.mark.asyncio
async def test_limited_is_a_warning_after_two_checks_then_recovers(factory: Any) -> None:
    client = FakeClient()
    client.set(3, "tunnel-c", "limited", "data limit reached")
    await tick(factory, client, 0)
    assert await outbox(factory) == []
    await tick(factory, client, 2)
    rows = await outbox(factory)
    assert len(rows) == 1 and "سقف حجم" in rows[0].body and "data limit reached" in rows[0].body
    client.set(3, "tunnel-c", "connected")
    await tick(factory, client, 4)
    await tick(factory, client, 6)
    assert len(await outbox(factory)) == 2


@pytest.mark.asyncio
async def test_message_is_escaped_and_trimmed_in_the_report(factory: Any) -> None:
    client = FakeClient()
    client.set(2, "<b>n</b>", "error", "<script>" + "x" * 300)
    await tick(factory, client, 0)
    await tick(factory, client, 2)
    body = (await outbox(factory))[0].body
    assert "<script>" not in body and "&lt;script&gt;" in body and "x" * 100 not in body
    assert "<b>n</b>" not in body


# ── the panel itself ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_panel_unreachable_then_back(factory: Any) -> None:
    client = FakeClient()
    client.set(2, "tunnel-b", "error")
    await tick(factory, client, 0)
    before = await state(factory, 2)
    client.panel_down = True
    assert await tick(factory, client, 2) == []
    assert len(await tick(factory, client, 4)) == 1
    assert await tick(factory, client, 6) == []
    rows = await outbox(factory)
    assert len(rows) == 1 and "پنل در دسترس نیست" in rows[0].body
    assert "db.internal" not in rows[0].body
    after = await state(factory, 2)
    assert before is not None and after is not None
    assert (after.checks_in_status, after.status) == (before.checks_in_status, before.status)
    client.panel_down = False
    client.set(2, "tunnel-b", "connected")
    assert await tick(factory, client, 8) == []
    assert len(await tick(factory, client, 10)) == 1
    rows = await outbox(factory)
    assert len(rows) == 2 and "دوباره در دسترس" in rows[1].body and "۶ دقیقه" in rows[1].body
    panel = await state(factory, 0)
    assert panel is not None and panel.status == "connected" and panel.reported_status is None


@pytest.mark.asyncio
async def test_refused_key_counts_as_panel_unreachable(factory: Any) -> None:
    client = FakeClient()
    client.auth_error = True
    await tick(factory, client, 0)
    assert len(await tick(factory, client, 2)) == 1


@pytest.mark.asyncio
async def test_vanished_node_is_removed_without_alert(factory: Any) -> None:
    client = FakeClient()
    client.set(2, "tunnel-b", "error")
    client.set(3, "tunnel-c", "connected")
    await tick(factory, client, 0)
    await tick(factory, client, 2)
    assert len(await outbox(factory)) == 1
    del client.nodes[2]
    assert await tick(factory, client, 4) == []
    assert await state(factory, 2) is None and await state(factory, 3) is not None
    assert len(await outbox(factory)) == 1


# ── event keys ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_event_keys_name_the_transition_and_differ_per_outage(factory: Any) -> None:
    client = FakeClient()
    keys: list[str] = []
    for status, minute in (
        ("error", 0),
        ("error", 2),
        ("connected", 4),
        ("connected", 5),
        ("error", 6),
        ("error", 8),
        ("connected", 10),
        ("connected", 12),
    ):
        client.set(2, "tunnel-b", status)
        await tick(factory, client, minute)
    keys = [r.event_key for r in await outbox(factory)]
    assert keys == [
        f"panel_node:2:error:{at(0).isoformat()}",
        f"panel_node:2:connected:{at(4).isoformat()}",
        f"panel_node:2:error:{at(6).isoformat()}",
        f"panel_node:2:connected:{at(10).isoformat()}",
    ]
    assert len(set(keys)) == 4


@pytest.mark.asyncio
async def test_panel_event_keys_use_node_id_zero(factory: Any) -> None:
    client = FakeClient()
    client.panel_down = True
    await tick(factory, client, 0)
    await tick(factory, client, 2)
    client.panel_down = False
    await tick(factory, client, 4)
    await tick(factory, client, 5)
    keys = [r.event_key for r in await outbox(factory)]
    assert keys == [
        f"panel_node:0:unreachable:{at(0).isoformat()}",
        f"panel_node:0:connected:{at(4).isoformat()}",
    ]


@pytest.mark.asyncio
async def test_same_transition_reported_twice_is_one_report(factory: Any) -> None:
    from services.report_events import panel_node_down
    from services.report_outbox import enqueue_report

    report = panel_node_down(node_id=2, name="n", status="error", since=T0)
    async with factory() as session:
        first = await enqueue_report(
            session, event_key=report.event_key, topic_key=report.topic_key, body=report.body
        )
        second = await enqueue_report(
            session, event_key=report.event_key, topic_key=report.topic_key, body=report.body
        )
        await session.commit()
    assert first is not None and second is None


# ── home lines ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_home_status_lists_only_nodes_down_for_two_checks(factory: Any) -> None:
    client = FakeClient()
    client.set(1, "fine", "connected")
    client.set(2, "tunnel-b", "error")
    client.set(3, "tunnel-c", "limited")
    client.set(4, "off", "disabled")
    await tick(factory, client, 0)
    async with factory() as session:
        assert (await get_home_status(session, now=at(1))).panel_down == []
    await tick(factory, client, 2)
    async with factory() as session:
        status = await get_home_status(session, now=at(3))
    assert [(d.name, d.is_panel) for d in status.panel_down] == [("tunnel-b", False)]
    assert status.panel_down[0].since == at(0)
    client.panel_down = True
    await tick(factory, client, 4)
    await tick(factory, client, 6)
    async with factory() as session:
        status = await get_home_status(session, now=at(7))
    assert [(d.name, d.is_panel) for d in status.panel_down] == [
        ("panel", True),
        ("tunnel-b", False),
    ]
    text = home_text(status)
    assert "⚠️ پنل در دسترس نیست (از " in text and "⚠️ سرور «tunnel-b» قطع است (از " in text
    assert text.index("پنل در دسترس نیست") < text.index("tunnel-b")


# ── job switch ────────────────────────────────────────────────────────────


def _settings(interval: int) -> Settings:
    return Settings.model_validate(
        {
            "BOT_TOKEN": "test-token",
            "SCHEDULER_ENABLED": True,
            "PANEL_WATCH_INTERVAL_SECONDS": interval,
            "BACKUP_ENABLED": False,
            "PASARGUARD_API_KEY": "pg_key_test",
        }
    )


def test_interval_default_is_120() -> None:
    assert Settings.model_validate({"BOT_TOKEN": "t"}).panel_watch_interval_seconds == 120


@pytest.mark.asyncio
async def test_interval_zero_disables_job_and_registration(factory: Any) -> None:
    from scheduler import setup_scheduler, shutdown_scheduler

    assert await job.run_panel_watch(factory, _settings(0)) is None
    assert not job.panel_watch_enabled(_settings(0)) and job.panel_watch_enabled(_settings(120))
    try:
        off = setup_scheduler(bot=SimpleNamespace(), session_factory=factory, settings=_settings(0))  # type: ignore[arg-type]
        assert off is not None and off.get_job("panel_watch") is None
        await shutdown_scheduler()
        on = setup_scheduler(bot=SimpleNamespace(), session_factory=factory, settings=_settings(90))  # type: ignore[arg-type]
        assert on is not None
        watch = on.get_job("panel_watch")
        assert watch is not None and watch.trigger.interval.total_seconds() == 90
        assert watch.max_instances == 1 and watch.coalesce
    finally:
        await shutdown_scheduler()


@pytest.mark.asyncio
async def test_job_queues_reports_and_commits(
    factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient()
    client.set(2, "tunnel-b", "error")

    class Closable(FakeClient):
        async def aclose(self) -> None:
            return None

    fake = Closable()
    fake.nodes = client.nodes
    monkeypatch.setattr(job, "create_pasarguard_client_from_config", lambda _cfg: fake)
    settings = _settings(120)
    assert await job.run_panel_watch(factory, settings, now=at(0)) == 0
    assert await job.run_panel_watch(factory, settings, now=at(2)) == 1
    assert len(await outbox(factory)) == 1


@pytest.mark.asyncio
async def test_job_skips_when_a_pass_is_still_running(
    factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await job._watch_lock.acquire()
    try:
        assert await job.run_panel_watch(factory, _settings(120)) is None
    finally:
        job._watch_lock.release()


@pytest.mark.asyncio
async def test_job_does_nothing_without_an_api_key(factory: Any) -> None:
    settings = Settings.model_validate(
        {"BOT_TOKEN": "t", "PANEL_WATCH_INTERVAL_SECONDS": 120, "PASARGUARD_API_KEY": ""}
    )
    assert await job.run_panel_watch(factory, settings) is None
    assert await state(factory, 0) is None


@pytest.mark.asyncio
async def test_real_client_end_to_end(factory: Any) -> None:
    """Through PasarGuardClient + MockTransport, so the wire shapes are exercised too."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/nodes/simple":
            body = {"nodes": [{"id": 7, "name": "edge", "status": "error"}], "total": 1}
            return httpx.Response(200, json=body)
        if request.url.path == "/api/nodes":
            node = {"id": 7, "name": "edge", "status": "error", "message": "timeout"}
            return httpx.Response(200, json={"nodes": [node], "total": 1})
        return httpx.Response(404)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://panel.example.com"
    )
    client = PasarGuardClient(base_url="https://panel.example.com", api_key="k", client=http)
    async with http:
        for minute in (0, 2):
            async with factory() as session:
                await check_panel_and_nodes(session, client, at(minute))
                await session.commit()
    rows = await outbox(factory)
    assert len(rows) == 1 and "edge" in rows[0].body and "timeout" in rows[0].body


# ── error -> connecting -> error is one outage ───────────────────────────


@pytest.mark.asyncio
async def test_error_connecting_flapping_alerts_exactly_once(factory: Any) -> None:
    client = FakeClient()
    for minute, status in enumerate(["error", "connecting", "error", "connecting", "error"]):
        client.set(2, "tunnel-b", status)
        await tick(factory, client, minute * 2)
        if minute == 0:
            assert await outbox(factory) == []
    assert len(await outbox(factory)) == 1
    row = await state(factory, 2)
    assert row is not None and row.status == "error" and row.checks_in_status == 5


@pytest.mark.asyncio
async def test_connecting_on_a_down_row_keeps_status_and_start(factory: Any) -> None:
    client = FakeClient()
    client.set(2, "tunnel-b", "error")
    await tick(factory, client, 0)
    client.set(2, "tunnel-b", "connecting")
    await tick(factory, client, 2)
    row = await state(factory, 2)
    assert row is not None and row.status == "error" and row.checks_in_status == 2
    assert row.status_since.replace(tzinfo=UTC) == at(0)


@pytest.mark.asyncio
async def test_error_connecting_connected_sends_one_recovery(factory: Any) -> None:
    client = FakeClient()
    client.set(2, "tunnel-b", "error")
    await tick(factory, client, 0)
    await tick(factory, client, 2)
    assert len(await outbox(factory)) == 1
    client.set(2, "tunnel-b", "connecting")
    await tick(factory, client, 4)
    client.set(2, "tunnel-b", "connected")
    await tick(factory, client, 6)
    await tick(factory, client, 8)
    assert len(await outbox(factory)) == 2  # one alert + exactly one recovery


@pytest.mark.asyncio
async def test_healthy_connecting_connected_sends_nothing(factory: Any) -> None:
    client = FakeClient()
    for minute, status in enumerate(["connected", "connecting", "connected", "connecting"]):
        client.set(1, "a", status)
        await tick(factory, client, minute * 2)
    assert await outbox(factory) == []
    row = await state(factory, 1)
    assert row is not None and row.status == "connecting" and row.checks_in_status == 1


@pytest.mark.asyncio
async def test_limited_connecting_limited_still_alerts_once(factory: Any) -> None:
    client = FakeClient()
    for minute, status in enumerate(["limited", "connecting", "limited"]):
        client.set(3, "c", status)
        await tick(factory, client, minute * 2)
    assert len(await outbox(factory)) == 1


@pytest.mark.asyncio
async def test_403_on_nodes_with_admin_probe_ok_never_alerts(factory: Any) -> None:
    client = FakeClient()
    client.set(2, "tunnel-b", "error")
    await tick(factory, client, 0)
    before = await state(factory, 2)
    client.auth_error = True
    client.probe_ok = True
    for minute in range(2, 20, 2):
        assert await tick(factory, client, minute) == []
    assert await outbox(factory) == []
    panel = await state(factory, 0)
    assert panel is not None and panel.status == "connected"
    after = await state(factory, 2)
    assert before is not None and after is not None
    assert (after.checks_in_status, after.status) == (before.checks_in_status, before.status)
    async with factory() as session:
        result = await check_panel_and_nodes(session, client, at(30))  # type: ignore[arg-type]
    assert result.nodes_readable is False and result.panel_reachable is True


@pytest.mark.asyncio
async def test_403_on_nodes_with_failing_probe_is_unreachable(factory: Any) -> None:
    client = FakeClient()
    client.auth_error = True
    await tick(factory, client, 0)
    assert len(await tick(factory, client, 2)) == 1
    assert client.probe_calls >= 1


@pytest.mark.asyncio
async def test_401_is_unreachable_without_probing(factory: Any) -> None:
    client = FakeClient()
    client.auth_error = True
    client.auth_status = 401
    client.probe_ok = True
    await tick(factory, client, 0)
    assert len(await tick(factory, client, 2)) == 1
    assert client.probe_calls == 0


@pytest.mark.asyncio
async def test_reported_unreachable_recovers_when_403_with_probe_ok(factory: Any) -> None:
    client = FakeClient()
    client.panel_down = True
    await tick(factory, client, 0)
    assert len(await tick(factory, client, 2)) == 1
    client.panel_down = False
    client.auth_error = True
    client.probe_ok = True
    assert await tick(factory, client, 4) == []
    assert len(await tick(factory, client, 6)) == 1
    assert await tick(factory, client, 8) == []
    rows = await outbox(factory)
    assert len(rows) == 2 and "دوباره در دسترس" in rows[1].body


@pytest.mark.asyncio
async def test_down_connected_down_flap_sends_no_recovery_and_no_second_alert(
    factory: Any,
) -> None:
    """One connected check between two error stretches is not a recovery."""
    client = FakeClient()
    for status, minute in (
        ("error", 0),
        ("error", 2),  # alert
        ("connected", 4),
        ("error", 6),
        ("error", 8),
    ):
        client.set(2, "tunnel-b", status)
        await tick(factory, client, minute)
    rows = await outbox(factory)
    assert len(rows) == 1, "no «recovered», and the same outage is not announced again"
    row = await state(factory, 2)
    assert row is not None and row.reported_status == "error"

"""Runtime health must follow the receiver, not merely a reachable database."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramConflictError
from aiogram.methods import GetMe, GetUpdates

from bot.runtime_health import RuntimeHealth, RuntimeHealthError, check_runtime_health


def test_polling_is_not_ready_before_a_successful_receive(tmp_path):
    health = RuntimeHealth("polling", path=tmp_path / "health.json")
    health.publish()
    with pytest.raises(RuntimeHealthError, match="receiver_not_ready"):
        check_runtime_health(health.path)


@pytest.mark.asyncio
async def test_conflict_fails_health_until_get_updates_really_succeeds(tmp_path):
    health = RuntimeHealth("polling", path=tmp_path / "health.json")
    call = AsyncMock(return_value=[])
    middleware = health.middleware()
    await middleware(call, None, GetUpdates())
    assert check_runtime_health(health.path)["mode"] == "polling"
    call.side_effect = TelegramConflictError(method=GetUpdates(), message="secret-fragment")
    with pytest.raises(TelegramConflictError):
        await middleware(call, None, GetUpdates())
    with pytest.raises(RuntimeHealthError, match="receiver_not_ready"):
        check_runtime_health(health.path)
    assert "secret-fragment" not in health.path.read_text()
    call.side_effect = None
    await middleware(call, None, GetMe())
    with pytest.raises(RuntimeHealthError):
        check_runtime_health(health.path)
    await middleware(call, None, GetUpdates())
    check_runtime_health(health.path)


def test_alive_event_loop_does_not_hide_a_stalled_poll(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("bot.runtime_health.time.time", lambda: clock[0])
    health = RuntimeHealth("polling", path=tmp_path / "health.json")
    health.received()
    clock[0] += 91
    health.publish()
    with pytest.raises(RuntimeHealthError, match="receiver_stale"):
        check_runtime_health(health.path)


def test_stale_loop_and_stopped_process_are_unhealthy(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("bot.runtime_health.time.time", lambda: clock[0])
    health = RuntimeHealth("webhook", path=tmp_path / "health.json")
    health.listener_ready(lambda: True)
    check_runtime_health(health.path)
    clock[0] += 91
    with pytest.raises(RuntimeHealthError, match="event_loop_stale"):
        check_runtime_health(health.path)
    health.stopped()
    with pytest.raises(RuntimeHealthError, match="runtime_stopped"):
        check_runtime_health(health.path)


def test_missing_malformed_future_and_dead_process_fail_closed(tmp_path, monkeypatch):
    path = tmp_path / "health.json"
    with pytest.raises(RuntimeHealthError):
        check_runtime_health(path)
    path.write_text("{invalid")
    with pytest.raises(RuntimeHealthError):
        check_runtime_health(path)
    health = RuntimeHealth("webhook", path=path)
    health.listener_ready(lambda: True)
    data = json.loads(path.read_text())
    data["loop_at"] += 1000
    path.write_text(json.dumps(data))
    with pytest.raises(RuntimeHealthError):
        check_runtime_health(path)
    health.publish()
    monkeypatch.setattr(
        "bot.runtime_health.os.kill", lambda *_args: (_ for _ in ()).throw(ProcessLookupError())
    )
    with pytest.raises(RuntimeHealthError, match="process_missing"):
        check_runtime_health(path)


@pytest.mark.asyncio
async def test_tick_task_and_shutdown_publish_actual_lifecycle(tmp_path):
    health = RuntimeHealth("polling", path=tmp_path / "health.json")
    bot = SimpleNamespace(session=SimpleNamespace(middleware=[]))
    # Runtime monitoring attaches to the actual session middleware chain.
    from aiogram.client.session.middlewares.manager import RequestMiddlewareManager

    bot.session.middleware = RequestMiddlewareManager()
    health.start(bot)
    assert any(type(m).__name__ == "ReceiveHealthMiddleware" for m in bot.session.middleware)
    health.received()
    check_runtime_health(health.path)
    assert health.path.stat().st_mode & 0o777 == 0o600
    await health.close()
    with pytest.raises(RuntimeHealthError, match="runtime_stopped"):
        check_runtime_health(health.path)


@pytest.mark.asyncio
async def test_monitor_disk_error_cannot_drop_a_received_update(tmp_path, monkeypatch):
    health = RuntimeHealth("polling", path=tmp_path / "health.json")

    def no_disk():
        raise OSError("private-filesystem-detail")

    monkeypatch.setattr(health, "publish", no_disk)
    call = AsyncMock(return_value=["update"])
    assert await health.middleware()(call, None, GetUpdates()) == ["update"]
    call.side_effect = TelegramConflictError(method=GetUpdates(), message="private-token")
    with pytest.raises(TelegramConflictError):
        await health.middleware()(call, None, GetUpdates())
    await health.close()


@pytest.mark.asyncio
async def test_healthcheck_uses_runtime_evidence_before_database(tmp_path, monkeypatch):
    from scripts.healthcheck import _check

    with pytest.raises(RuntimeHealthError):
        await _check(runtime_path=tmp_path / "missing.json")


@pytest.mark.asyncio
async def test_actual_heartbeat_task_refreshes_then_stops(tmp_path, monkeypatch):
    import asyncio

    import bot.runtime_health as module

    health = RuntimeHealth("webhook", path=tmp_path / "health.json")
    calls = []
    original = health.publish

    def record_publish():
        calls.append(1)
        original()

    monkeypatch.setattr(health, "publish", record_publish)
    original_sleep = asyncio.sleep

    async def fast_tick(_delay):
        await original_sleep(0.001)

    monkeypatch.setattr(module.asyncio, "sleep", fast_tick)
    from aiogram.client.session.middlewares.manager import RequestMiddlewareManager

    health.start(SimpleNamespace(session=SimpleNamespace(middleware=RequestMiddlewareManager())))
    try:
        await original_sleep(0.02)
        assert len(calls) > 1
    finally:
        await health.close()
    count = len(calls)
    await original_sleep(0.02)
    assert len(calls) == count


@pytest.mark.asyncio
async def test_real_main_wires_health_to_session_and_closes_it(tmp_path, monkeypatch):
    from bot import main
    from bot.client import create_bot
    from tests.e2e.harness.settings import make_settings
    from tests.e2e.harness.telegram import FakeTelegram

    settings = make_settings("sqlite+aiosqlite:///:memory:", tmp_path)
    bot = create_bot(settings, session=FakeTelegram())
    health = RuntimeHealth("polling", path=tmp_path / "health.json")

    async def poll(_bot):
        assert any(type(m).__name__ == "ReceiveHealthMiddleware" for m in bot.session.middleware)
        health.received()
        check_runtime_health(health.path)

    dp = SimpleNamespace(
        startup=SimpleNamespace(register=lambda _f: None),
        shutdown=SimpleNamespace(register=lambda _f: None),
        start_polling=AsyncMock(side_effect=poll),
    )
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "init_db", lambda _s: None)
    monkeypatch.setattr(main, "create_bot", lambda _s: bot)
    monkeypatch.setattr(main, "create_dispatcher", lambda: dp)
    monkeypatch.setattr(main, "RuntimeHealth", lambda _mode: health)
    monkeypatch.setattr(main, "get_session_factory", lambda: None)
    monkeypatch.setattr(main, "start_polling_panel", AsyncMock(return_value=None))
    monkeypatch.setattr(main, "shutdown_scheduler", AsyncMock())
    monkeypatch.setattr(main, "close_db", AsyncMock())
    try:
        await main.main()
        dp.start_polling.assert_awaited_once()
        with pytest.raises(RuntimeHealthError, match="runtime_stopped"):
            check_runtime_health(health.path)
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_webhook_listener_loss_is_unhealthy_even_with_a_live_loop(tmp_path):
    from aiohttp import web

    app = web.Application()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    health = RuntimeHealth("webhook", path=tmp_path / "health.json")
    try:
        await site.start()
        health.listener_ready(lambda: site in runner.sites and bool(runner.addresses))
        check_runtime_health(health.path)
        await site.stop()
        health.publish()
        with pytest.raises(RuntimeHealthError, match="receiver_not_ready"):
            check_runtime_health(health.path)
    finally:
        await runner.cleanup()

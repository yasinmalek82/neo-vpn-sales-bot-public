"""Audit F3 extras: suspects and defences that had no xfail proof in test_audit_jobs.py."""

from __future__ import annotations

import asyncio
import gzip
import sqlite3
import threading
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from bot.texts import texts
from database.base import Base
from database.models import Order, OrderStatus, PasarGuardAccount, Plan, User
from database.models.broadcast import BroadcastAudience, BroadcastTarget, DeliveryState
from services.automation import find_renewal_reminder_candidates, mark_reminder_sent
from services.broadcast import create_broadcast, run_pending_broadcasts
from services.db_backup import create_gzipped_dump
from services.reporting import get_finance_report, window_start


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def _seed(session: AsyncSession, tg: int) -> tuple[User, Plan]:
    user = User(telegram_id=tg, referral_code=f"X{tg}")
    plan = Plan(name="p", data_limit_gb=50, duration_days=30, price_toman=1000)
    session.add_all([user, plan])
    await session.flush()
    return user, plan


def _order(user: User, plan: Plan, **kw: object) -> Order:
    return Order(
        user_id=user.id,
        plan_id=plan.id,
        status=OrderStatus.ACTIVE,
        price_toman=1000,
        discount_toman=0,
        **kw,
    )


# --- JOBS-2 defence: one reminder per account --------------------------------


@pytest.mark.asyncio
async def test_one_reminder_per_account_when_two_active_orders_share_it(
    session: AsyncSession,
) -> None:
    user, plan = await _seed(session, 1)
    now = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    soon = now + timedelta(hours=20)
    # A switch to a shorter plan leaves two ACTIVE orders, both inside the window.
    session.add_all(
        [
            _order(user, plan, panel_username="svc_a", expires_at=soon, activated_at=now),
            _order(
                user,
                plan,
                panel_username="svc_a",
                expires_at=soon + timedelta(hours=1),
                activated_at=now,
            ),
        ]
    )
    await session.flush()

    got = await find_renewal_reminder_candidates(session, reminder_days=[1], now=now)

    assert len(got) == 1


# --- JOBS-3 edges -------------------------------------------------------------


@pytest.mark.asyncio
async def test_reminder_returns_for_the_next_threshold_and_after_a_renewal(
    session: AsyncSession,
) -> None:
    user, plan = await _seed(session, 2)
    t0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    order = _order(user, plan, expires_at=t0 + timedelta(days=2, hours=12), activated_at=t0)
    session.add(order)
    await session.flush()

    assert len(await find_renewal_reminder_candidates(session, reminder_days=[3, 1], now=t0)) == 1
    await mark_reminder_sent(session, order_id=order.id, now=t0)
    # Still the «3 days» window 6 hours later: nothing.
    assert (
        await find_renewal_reminder_candidates(
            session, reminder_days=[3, 1], now=t0 + timedelta(hours=6)
        )
        == []
    )
    # Inside the «1 day» window: the next threshold fires.
    later = t0 + timedelta(days=1, hours=13)
    assert (
        len(await find_renewal_reminder_candidates(session, reminder_days=[3, 1], now=later)) == 1
    )
    # A renewal moves expiry a month out, then back into a window: fires again.
    order.expires_at = later + timedelta(days=30)
    await mark_reminder_sent(session, order_id=order.id, now=later)
    again = order.expires_at - timedelta(hours=10)
    assert (
        len(await find_renewal_reminder_candidates(session, reminder_days=[3, 1], now=again)) == 1
    )


# --- window_start ---------------------------------------------------------------


def test_week_and_month_windows_start_at_tehran_midnight() -> None:
    now = datetime(2026, 10, 1, 0, 30, tzinfo=UTC)  # 04:00 Tehran, 1 Oct
    assert window_start(now, 1) == datetime(2026, 9, 30, 20, 30, tzinfo=UTC)
    assert window_start(now, 7) == datetime(2026, 9, 24, 20, 30, tzinfo=UTC)
    assert window_start(now, 30) == datetime(2026, 9, 1, 20, 30, tzinfo=UTC)


# --- SUSPECT-JOBS-5: only «recipient gone» BadRequests mean blocked --------------


class _BadRequestBot:
    def __init__(self, messages: dict[int, str]) -> None:
        self.messages = messages

    async def send_message(self, *, chat_id: int, **_: object) -> None:
        raise TelegramBadRequest(method=None, message=self.messages[chat_id])  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_only_recipient_gone_bad_requests_are_blocked(session: AsyncSession) -> None:
    for tg in (21, 22):
        session.add(User(telegram_id=tg, referral_code=f"B{tg}"))
    await session.flush()
    await create_broadcast(session, body="hi", audience=BroadcastAudience.all_customers)
    await session.commit()

    bot = _BadRequestBot(
        {21: "Bad Request: chat not found", 22: "Bad Request: message is too long"}
    )
    await run_pending_broadcasts(session, bot)  # type: ignore[arg-type]

    from sqlalchemy import select

    rows = {
        int(t.telegram_id): t.state
        for t in (await session.execute(select(BroadcastTarget))).scalars()
    }
    assert rows[21] is DeliveryState.blocked
    assert rows[22] is DeliveryState.failed


# --- SUSPECT-JOBS-1: backup ----------------------------------------------------------


def _make_db(path: Path) -> str:
    conn = sqlite3.connect(path)
    conn.execute("create table t (x)")
    conn.commit()
    conn.close()
    return f"sqlite+aiosqlite:///{path}"


def test_partial_gz_is_deleted_when_the_dump_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = _make_db(tmp_path / "src.db")
    dest = tmp_path / "out" / "dump.gz"

    def boom(src: object, dst: object, *a: object) -> None:
        dst.write(b"partial")  # type: ignore[attr-defined]
        raise OSError("disk full")

    monkeypatch.setattr("services.db_backup.shutil.copyfileobj", boom)
    with pytest.raises(OSError):
        create_gzipped_dump(url, dest)

    assert not dest.exists()


@pytest.mark.asyncio
async def test_backup_dump_runs_off_the_event_loop_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scheduler.jobs import backup as backup_job

    seen: list[int] = []

    def fake_dump(url: str, dest: Path) -> Path:  # noqa: ARG001
        seen.append(threading.get_ident())
        dest.write_bytes(gzip.compress(b"x"))  # the integrity check reads it back
        return dest

    monkeypatch.setattr(backup_job, "create_gzipped_dump", fake_dump)
    monkeypatch.setattr(backup_job, "count_core_rows", lambda _url: (0, 0, 0))

    async def no_send(*a: object, **k: object) -> bool:
        return True

    monkeypatch.setattr(backup_job, "_send_backup_document", no_send)
    settings = SimpleNamespace(
        backup_dir=str(tmp_path), database_url="x", backup_retain_daily=3, backup_enabled=True
    )
    await backup_job._run_backup(None, None, settings, max_document_bytes=10**9)  # type: ignore[arg-type]

    assert seen and seen[0] != threading.get_ident()


# --- SUSPECT-JOBS-3: digest misfire grace ------------------------------------------


@pytest.mark.asyncio
async def test_nightly_digest_job_survives_a_late_start() -> None:
    from bot.config import Settings
    from scheduler import setup_scheduler, shutdown_scheduler

    settings = Settings(BOT_TOKEN="t", SCHEDULER_ENABLED=True)  # type: ignore[call-arg]
    scheduler = setup_scheduler(bot=None, session_factory=None, settings=settings)  # type: ignore[arg-type]
    try:
        assert scheduler is not None
        job = scheduler.get_job("nightly_digest")
        assert job is not None
        assert job.misfire_grace_time == 3600 and job.coalesce is True
    finally:
        await shutdown_scheduler()
        await asyncio.sleep(0)


# --- SUSPECT-JOBS-6: the label says what is counted ---------------------------------


@pytest.mark.asyncio
async def test_registered_services_label_does_not_claim_active(session: AsyncSession) -> None:
    user, _ = await _seed(session, 40)
    session.add(PasarGuardAccount(user_id=user.id, panel_username="old", panel_user_id=9))
    await session.flush()  # an account with no ACTIVE order: long expired

    report = await get_finance_report(session, now=datetime(2026, 9, 30, tzinfo=UTC))

    assert report.active_services == 1  # counts every account ever created
    assert "سرویس فعال" not in texts.admin_stats

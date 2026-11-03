"""A scheduler job with nothing to do must not commit a write.

Every committed write moves the database on, and SQLite then refuses any other
session's first write if that session read before the commit (WAL,
SQLITE_BUSY_SNAPSHOT: «database is locked»). The jobs run every 20 to 120
seconds in the bot's own process, so an idle job that still wrote turned an
ordinary slow Telegram call into a failed tap.

``PRAGMA data_version`` changes, for a connection, exactly when another
connection commits a change. Read from a connection of its own, it is the
observable for «this job wrote».
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from database.models import Order, OrderStatus, Plan, User
from database.models.lifecycle import ServiceNotice
from database.models.marketing import NOTICE_MKT_CART, MarketingSettings
from database.models.plan import DataLimitResetStrategy
from database.models.report import REPORT_SETTINGS_ROW_ID, ReportSettings
from scheduler.jobs import panel_watch as panel_watch_job
from scheduler.jobs.broadcast_worker import run_broadcast_worker
from scheduler.jobs.expire_orders import run_expire_stale_orders
from scheduler.jobs.marketing import run_marketing_cart, run_marketing_hourly
from scheduler.jobs.renewal_reminders import run_renewal_reminders
from scheduler.jobs.report_flush import run_report_flush
from tests.unit.test_panel_watch import FakeClient

Factory = async_sessionmaker[AsyncSession]


class _Watcher:
    """A connection of its own, so it sees every other connection's commits."""

    def __init__(self, url: str) -> None:
        self._conn = sqlite3.connect(url.split("///", 1)[1])

    def version(self) -> int:
        return int(self._conn.execute("PRAGMA data_version").fetchone()[0])

    def close(self) -> None:
        self._conn.close()


class _Bot:
    """Nothing to deliver means nothing is sent: any call here is a bug in the test."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"an idle job called bot.{name}")


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "BOT_TOKEN": "test-token",
            "PANEL_WATCH_INTERVAL_SECONDS": 120,
            "BACKUP_ENABLED": False,
            "PASARGUARD_API_KEY": "pg_key_test",
        }
    )


async def _seed_a_quiet_shop(factory: Factory) -> None:
    """A shop with customers and a healthy service, and nothing due."""
    async with factory() as session:
        plan = Plan(
            name="volume-10GB-30d",
            description="test",
            data_limit_gb=10,
            duration_days=30,
            price_toman=150_000,
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            group_ids=[1],
            hwid_limit=2,
            is_active=True,
        )
        user = User(telegram_id=5000, first_name="C", referral_code="IDLE0001")
        session.add_all([plan, user])
        await session.flush()
        session.add(
            Order(
                user_id=user.id,
                plan_id=plan.id,
                status=OrderStatus.ACTIVE,
                price_toman=150_000,
                discount_toman=0,
                expires_at=datetime.now(UTC) + timedelta(days=25),
            )
        )
        session.add(
            ReportSettings(
                id=REPORT_SETTINGS_ROW_ID,
                chat_id=-1001234567890,
                topics={"sales": 11},
                verified={},
                enabled=True,
            )
        )
        await session.commit()


async def _assert_read_only(url: str, run: Callable[[], Awaitable[Any]]) -> None:
    watcher = _Watcher(url)
    try:
        before = watcher.version()
        await run()
        assert watcher.version() == before, "an idle run committed a write"
    finally:
        watcher.close()


@pytest.mark.asyncio
async def test_the_watcher_itself_sees_a_committed_write(
    two_sessions: tuple[Factory, str],
) -> None:
    """Without this, every test below would pass on a watcher that sees nothing."""
    factory, url = two_sessions
    await _seed_a_quiet_shop(factory)

    async def write() -> None:
        async with factory() as session:
            session.add(User(telegram_id=1, first_name="W", referral_code="IDLE0002"))
            await session.commit()

    with pytest.raises(AssertionError, match="committed a write"):
        await _assert_read_only(url, write)


@pytest.mark.asyncio
async def test_an_idle_broadcast_worker_writes_nothing(two_sessions: tuple[Factory, str]) -> None:
    factory, url = two_sessions
    await _seed_a_quiet_shop(factory)
    await _assert_read_only(url, lambda: run_broadcast_worker(factory, _Bot()))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_an_idle_report_flush_writes_nothing(two_sessions: tuple[Factory, str]) -> None:
    factory, url = two_sessions
    await _seed_a_quiet_shop(factory)
    await _assert_read_only(url, lambda: run_report_flush(factory, _Bot()))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_an_idle_order_expiry_writes_nothing(two_sessions: tuple[Factory, str]) -> None:
    factory, url = two_sessions
    await _seed_a_quiet_shop(factory)
    await _assert_read_only(
        url,
        lambda: run_expire_stale_orders(
            session_factory=factory, expiry_minutes=30, bot=_Bot(), settings=_settings()  # type: ignore[arg-type]
        ),
    )


@pytest.mark.asyncio
async def test_an_idle_reminder_pass_writes_nothing(two_sessions: tuple[Factory, str]) -> None:
    factory, url = two_sessions
    await _seed_a_quiet_shop(factory)
    await _assert_read_only(
        url,
        lambda: run_renewal_reminders(
            session_factory=factory, bot=_Bot(), reminder_days=[3, 1], settings=_settings()  # type: ignore[arg-type]
        ),
    )


@pytest.mark.asyncio
async def test_a_panel_watch_pass_over_a_steady_panel_writes_nothing(
    two_sessions: tuple[Factory, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Healthy yesterday, healthy now: there is no news to record.

    The first two passes do write: a new row, then the second consecutive
    check that the alert debounce counts. From the third on nothing changes.
    """
    factory, url = two_sessions
    await _seed_a_quiet_shop(factory)

    class Panel(FakeClient):
        async def aclose(self) -> None:
            return None

    panel = Panel()
    panel.nodes = {1: ("node-1", "connected", None), 2: ("node-2", "disabled", None)}
    monkeypatch.setattr(panel_watch_job, "create_pasarguard_client_from_config", lambda _cfg: panel)
    settings = _settings()
    for _warm_up in range(2):
        assert await panel_watch_job.run_panel_watch(factory, settings) == 0

    await _assert_read_only(url, lambda: panel_watch_job.run_panel_watch(factory, settings))


@pytest.mark.asyncio
async def test_a_steady_outage_still_counts_its_second_check_and_alerts(
    two_sessions: tuple[Factory, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not writing on a quiet pass must not eat the check that makes an alert due."""
    factory, _url = two_sessions
    await _seed_a_quiet_shop(factory)

    class Panel(FakeClient):
        async def aclose(self) -> None:
            return None

    panel = Panel()
    panel.nodes = {1: ("node-1", "connected", None)}
    monkeypatch.setattr(panel_watch_job, "create_pasarguard_client_from_config", lambda _cfg: panel)
    settings = _settings()
    for _steady in range(4):
        assert await panel_watch_job.run_panel_watch(factory, settings) == 0
    panel.nodes = {1: ("node-1", "error", None)}
    assert await panel_watch_job.run_panel_watch(factory, settings) == 0
    assert await panel_watch_job.run_panel_watch(factory, settings) == 1
    assert await panel_watch_job.run_panel_watch(factory, settings) == 0


# --- the volume warning (services/volume_warnings.py) -------------------------


class _PanelUser:
    """What `volume_verdict` and the period read from a panel user."""

    class _Status:
        value = "active"

    def __init__(self, *, used_gb: float, limit_gb: int = 10) -> None:
        self.status = self._Status()
        self.used_traffic = int(used_gb * 1024**3)
        self.data_limit = limit_gb * 1024**3
        self.expire = None


class _VolumePanel:
    def __init__(self, used_gb: float) -> None:
        self.used_gb = used_gb
        self.reads = 0

    async def get_user(self, _username: str) -> _PanelUser:
        self.reads += 1
        return _PanelUser(used_gb=self.used_gb)

    async def aclose(self) -> None:
        return None


async def _seed_a_service(factory: Factory) -> None:
    from database.models import PasarGuardAccount

    async with factory() as session:
        user = (await session.execute(select(User))).scalars().first()
        assert user is not None
        session.add(
            PasarGuardAccount(
                user_id=user.id,
                panel_user_id=7,
                panel_username="idle_svc",
                display_name="idle",
                service_name="idle_svc",
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_an_idle_volume_warning_pass_writes_nothing(
    two_sessions: tuple[Factory, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A healthy service, and a low one whose warning was already sent, are reads only."""
    from services import volume_warnings as vw

    factory, url = two_sessions
    await _seed_a_quiet_shop(factory)
    await _seed_a_service(factory)
    panel = _VolumePanel(used_gb=2)
    monkeypatch.setattr(vw, "get_pasarguard_client", _async_returning(panel))

    async def run() -> int:
        return await vw.warn_low_volume(
            session_factory=factory, bot=_Bot(), settings=_settings(), pause_seconds=0  # type: ignore[arg-type]
        )

    await _assert_read_only(url, run)
    assert panel.reads == 1, "the pass did look at the panel"


@pytest.mark.asyncio
async def test_a_service_that_stays_low_is_not_written_again_after_its_warning(
    two_sessions: tuple[Factory, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from services import volume_warnings as vw

    factory, url = two_sessions
    await _seed_a_quiet_shop(factory)
    await _seed_a_service(factory)
    panel = _VolumePanel(used_gb=9)
    monkeypatch.setattr(vw, "get_pasarguard_client", _async_returning(panel))

    class Bot:
        sent = 0

        async def send_message(self, **_kw: Any) -> None:
            Bot.sent += 1

    async def run() -> int:
        return await vw.warn_low_volume(
            session_factory=factory, bot=Bot(), settings=_settings(), pause_seconds=0  # type: ignore[arg-type]
        )

    assert await run() == 1 and Bot.sent == 1  # the one real warning writes its claim
    await _assert_read_only(url, run)
    assert Bot.sent == 1


def _async_returning(value: Any) -> Callable[..., Awaitable[Any]]:
    async def make(*_a: Any, **_kw: Any) -> Any:
        return value

    return make


# --- the daily lifecycle job (services/lifecycle_run.py) ---------------------------


class _LifecyclePanelUser:
    class _Status:
        def __init__(self, value: str) -> None:
            self.value = value

    def __init__(self, status: str) -> None:
        self.status = self._Status(status)
        self.expire = None
        self.data_limit = 10 * 1024**3
        self.used_traffic = 0


class _LifecyclePanel:
    def __init__(self, status: str) -> None:
        self.status = status
        self.reads = 0
        self.deletes = 0

    async def get_user(self, _username: str) -> _LifecyclePanelUser:
        self.reads += 1
        return _LifecyclePanelUser(self.status)

    async def delete_user(self, _username: str) -> None:
        self.deletes += 1

    async def aclose(self) -> None:
        return None


async def _set_mode(factory: Factory, mode: str) -> None:
    from database.models.lifecycle import LifecycleSettings

    async with factory() as session:
        session.add(
            LifecycleSettings(
                id=1, warn_days=[3, 1], warn_volume_percents=[20, 5], delete_mode=mode
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_a_lifecycle_run_with_deleting_off_writes_nothing_and_reads_no_panel(
    two_sessions: tuple[Factory, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from services import lifecycle_run as lr

    factory, url = two_sessions
    await _seed_a_quiet_shop(factory)
    await _seed_a_service(factory)
    panel = _LifecyclePanel("expired")
    monkeypatch.setattr(lr, "get_pasarguard_client", _async_returning(panel))

    async def run() -> object:
        return await lr.run_lifecycle(
            session_factory=factory, bot=_Bot(), settings=_settings(), pause_seconds=0  # type: ignore[arg-type]
        )

    await _assert_read_only(url, run)
    assert panel.reads == 0 and panel.deletes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "expired"])
async def test_a_lifecycle_run_with_deleting_on_and_nothing_to_do_writes_nothing(
    two_sessions: tuple[Factory, str], monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """A healthy service, and an ended one that is too young to delete (created just now)."""
    from services import lifecycle_run as lr

    factory, url = two_sessions
    await _seed_a_quiet_shop(factory)
    await _seed_a_service(factory)  # created now: «recent_change» keeps it
    await _set_mode(factory, "on")
    panel = _LifecyclePanel(status)
    monkeypatch.setattr(lr, "get_pasarguard_client", _async_returning(panel))

    async def run() -> object:
        return await lr.run_lifecycle(
            session_factory=factory, bot=_Bot(), settings=_settings(), pause_seconds=0  # type: ignore[arg-type]
        )

    await _assert_read_only(url, run)
    assert panel.reads == 1 and panel.deletes == 0


async def _all_campaigns_on(factory: Factory) -> None:
    async with factory() as session:
        moment = datetime.now(UTC) - timedelta(days=30)
        session.add(
            MarketingSettings(
                id=1,
                cart_enabled_at=moment,
                trial_convert_enabled_at=moment,
                winback_enabled_at=moment,
                upgrade_enabled_at=moment,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_the_marketing_jobs_with_every_campaign_off_write_nothing(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, url = two_sessions
    await _seed_a_quiet_shop(factory)
    settings = _settings()
    await _assert_read_only(
        url, lambda: run_marketing_cart(session_factory=factory, bot=_Bot(), settings=settings)  # type: ignore[arg-type]
    )
    await _assert_read_only(
        url, lambda: run_marketing_hourly(session_factory=factory, bot=_Bot(), settings=settings)  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_the_marketing_jobs_with_nothing_due_write_nothing(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, url = two_sessions
    await _seed_a_quiet_shop(factory)
    await _all_campaigns_on(factory)
    settings = _settings()
    await _assert_read_only(
        url, lambda: run_marketing_cart(session_factory=factory, bot=_Bot(), settings=settings)  # type: ignore[arg-type]
    )
    await _assert_read_only(
        url, lambda: run_marketing_hourly(session_factory=factory, bot=_Bot(), settings=settings)  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_an_order_already_reminded_is_looked_at_and_not_written_again(
    two_sessions: tuple[Factory, str],
) -> None:
    """The reminder stays a candidate for the rest of its window, run after run."""
    factory, url = two_sessions
    await _seed_a_quiet_shop(factory)
    await _all_campaigns_on(factory)
    async with factory() as session:
        plan = (await session.scalars(select(Plan))).one()
        user = User(telegram_id=6000, first_name="D", referral_code="IDLE0006")
        session.add(user)
        await session.flush()
        order = Order(
            user_id=user.id,
            plan_id=plan.id,
            status=OrderStatus.PENDING_PAYMENT,
            price_toman=150_000,
            discount_toman=0,
            created_at=datetime.now(UTC) - timedelta(minutes=20),
        )
        session.add(order)
        await session.flush()
        stamp = datetime.now(UTC)
        session.add(
            ServiceNotice(
                event_key=f"mkt:cart:{order.id}",
                kind=NOTICE_MKT_CART,
                account_id=None,
                user_id=user.id,
                sent_at=stamp,
                delivered_at=stamp,
            )
        )
        await session.commit()
    settings = _settings()
    await _assert_read_only(
        url, lambda: run_marketing_cart(session_factory=factory, bot=_Bot(), settings=settings)  # type: ignore[arg-type]
    )

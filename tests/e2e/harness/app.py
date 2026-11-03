"""The assembled bot, wired to fakes, for one scenario.

``running_app`` builds everything a scenario needs and puts every piece of global
state back afterwards, so the existing suite is unaffected in the same pytest run:

* a private copy of the migrated database, with ``database.base`` pointed at it;
* ``bot.config`` / ``bot.main`` settings swapped for the explicit harness values;
* the real ``create_dispatcher()`` (real routers, middlewares, MemoryStorage,
  SimpleEventIsolation). aiogram routers are module singletons that attach to
  one parent only, so they are detached from any dispatcher the session already
  built and re-attached on the way out;
* every ``PasarGuardClient`` redirected to the fake panel;
* the scheduler replaced by a recorder, so ``run_job`` can call the real job
  functions with the exact arguments production registers.
"""

# ruff: noqa: ARG002  -- handlers share one signature; early returns read better here

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, Router
from aiogram.types import Update
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import database.base as db_base
from bot.client import create_bot
from bot.config import Settings
from tests.e2e.harness.actors import Actor
from tests.e2e.harness.db import copy_template
from tests.e2e.harness.panel import FakePanel
from tests.e2e.harness.panel import install as install_panel
from tests.e2e.harness.settings import make_settings
from tests.e2e.harness.telegram import ChatState, FakeTelegram, SentMessage

JOBS = {
    "expire": "expire_stale_orders",
    "report_flush": "report_flush",
    "delivery": "delivery_flush",
    "provision": "provision_recovery",
    "broadcast": "broadcast_worker",
    "cleanup": "cleanup",
    "digest": "nightly_digest",
    "reminders": "renewal_reminders",
    "volume": "volume_warnings",
    "marketing_cart": "marketing_cart",
    "marketing_hourly": "marketing_hourly",
    "lifecycle": "service_lifecycle",
    "panel_watch": "panel_watch",
    "backup": "database_backup",
}


class _SchedulerRecorder:
    """Stands in for ``AsyncIOScheduler``: remembers each job, runs none."""

    def __init__(self) -> None:
        self.jobs: dict[str, tuple[Any, dict[str, Any]]] = {}
        self.running = False

    def add_job(self, func: Any, trigger: Any = None, *, id: str, **options: Any) -> None:
        self.jobs[id] = (func, dict(options.get("kwargs") or {}))

    def start(self) -> None:
        self.running = True

    def shutdown(self, wait: bool = False) -> None:
        self.running = False


class _ErrorCapture(logging.Handler):
    """Collects what ``ErrorHandlingMiddleware`` swallowed.

    The middleware turns any exception into a generic apology, so a scenario that
    only reads the screen would pass while the handler crashed.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.unhandled: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage() == "Unhandled bot error" and record.exc_info:
            exc = record.exc_info[1]
            self.unhandled.append(f"{type(exc).__name__}: {exc}")


class ForumGroup:
    """The report group, as the owner sees it: topics, and what landed in each."""

    def __init__(self, app: App, chat: ChatState) -> None:
        self.app = app
        self.chat = chat

    async def _threads(self) -> dict[str, int]:
        from database.models.report import ReportSettings

        async with self.app.session() as session:
            row = await session.get(ReportSettings, 1)
            return {k: int(v) for k, v in dict(row.topics or {}).items()} if row else {}

    async def messages(self, topic_key: str) -> list[SentMessage]:
        """What reached one topic (keys: sales, payments, wallet, customers, errors, ...)."""
        threads = await self._threads()
        if topic_key not in threads:
            raise AssertionError(f"no topic {topic_key!r}; the group has {sorted(threads)}")
        return self.app.tg.thread(self.chat.id, threads[topic_key])

    async def texts(self, topic_key: str) -> list[str]:
        return [m.text for m in await self.messages(topic_key)]


class App:
    """Everything a scenario drives. Build one with the ``app`` fixture."""

    def __init__(
        self,
        *,
        settings: Settings,
        tg: FakeTelegram,
        bot: Bot,
        dp: Dispatcher,
        panel: FakePanel,
        session_factory: async_sessionmaker[AsyncSession],
        db_path: Path,
        jobs: dict[str, tuple[Any, dict[str, Any]]],
        errors: _ErrorCapture,
    ) -> None:
        self.settings = settings
        self.tg = tg
        self.bot = bot
        self.dp = dp
        self.panel = panel
        self.session_factory = session_factory
        self.db_path = db_path
        self._jobs = jobs
        self._errors = errors
        self._update_id = 1000
        self._callback_id = 0
        self._customers = 0
        self._expect_unhandled = False
        self.network_commits = 0
        for admin_id in settings.admin_telegram_ids:
            tg.add_private_chat(
                admin_id, first_name=f"Admin{admin_id}", username=f"admin{admin_id}"
            )

    # ------------------------------------------------------------------ actors

    def customer(
        self,
        name: str = "customer",
        *,
        first_name: str | None = None,
        username: str | None = None,
        last_name: str | None = None,
        user_id: int | None = None,
    ) -> Actor:
        """A new person who is not an admin. ``first_name`` defaults to ``name``."""
        self._customers += 1
        return Actor(
            self,
            user_id if user_id is not None else 100_000 + self._customers,
            name=name,
            first_name=name if first_name is None else first_name,
            last_name=last_name,
            username=username,
        )

    def admin(self, index: int = 0, *, started: bool = True) -> Actor:
        """An admin from ``ADMIN_TELEGRAM_IDS`` (the first, unless ``index`` says otherwise).

        ``started=True`` means they have used the bot before, so the database knows
        them. ``started=False`` is an admin who never pressed /start.
        """
        admin_id = self.settings.admin_telegram_ids[index]
        return Actor(
            self,
            admin_id,
            name=f"admin{index}",
            first_name=f"Admin{admin_id}",
            username=f"admin{admin_id}",
            known_to_bot=started,
        )

    # ----------------------------------------------------------------- plumbing

    def next_update_id(self) -> int:
        self._update_id += 1
        return self._update_id

    def next_callback_id(self) -> str:
        self._callback_id += 1
        return f"cb{self._callback_id}"

    @staticmethod
    def now_unix() -> int:
        return int(time.time())

    async def feed(self, raw: dict[str, Any]) -> None:
        """Hand one Update to the dispatcher, the way the polling loop does."""
        update = Update.model_validate(raw, context={"bot": self.bot})
        await self.dp.feed_update(self.bot, update)

    # ----------------------------------------------------------------- database

    def session(self) -> AsyncSession:
        """``async with app.session() as s:`` a session on the scenario's database."""
        return self.session_factory()

    async def age(
        self,
        model: Any,
        row_id: int,
        *,
        minutes: int = 0,
        days: int = 0,
        columns: tuple[str, ...] = ("created_at", "updated_at"),
    ) -> None:
        """Move a row back in time, as if it had been made ``minutes``/``days`` ago."""
        delta = timedelta(minutes=minutes, days=days)
        async with self.session() as session:
            row = await session.get(model, row_id)
            if row is None:
                raise AssertionError(f"{model.__name__} {row_id} does not exist")
            for column in columns:
                value = getattr(row, column, None)
                if value is not None:
                    setattr(row, column, value - delta)
            await session.commit()

    async def age_order(self, order_id: int, *, minutes: int = 0, days: int = 0) -> None:
        from database.models import Order

        await self.age(Order, order_id, minutes=minutes, days=days)

    async def one(self, statement: Any) -> Any:
        """Run a select and return its single row (or scalar)."""
        async with self.session() as session:
            result = await session.execute(statement)
            return result.scalars().one()

    async def all(self, statement: Any) -> list[Any]:
        async with self.session() as session:
            result = await session.execute(statement)
            return list(result.scalars().all())

    async def latest_order(self, customer: Actor | None = None) -> Any:
        """The newest order, of one person or of anyone."""
        from database.models import Order, User

        statement = select(Order).order_by(Order.id.desc()).limit(1)
        if customer is not None:
            statement = (
                select(Order)
                .join(User, User.id == Order.user_id)
                .where(User.telegram_id == customer.user_id)
                .order_by(Order.id.desc())
                .limit(1)
            )
        async with self.session() as session:
            result = await session.execute(statement)
            order = result.scalars().first()
        if order is None:
            raise AssertionError("there is no order yet")
        return order

    async def give_wallet(self, customer: Actor, toman: int) -> None:
        """Put money in a customer's wallet (they must have pressed /start already)."""
        from database.repositories.user import get_user_by_telegram_id
        from services.wallet import credit

        async with self.session() as session:
            user = await get_user_by_telegram_id(session, customer.user_id)
            if user is None:
                raise AssertionError(f"{customer.name} has no database row: call start() first")
            await credit(
                session,
                user_id=user.id,
                amount_toman=toman,
                reason="admin_adjust",
                idempotency_key=f"e2e:{customer.user_id}:{self._update_id}:{toman}",
            )
            await session.commit()

    async def wallet_balance(self, customer: Actor) -> int:
        from database.repositories.user import get_user_by_telegram_id

        async with self.session() as session:
            user = await get_user_by_telegram_id(session, customer.user_id)
            return int(user.wallet_balance_toman)

    # --------------------------------------------------------------------- jobs

    async def run_job(self, name: str) -> Any:
        """Run one scheduler job once, with the arguments production registers.

        Names: ``expire``, ``report_flush``, ``broadcast``, ``reminders``,
        ``digest``, ``panel_watch``, ``cleanup``, ``backup``.
        """
        if name not in JOBS:
            raise KeyError(f"unknown job {name!r}; known: {sorted(JOBS)}")
        job_id = JOBS[name]
        if job_id not in self._jobs:
            raise AssertionError(
                f"the scheduler does not register {job_id!r} with these settings; "
                f"it registers {sorted(self._jobs)}"
            )
        func, kwargs = self._jobs[job_id]
        return await func(**kwargs)

    # ------------------------------------------------------------------ groups

    async def connect_report_group(self, chat_id: int = -1001234567890) -> ForumGroup:
        """Make a forum group, add the bot as admin, and run the real one-tap setup."""
        from services.report_group import run_full_setup

        chat = self.tg.add_forum(chat_id)
        async with self.session() as session:
            result = await run_full_setup(session, self.bot, chat_id=chat_id)
            await session.commit()
        assert result.ok, f"report group setup failed: {result.failures}"
        return ForumGroup(self, chat)

    # ------------------------------------------------- commits from elsewhere

    def commit_at_every_network_call(self) -> None:
        """Another connection commits before every Bot API and panel call.

        That is what a scheduler job does to a request in production. A handler
        that reads, awaits the network and then writes on the same snapshot now
        dies with «database is locked» in the scenario, and ``final_checks``
        fails it. ``E2E_COMMIT_AT_NETWORK=1`` turns this on for every scenario.
        """
        self.network_commits = 0
        self.tg.on_network = self._commit_elsewhere
        self.panel.on_network = self._commit_elsewhere

    def _commit_elsewhere(self) -> None:
        connection = sqlite3.connect(self.db_path, timeout=0)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            connection.execute(f"PRAGMA user_version = {int(version) + 1}")
            connection.commit()
            self.network_commits += 1
        except sqlite3.OperationalError:
            # The request holds the write lock: it has already written, so its
            # snapshot cannot go stale and a real job would simply wait.
            pass
        finally:
            connection.close()

    # ------------------------------------------------------------ final checks

    def expect_unhandled_errors(self) -> None:
        """This scenario provokes an exception the error middleware is meant to catch."""
        self._expect_unhandled = True

    @property
    def unhandled_errors(self) -> list[str]:
        return list(self._errors.unhandled)

    def final_checks(self) -> None:
        """Run after the scenario body: the bot must not have broken a rule silently."""
        problems: list[str] = []
        rejected = self.tg.unaccepted()
        if rejected:
            problems.append(
                "Telegram would have rejected what the bot sent (tg.accept(kind) if expected):\n  "
                + "\n  ".join(str(r) for r in rejected)
            )
        violations = self.panel.unexpected_violations()
        if violations:
            problems.append(
                "the bot sent the panel requests the real PasarGuard refuses "
                "(panel.allow_violations() if expected):\n  " + "\n  ".join(violations)
            )
        if self._errors.unhandled and not self._expect_unhandled:
            problems.append(
                "a handler raised and the error middleware swallowed it "
                "(app.expect_unhandled_errors() if expected):\n  "
                + "\n  ".join(self._errors.unhandled)
            )
        if problems:
            raise AssertionError("\n".join(problems))


@contextmanager
def _detached_routers() -> Iterator[None]:
    """Free the module-level routers from any dispatcher, then give them back."""
    from bot.handlers import register_handlers

    found: list[Router] = []

    class _Spy:
        def include_router(self, router: Router) -> None:
            found.append(router)

    register_handlers(_Spy())  # type: ignore[arg-type]
    saved: list[tuple[Router, Router | None, int | None]] = []
    for router in found:
        parent = router._parent_router
        index = None
        if parent is not None:
            index = parent.sub_routers.index(router)
            parent.sub_routers.remove(router)
            router._parent_router = None
        saved.append((router, parent, index))
    try:
        yield
    finally:
        # Reverse order: each index was taken after the earlier ones had been removed.
        for router, parent, index in reversed(saved):
            attached = router._parent_router
            if attached is not None:
                if router in attached.sub_routers:
                    attached.sub_routers.remove(router)
                router._parent_router = None
            if parent is not None and index is not None:
                parent.sub_routers.insert(index, router)
                router._parent_router = parent


@asynccontextmanager
async def running_app(
    template: Path,
    workdir: Path,
    monkeypatch: Any,
    **settings_overrides: Any,
) -> AsyncIterator[App]:
    """Build the bot on a fresh database copy, yield it, and restore global state."""
    import bot.config as bot_config
    import bot.main as bot_main
    import scheduler as scheduler_module
    from bot.keyboards import style

    db_path = workdir / "bot.e2e.db"
    backup_dir = workdir / "backups"
    backup_dir.mkdir(exist_ok=True)
    url = copy_template(template, db_path)
    settings = make_settings(url, backup_dir, **settings_overrides)

    # Settings: the harness values everywhere the bot looks for them.
    monkeypatch.setattr(bot_config, "settings", settings)
    monkeypatch.setattr(bot_config, "get_settings", lambda: settings)
    monkeypatch.setattr(bot_main, "settings", settings)

    saved_db = (db_base._engine, db_base._session_factory)
    saved_colors = style._enabled
    saved_scheduler = scheduler_module._scheduler
    errors = _ErrorCapture()
    error_logger = logging.getLogger("bot.middlewares.errors")
    error_logger.addHandler(errors)
    panel = FakePanel()
    tg = FakeTelegram(bot_username=settings.bot_username)
    install_panel(panel, monkeypatch)
    style.set_enabled(settings.button_colors_enabled)
    db_base.init_db(settings)
    bot: Bot | None = None
    try:
        factory = db_base.get_session_factory()
        bot = create_bot(settings, session=tg)
        recorder = _SchedulerRecorder()
        monkeypatch.setattr(scheduler_module, "AsyncIOScheduler", lambda: recorder)
        scheduler_module._scheduler = None
        scheduler_module.setup_scheduler(bot=bot, session_factory=factory, settings=settings)
        scheduler_module._scheduler = None
        with _detached_routers():
            dp = bot_main.create_dispatcher()
            app = App(
                settings=settings,
                tg=tg,
                bot=bot,
                dp=dp,
                panel=panel,
                session_factory=factory,
                db_path=db_path,
                jobs=recorder.jobs,
                errors=errors,
            )
            if os.environ.get("E2E_COMMIT_AT_NETWORK") == "1":
                app.commit_at_every_network_call()
            yield app
    finally:
        error_logger.removeHandler(errors)
        if bot is not None:
            await bot.session.close()
        await db_base.close_db()
        db_base._engine, db_base._session_factory = saved_db
        style.set_enabled(saved_colors)
        scheduler_module._scheduler = saved_scheduler
        shutil.rmtree(backup_dir, ignore_errors=True)

"""APScheduler setup for Neo VPN bot automation jobs."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.config import Settings
from scheduler.jobs.broadcast_worker import run_broadcast_worker
from scheduler.jobs.cleanup import run_cleanup
from scheduler.jobs.delivery_flush import run_delivery_flush
from scheduler.jobs.expire_orders import run_expire_stale_orders
from scheduler.jobs.marketing import run_marketing_cart, run_marketing_hourly
from scheduler.jobs.nightly_digest import run_nightly_digest
from scheduler.jobs.panel_watch import run_panel_watch
from scheduler.jobs.provision_recovery import run_provision_recovery
from scheduler.jobs.renewal_reminders import run_renewal_reminders
from scheduler.jobs.report_flush import run_report_flush
from scheduler.jobs.service_lifecycle import run_service_lifecycle
from scheduler.jobs.volume_warnings import run_volume_warnings
from services.panel_watch import panel_watch_enabled

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


class _QuietFastWorkerInfo(logging.Filter):
    """Keep frequent idle polls from filling logs; retain warnings and errors."""

    def filter(self, record: logging.LogRecord) -> bool:
        if (
            record.levelno == logging.INFO
            and record.msg
            in ('Running job "%s" (scheduled at %s)', 'Job "%s" executed successfully')
            and isinstance(record.args, tuple)
            and record.args
            and getattr(record.args[0], "id", None) in ("provision_recovery", "delivery_flush")
        ):
            return False
        return True


def get_scheduler() -> AsyncIOScheduler | None:
    return _scheduler


def setup_scheduler(
    *,
    bot: Bot,
    session_factory: async_sessionmaker[Any],
    settings: Settings,
) -> AsyncIOScheduler | None:
    """Create and start scheduler when SCHEDULER_ENABLED. Idempotent."""
    global _scheduler
    if not settings.scheduler_enabled:
        logger.info("Scheduler disabled (SCHEDULER_ENABLED=false)")
        return None
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    executor_log = logging.getLogger("apscheduler.executors.default")
    if not any(isinstance(log_filter, _QuietFastWorkerInfo) for log_filter in executor_log.filters):
        executor_log.addFilter(_QuietFastWorkerInfo())
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_delivery_flush,
        trigger="interval",
        seconds=5,
        id="delivery_flush",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        kwargs={"session_factory": session_factory, "bot": bot, "settings": settings},
    )
    scheduler.add_job(
        run_provision_recovery,
        trigger="interval",
        seconds=5,
        id="provision_recovery",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        kwargs={"session_factory": session_factory, "settings": settings},
    )
    expire_minutes = max(1, int(settings.expire_orders_interval_minutes))
    scheduler.add_job(
        run_expire_stale_orders,
        trigger="interval",
        minutes=expire_minutes,
        id="expire_stale_orders",
        replace_existing=True,
        kwargs={
            "session_factory": session_factory,
            "expiry_minutes": settings.order_expiry_minutes,
            "bot": bot,
            "settings": settings,
        },
    )
    # Reports are delivered on their own interval so a broken group never adds
    # latency to a purchase, and a backlog drains once the group is fixed.
    scheduler.add_job(
        run_report_flush,
        trigger="interval",
        minutes=1,
        id="report_flush",
        replace_existing=True,
        kwargs={"session_factory": session_factory, "bot": bot},
    )
    scheduler.add_job(
        run_broadcast_worker,
        trigger="interval",
        seconds=20,
        id="broadcast_worker",
        replace_existing=True,
        kwargs={"session_factory": session_factory, "bot": bot},
    )
    # CLEANUP_LOGS_DAYS finally has a consumer: the outbox and broadcast tables
    # are append-only by design and would otherwise grow without bound.
    scheduler.add_job(
        run_cleanup,
        trigger="interval",
        hours=24,
        id="cleanup",
        replace_existing=True,
        kwargs={
            "session_factory": session_factory,
            "retention_days": settings.cleanup_logs_days,
        },
    )
    scheduler.add_job(
        run_nightly_digest,
        trigger=CronTrigger(hour=0, minute=5, timezone="Asia/Tehran"),
        id="nightly_digest",
        replace_existing=True,
        kwargs={"session_factory": session_factory},
        max_instances=1,
        coalesce=True,
        # A restart at 00:06 would otherwise skip the night's digest (the default
        # grace is 1 second); an hour of grace runs it late instead of never.
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        run_renewal_reminders,
        trigger="interval",
        hours=6,
        id="renewal_reminders",
        replace_existing=True,
        kwargs={
            "session_factory": session_factory,
            "bot": bot,
            "reminder_days": list(settings.renewal_reminder_days),
            "settings": settings,
        },
    )
    # The volume warning shares the reminders' cadence: it reads the panel once per
    # service, and a warning is once per threshold per period, so a faster pass would
    # only cost panel calls.
    scheduler.add_job(
        run_volume_warnings,
        trigger="interval",
        hours=6,
        id="volume_warnings",
        replace_existing=True,
        kwargs={"session_factory": session_factory, "bot": bot, "settings": settings},
        max_instances=1,
        coalesce=True,
    )

    # Marketing nudges. Both read the owner's switches first and do nothing while every
    # campaign is off. The cart reminder is database only, so it can run often; the hourly
    # pass reads the panel once per winback candidate.
    scheduler.add_job(
        run_marketing_cart,
        trigger="interval",
        minutes=5,
        id="marketing_cart",
        replace_existing=True,
        kwargs={"session_factory": session_factory, "bot": bot, "settings": settings},
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_marketing_hourly,
        trigger="interval",
        hours=1,
        id="marketing_hourly",
        replace_existing=True,
        kwargs={"session_factory": session_factory, "bot": bot, "settings": settings},
        max_instances=1,
        coalesce=True,
    )

    # Warn about, and delete, services that ended long ago. A fixed early hour, like the
    # digest: the customer reads the warning in the morning. A restart in the hour after
    # it runs it late instead of never; two overlapping runs are refused by the job's lock.
    scheduler.add_job(
        run_service_lifecycle,
        trigger=CronTrigger(hour=4, minute=30, timezone="Asia/Tehran"),
        id="service_lifecycle",
        replace_existing=True,
        kwargs={"session_factory": session_factory, "bot": bot, "settings": settings},
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    if panel_watch_enabled(settings):
        # Alerts need two checks in a row, so a dead node is reported after about
        # two intervals. max_instances/coalesce plus the job's own lock keep a slow
        # panel from stacking passes.
        scheduler.add_job(
            run_panel_watch,
            trigger="interval",
            seconds=int(settings.panel_watch_interval_seconds),
            id="panel_watch",
            replace_existing=True,
            kwargs={"session_factory": session_factory, "settings": settings},
            max_instances=1,
            coalesce=True,
        )

    if settings.backup_enabled:
        from scheduler.jobs.backup import run_database_backup

        scheduler.add_job(
            run_database_backup,
            trigger=CronTrigger.from_crontab(settings.backup_cron),
            id="database_backup",
            replace_existing=True,
            kwargs={
                "session_factory": session_factory,
                "bot": bot,
                "settings": settings,
            },
            max_instances=1,
            coalesce=True,
        )
    scheduler.start()
    _scheduler = scheduler
    logger.info("Scheduler started (expire every %sm, renewal every 6h)", expire_minutes)
    return scheduler


async def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
    _scheduler = None

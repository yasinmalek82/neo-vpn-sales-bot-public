"""The durable workers run frequently without overlapping or starting the bot."""

import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import scheduler
from bot.config import Settings


def test_bank_provision_and_delivery_workers_have_short_nonoverlapping_intervals(monkeypatch):
    monkeypatch.setattr(scheduler, "_scheduler", None)
    monkeypatch.setattr(logging.getLogger("apscheduler.executors.default"), "filters", [])
    settings = Settings(_env_file=None, BOT_TOKEN="test-token", SCHEDULER_ENABLED=True)
    with patch.object(AsyncIOScheduler, "start"):
        instance = scheduler.setup_scheduler(
            bot=None, session_factory=None, settings=settings  # type: ignore[arg-type]
        )
    assert instance is not None
    for name in ("provision_recovery", "delivery_flush"):
        job = instance.get_job(name)
        assert job is not None
        assert job.trigger.interval == timedelta(seconds=5)
        assert job.max_instances == 1
        assert job.coalesce is True


def test_fast_worker_success_logs_are_quiet_but_warnings_and_other_jobs_remain(monkeypatch, caplog):
    monkeypatch.setattr(scheduler, "_scheduler", None)
    logger = logging.getLogger("apscheduler.executors.default")
    # In-process migration tests can disable existing loggers via fileConfig.
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(logger, "filters", [])
    settings = Settings(_env_file=None, BOT_TOKEN="test-token", SCHEDULER_ENABLED=True)
    with patch.object(AsyncIOScheduler, "start"):
        scheduler.setup_scheduler(bot=None, session_factory=None, settings=settings)  # type: ignore[arg-type]
    fast = SimpleNamespace(id="provision_recovery")
    delivery = SimpleNamespace(id="delivery_flush")
    other = SimpleNamespace(id="report_flush")

    with caplog.at_level(logging.INFO, logger=logger.name):
        logger.info('Running job "%s" (scheduled at %s)', fast, "now")
        logger.info('Job "%s" executed successfully', delivery)
        logger.info('Job "%s" executed successfully', other)
        logger.info('Operator note for job "%s"', fast)
        logger.warning('Run time of job "%s" was missed by %s', fast, "1 second")
        logger.error('Job "%s" raised an exception', fast)
    messages = [record.getMessage() for record in caplog.records if record.name == logger.name]
    assert len(messages) == 4
    assert "report_flush" in messages[0]
    assert "provision_recovery" in messages[1] and "Operator note" in messages[1]
    assert "provision_recovery" in messages[2] and "missed" in messages[2]
    assert "provision_recovery" in messages[3] and "exception" in messages[3]

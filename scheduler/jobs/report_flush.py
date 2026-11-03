"""Deliver queued reports.

Runs on its own interval rather than at the end of each handler, so a slow or
broken report group never adds latency to a purchase — and a report that
failed while the group was misconfigured is picked up once it is fixed.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from sqlalchemy.ext.asyncio import async_sessionmaker

from services.report_outbox import flush_reports

logger = logging.getLogger(__name__)


async def run_report_flush(session_factory: async_sessionmaker, bot: Bot) -> None:
    async with session_factory() as session:
        try:
            sent, failed = await flush_reports(session, bot)
        except Exception:
            logger.exception("Report flush failed")
            return
    if sent or failed:
        logger.info("Report flush: sent=%s failed=%s", sent, failed)

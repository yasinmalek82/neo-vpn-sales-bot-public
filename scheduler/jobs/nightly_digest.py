"""00:05 Tehran. One summary of the day that just ended.

Keyed on the Jalali date, so a second fire the same night is swallowed by the
outbox instead of posting the digest twice.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from services.nightly_digest import build_nightly_digest
from services.report_outbox import enqueue_report

logger = logging.getLogger(__name__)


async def run_nightly_digest(
    session_factory: async_sessionmaker, *, now: datetime | None = None
) -> None:
    async with session_factory() as session:
        try:
            report = await build_nightly_digest(session, now=now)
            await enqueue_report(
                session,
                event_key=report.event_key,
                topic_key=report.topic_key,
                body=report.body,
                link_user_id=report.link_user_id,
            )
            await session.commit()
        except Exception:
            logger.exception("Nightly digest failed")

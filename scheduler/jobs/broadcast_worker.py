"""Push running broadcasts forward, one batch at a time.

Batched rather than looping to completion so a long campaign never starves the
bot's ordinary traffic, and a cancellation takes effect within seconds.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from sqlalchemy.ext.asyncio import async_sessionmaker

from services.broadcast import run_pending_broadcasts

logger = logging.getLogger(__name__)


async def run_broadcast_worker(session_factory: async_sessionmaker, bot: Bot) -> None:
    async with session_factory() as session:
        try:
            progress = await run_pending_broadcasts(session, bot)
        except Exception:
            logger.exception("Broadcast worker failed")
            return
    if progress is not None:
        logger.info(
            "Broadcast %s: sent=%s blocked=%s failed=%s pending=%s",
            progress.broadcast_id,
            progress.sent,
            progress.blocked,
            progress.failed,
            progress.pending,
        )

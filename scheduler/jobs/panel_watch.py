"""Every PANEL_WATCH_INTERVAL_SECONDS: read the panel's nodes, queue alerts on change.

The job never sends anything. ``check_panel_and_nodes`` enqueues reports in the
same transaction as the state it records, and the report worker delivers them.
Never raises into the scheduler loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from api_client.factory import create_pasarguard_client_from_config
from bot.config import Settings
from services.panel_settings import get_effective_pasarguard_config
from services.panel_watch import check_panel_and_nodes, panel_watch_enabled

logger = logging.getLogger(__name__)

# One pass at a time. A slow panel can make a pass outlast the interval, and two
# overlapping passes would each count «one more check» for the same outage and
# alert a check early.
_watch_lock = asyncio.Lock()


async def run_panel_watch(
    session_factory: async_sessionmaker,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> int | None:
    """One watcher pass. Returns the number of reports queued, or None when skipped."""
    if not panel_watch_enabled(settings):
        logger.info("Panel watch skipped (PANEL_WATCH_INTERVAL_SECONDS=0)")
        return None
    if _watch_lock.locked():
        logger.info("Panel watch already running; skip this fire")
        return None
    async with _watch_lock:
        try:
            async with session_factory() as session:
                cfg = await get_effective_pasarguard_config(session, settings)
                if not cfg.uses_api_key:
                    # No key is a setup gap, not an outage: nothing to watch yet.
                    return None
                client = create_pasarguard_client_from_config(cfg)
                try:
                    result = await check_panel_and_nodes(session, client, now)
                finally:
                    await client.aclose()
                await session.commit()
                return len(result.reports)
        except Exception:
            logger.exception("Panel watch crashed")
            return None

"""Every 6 hours: warn a customer whose volume is running out.

The pass itself lives in ``services/volume_warnings.py``; like the other jobs
this only forwards the arguments production registers. Never raises.
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.config import Settings
from services.volume_warnings import DEFAULT_PAUSE_SECONDS, warn_low_volume


async def run_volume_warnings(
    *,
    session_factory: async_sessionmaker[Any],
    bot: Bot,
    settings: Settings,
    pause_seconds: float = DEFAULT_PAUSE_SECONDS,
) -> int:
    return await warn_low_volume(
        session_factory=session_factory, bot=bot, settings=settings, pause_seconds=pause_seconds
    )

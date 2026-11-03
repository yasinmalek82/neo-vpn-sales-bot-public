"""Once a day (04:30 Tehran): warn about, and delete, services that ended long ago.

All the decisions live in ``services/service_cleanup.py`` and the run in
``services/lifecycle_run.py``; like the other jobs this only forwards the arguments
production registers. With ``delete_mode`` off it reads and writes nothing.
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.config import Settings
from services.lifecycle_run import LifecycleRunResult, run_lifecycle


async def run_service_lifecycle(
    *, session_factory: async_sessionmaker[Any], bot: Bot, settings: Settings
) -> LifecycleRunResult:
    return await run_lifecycle(session_factory=session_factory, bot=bot, settings=settings)

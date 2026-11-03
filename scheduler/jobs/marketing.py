"""Marketing nudges: the cart reminder every 5 minutes, the trial-ended and winback nudges hourly.

The passes live in ``services/marketing_run.py``; like the other jobs this only
forwards the arguments production registers. Never raises.
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.config import Settings
from services.marketing_run import DEFAULT_PAUSE_SECONDS, run_cart_nudges, run_hourly_nudges


async def run_marketing_cart(
    *, session_factory: async_sessionmaker[Any], bot: Bot, settings: Settings
) -> int:
    return await run_cart_nudges(session_factory=session_factory, bot=bot, settings=settings)


async def run_marketing_hourly(
    *,
    session_factory: async_sessionmaker[Any],
    bot: Bot,
    settings: Settings,
    pause_seconds: float = DEFAULT_PAUSE_SECONDS,
) -> int:
    return await run_hourly_nudges(
        session_factory=session_factory, bot=bot, settings=settings, pause_seconds=pause_seconds
    )

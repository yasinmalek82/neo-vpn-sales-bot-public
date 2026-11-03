"""Retry committed delivery intents, independently of customer actions."""

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from services.delivery_outbox import flush_deliveries


async def run_delivery_flush(
    session_factory: async_sessionmaker[AsyncSession], bot: Bot, settings: Settings
) -> None:
    await flush_deliveries(session_factory, bot, settings)

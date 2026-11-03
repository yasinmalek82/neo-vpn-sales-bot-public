"""Resume durable provisioning intents without requiring a customer action."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from services.provision_recovery import recover_pending_provisions


async def run_provision_recovery(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    await recover_pending_provisions(session_factory, settings)

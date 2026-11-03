"""Build PasarGuardClient from application settings or DB-effective config."""

from __future__ import annotations

from typing import TYPE_CHECKING

from api_client.client import PasarGuardClient

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from bot.config import Settings
    from services.panel_settings import EffectivePasarGuardConfig


def create_pasarguard_client(settings: Settings) -> PasarGuardClient:
    """Build client from env/Settings only (no DB). Prefer get_pasarguard_client at runtime."""
    return PasarGuardClient(
        base_url=settings.pasar_guard_base_url,
        api_key=settings.pasar_guard_api_key,
        timeout=settings.pasar_guard_timeout,
        sub_path=settings.pasar_guard_sub_path,
    )


def create_pasarguard_client_from_config(cfg: EffectivePasarGuardConfig) -> PasarGuardClient:
    return PasarGuardClient(
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        timeout=cfg.timeout,
        sub_path=cfg.sub_path,
    )


async def get_pasarguard_client(session: AsyncSession, settings: Settings) -> PasarGuardClient:
    """Resolve DB-over-env config and build a client. Caller must aclose()."""
    from services.panel_settings import get_effective_pasarguard_config

    cfg = await get_effective_pasarguard_config(session, settings)
    return create_pasarguard_client_from_config(cfg)

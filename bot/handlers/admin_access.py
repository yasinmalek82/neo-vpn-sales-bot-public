"""Shared admin access guards for Telegram handlers.

The configured admin IDs remain authoritative through services.access.
"""

from __future__ import annotations

from aiogram.types import CallbackQuery

from bot.config import Settings
from bot.texts import texts
from database.models import User
from services.access import is_admin as access_is_admin


def _is_admin(
    telegram_id: int | None,
    settings: Settings,
    db_user: User | None = None,
) -> bool:
    """Match Phase 4 telegram-id list and menu is_admin (middleware keeps them in sync)."""
    return access_is_admin(telegram_id, settings, db_user)


async def _deny_if_not_admin(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
) -> bool:
    tg_id = callback.from_user.id if callback.from_user else None
    if not _is_admin(tg_id, settings, db_user):
        await callback.answer(texts.admin_action_denied, show_alert=True)
        return True
    return False

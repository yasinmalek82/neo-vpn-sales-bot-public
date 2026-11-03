"""Sale gate guard for PasarGuard panel outages (M1).

Refuses automated sales, renewals, plan switches, custom purchases, order
confirmations, wallet payments, and trial claims while the control panel is
confirmed down.
"""

from __future__ import annotations

import logging
from datetime import datetime

from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.texts import texts
from database.base import end_read_transaction
from services.panel_watch import is_panel_down

logger = logging.getLogger(__name__)


async def refuse_if_panel_down(
    event: Message | CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> bool:
    """If the panel is confirmed down, deliver refusal copy and return True.

    Otherwise return False (fails open). Pass ``callback.message`` where the
    handler has already answered the callback; pass the callback itself only
    where the guard is the handler's first act, and the customer also gets the
    popup.
    """
    if not await is_panel_down(session, settings, now=now):
        return False

    # Nothing is written here; free the read snapshot before waiting on Telegram.
    await end_read_transaction(session)
    if isinstance(event, CallbackQuery):
        try:
            await event.answer(texts.purchase_paused_panel_popup, show_alert=True)
        except Exception:
            logger.debug("Failed to answer callback query with alert popup", exc_info=True)
        if event.message and isinstance(event.message, Message):
            await event.message.answer(texts.purchase_paused_panel)
    elif isinstance(event, Message):
        await event.answer(texts.purchase_paused_panel)

    return True

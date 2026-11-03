"""Centralized error handling middleware."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from bot.config import Settings
from bot.texts import texts
from services.brand import with_support

logger = logging.getLogger(__name__)


class ErrorHandlingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except Exception:
            logger.exception("Unhandled bot error")
            try:
                await self._notify_user(event, data.get("bot"), data.get("settings"))
            except Exception:
                logger.exception("Failed to send error message to user")
            return None

    @staticmethod
    async def _notify_user(
        event: TelegramObject, bot: Bot | None, settings: Settings | None = None
    ) -> None:
        target = event
        if isinstance(event, Update):
            target = event.message or event.callback_query or event
        body = with_support(texts.error_generic, settings) if settings else texts.error_generic
        if isinstance(target, CallbackQuery):
            # A callback alert is a cramped popup; keep it to the bare apology.
            await target.answer(texts.error_generic, show_alert=True)
            return
        if isinstance(target, Message):
            await target.answer(body)
            return
        if bot is not None:
            user = getattr(target, "from_user", None)
            if user is not None:
                await bot.send_message(user.id, body)

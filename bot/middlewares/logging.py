"""Lightweight interaction logging (no secrets)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

logger = logging.getLogger("bot.interactions")


def _unwrap(event: TelegramObject) -> TelegramObject:
    if isinstance(event, Update):
        return event.message or event.callback_query or event
    return event


class InteractionLoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        inner = _unwrap(event)
        user = getattr(inner, "from_user", None)
        uid = getattr(user, "id", None)
        if isinstance(inner, Message):
            kind = "message"
            text = inner.text or inner.caption or ""
            # Never the text: admins type the panel API key and the payment card
            # here, and resellers type customers' phone numbers.
            command = text.split(maxsplit=1)[0].split("@")[0][:32] if text.startswith("/") else "-"
            detail = f"type={inner.content_type} len={len(text)} cmd={command}"
        elif isinstance(inner, CallbackQuery):
            kind = "callback"
            detail = (inner.data or "")[:80]
        else:
            kind = type(inner).__name__
            detail = ""
        logger.info(
            "interaction user=%s kind=%s state=%s detail=%s",
            uid,
            kind,
            data.get("raw_state"),
            detail,
        )
        return await handler(event, data)

"""The bot's customer and admin UI lives in private chats only."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.enums import ChatType
from aiogram.types import TelegramObject


class PrivateChatOnlyMiddleware(BaseMiddleware):
    """Drop every update that comes from a group, supergroup or channel.

    Seen live: added to the report group, the bot answered each service
    message («joined», «topic created» in all 8 report topics) with the
    private-chat fallback «این پیام رو نفهمیدم», and `/start`, `/admin` and the
    bottom-menu labels opened their screens in the group too. In a group the
    bot only *posts* (reports, topic tests); its report buttons are links into
    the private chat, so nothing it sends there needs an update handled.

    One gate on the update itself rather than a filter per router: a new
    router cannot forget it, and no session, user row or log line is spent.
    An update with no chat (an inline-message callback) passes through.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # aiogram's own user-context middleware resolved the chat already.
        chat = data.get("event_chat")
        if chat is not None and chat.type != ChatType.PRIVATE:
            return UNHANDLED
        return await handler(event, data)

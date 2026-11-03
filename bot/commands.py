"""Telegram command menu (the blue “Menu” button).

The bot previously registered no commands at all, so Telegram showed an empty
menu and the admin panel had exactly one entry — an inline button that scrolls
away. Customers get ``/start``; admins additionally get ``/admin`` and
``/cancel`` in their own private chat via a per-chat scope.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
)

logger = logging.getLogger(__name__)

CUSTOMER_COMMANDS: list[BotCommand] = [
    BotCommand(command="start", description="شروع و منوی اصلی"),
    BotCommand(command="cancel", description="لغو عملیات نیمه‌کاره"),
]

ADMIN_COMMANDS: list[BotCommand] = [
    BotCommand(command="start", description="شروع و منوی اصلی"),
    BotCommand(command="admin", description="پنل مدیریت"),
    BotCommand(command="cancel", description="لغو عملیات نیمه‌کاره"),
]


async def setup_bot_commands(bot: Bot, admin_telegram_ids: list[int]) -> None:
    """Publish the command menu. A per-admin failure must not stop startup."""
    await bot.set_my_commands(
        CUSTOMER_COMMANDS,
        scope=BotCommandScopeAllPrivateChats(),
    )
    for admin_id in admin_telegram_ids:
        try:
            await bot.set_my_commands(
                ADMIN_COMMANDS,
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception:
            # An admin who has never opened the bot has no chat yet — expected.
            logger.warning("Could not set admin commands for %s", admin_id, exc_info=True)

"""Register bot middlewares on the dispatcher."""

from __future__ import annotations

from aiogram import Dispatcher

from bot.middlewares.db import DbSessionMiddleware
from bot.middlewares.errors import ErrorHandlingMiddleware
from bot.middlewares.logging import InteractionLoggingMiddleware
from bot.middlewares.private_only import PrivateChatOnlyMiddleware
from bot.middlewares.user_context import UserContextMiddleware


def register_middlewares(dp: Dispatcher) -> None:
    # Outer: errors catch everything
    dp.update.outer_middleware(ErrorHandlingMiddleware())
    # Before the session and user middlewares: a group update costs nothing.
    dp.update.outer_middleware(PrivateChatOnlyMiddleware())
    dp.update.middleware(DbSessionMiddleware())
    dp.update.middleware(UserContextMiddleware())
    dp.update.middleware(InteractionLoggingMiddleware())

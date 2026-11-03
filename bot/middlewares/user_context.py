"""Load DB user into handler data; block banned users."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import CallbackQuery, Message, TelegramObject, Update
from aiogram.types import User as TgUser
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.texts import texts
from database.repositories.user import get_user_by_telegram_id
from services.brand import with_support

logger = logging.getLogger(__name__)


def _unwrap(event: TelegramObject) -> TelegramObject:
    if isinstance(event, Update):
        return (
            event.message
            or event.edited_message
            or event.callback_query
            or event.channel_post
            or event.my_chat_member
            or event
        )
    return event


def _extract_from_user(event: TelegramObject) -> TgUser | None:
    event = _unwrap(event)
    if isinstance(event, Message) and event.from_user:
        return event.from_user
    if isinstance(event, CallbackQuery) and event.from_user:
        return event.from_user
    user = getattr(event, "from_user", None)
    return user if isinstance(user, TgUser) else None


# Writing a timestamp on literally every tap would dirty the session — and so
# force a flush — on read-only screens too. A minute's resolution is plenty for
# «is this customer still around?», and it keeps idle browsing free.
LAST_SEEN_RESOLUTION = timedelta(minutes=1)


def _touch_last_seen(db_user: Any) -> None:
    now = datetime.now(UTC)
    previous = db_user.last_seen_at
    if previous is not None:
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=UTC)
        if now - previous < LAST_SEEN_RESOLUTION:
            return
    db_user.last_seen_at = now


async def _end_own_transaction(session: AsyncSession, db_user: Any, telegram_id: int) -> Any:
    """Commit the middleware's read and its ``last_seen_at`` touch right away.

    Seen live: «database is locked» on ``UPDATE users SET last_seen_at`` a
    second into an ordinary tap. The read above opened a WAL snapshot, the
    touch sat unflushed while the handler talked to Telegram, a scheduler job
    committed, and SQLite refuses at once to turn a stale read snapshot into a
    write (SQLITE_BUSY_SNAPSHOT; ``busy_timeout`` does not apply).

    Committing here writes the touch microseconds after the read and hands the
    handler a transaction that starts at its own first statement. If a commit
    still lands in that gap, only the touch is lost: roll back and re-read
    (a rollback expires the row, and an expired row is a lazy load).
    """
    try:
        await session.commit()
    except OperationalError:
        logger.info("last_seen touch skipped for %s: database busy", telegram_id)
        await session.rollback()
        db_user = await get_user_by_telegram_id(session, telegram_id)
        await session.commit()
    return db_user


class UserContextMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = _extract_from_user(event)
        if tg_user is None:
            return await handler(event, data)

        session: AsyncSession | None = data.get("session")
        settings: Settings = data["settings"]
        if session is None:
            return await handler(event, data)

        db_user = await get_user_by_telegram_id(session, tg_user.id)
        data["db_user"] = db_user

        if db_user is not None and db_user.is_banned:
            inner = _unwrap(event)
            # A banned user's only recourse is a human, so name one.
            body = with_support(texts.banned, settings)
            try:
                if isinstance(inner, CallbackQuery):
                    await inner.answer(body, show_alert=True)
                elif isinstance(inner, Message):
                    await inner.answer(body)
                else:
                    bot: Bot = data["bot"]
                    await bot.send_message(tg_user.id, body)
            except Exception:
                logger.exception("Failed to notify banned user %s", tg_user.id)
            return None

        if db_user is not None:
            should_admin = tg_user.id in settings.admin_telegram_ids
            if db_user.is_admin != should_admin:
                db_user.is_admin = should_admin
            _touch_last_seen(db_user)

        data["db_user"] = await _end_own_transaction(session, db_user, tg_user.id)
        return await handler(event, data)

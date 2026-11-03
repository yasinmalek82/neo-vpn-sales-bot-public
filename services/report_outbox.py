"""Write a report inside the business transaction, deliver it afterwards.

Sending to Telegram from inside a payment transaction couples two systems that
fail independently: a slow group makes a payment slow, and a failed send rolls
back money that was actually taken. So nothing here talks to Telegram while a
transaction is open — ``enqueue`` writes a row, and ``flush`` sends what was
committed. Each accepted send is committed on its own before the batch commit.
A crash in the gap before that per-report commit can still repeat that one
report.

Redaction happens once, here, so no caller can leak a subscription link or an
API key by forgetting to strip it.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.keyboards.admin import report_customer_link_keyboard
from database.models.report import (
    REPORT_SETTINGS_ROW_ID,
    OutboxState,
    ReportOutbox,
    ReportSettings,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_ATTEMPTS",
    "enqueue_report",
    "flush_reports",
    "redact",
]

# After this many failures a report stops retrying. A permanently broken topic
# must not keep a growing queue alive forever.
MAX_ATTEMPTS = 5

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Subscription links identify a customer's service; a report never needs one.
    (re.compile(r"https?://\S+", re.IGNORECASE), "[لینک حذف شد]"),
    (re.compile(r"pg_key_[A-Za-z0-9_\-]+"), "[کلید حذف شد]"),
    (re.compile(r"\b\d{16}\b"), "[شماره کارت حذف شد]"),
    (re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4}\b"), "[شماره کارت حذف شد]"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b"), "[توکن حذف شد]"),
)


def redact(text: str) -> str:
    """Strip links, keys, card numbers and bot tokens from a report body."""
    out = text
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


async def enqueue_report(
    session: AsyncSession,
    *,
    event_key: str,
    topic_key: str,
    body: str,
    link_user_id: int | None = None,
) -> ReportOutbox | None:
    """Queue one report. Safe to call twice for the same ``event_key``.

    Call this **inside** the transaction that makes the change being reported,
    so the report exists if and only if the change does. Returns None when the
    event was already queued.
    """
    existing = await session.scalar(select(ReportOutbox).where(ReportOutbox.event_key == event_key))
    if existing is not None:
        return None

    # ``begin_nested`` flushes pending work unconditionally. Do that before the
    # collision handler so an unrelated caller failure remains its failure.
    await session.flush()

    row = ReportOutbox(
        event_key=event_key,
        topic_key=topic_key,
        body=redact(body),
        link_user_id=int(link_user_id) if link_user_id else None,
        state=OutboxState.pending,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        # Two concurrent writers may pass the read above. Only a row now
        # visible for this event proves that this error was that collision.
        existing = await session.scalar(
            select(ReportOutbox).where(ReportOutbox.event_key == event_key)
        )
        if existing is not None:
            return None
        raise
    return row


async def _persist_report(session: AsyncSession, row: ReportOutbox) -> None:
    """Commit one report on its own connection.

    Telegram has already accepted or refused this report. The caller's batch
    commit can still crash, and that answer must survive the rollback. A crash
    in the gap before this commit can still repeat that one report.

    Once written here the row leaves the caller's session. On a SQLite file the
    caller still holds a read snapshot from before this write, and rewriting
    the same row from it is refused at once as «database is locked».
    """
    if row.state is OutboxState.pending and int(row.attempts) == 0:
        return
    bind = session.bind
    if bind is None:
        return
    maker = async_sessionmaker(bind, expire_on_commit=False)
    try:
        async with maker() as durable:
            stored = await durable.get(ReportOutbox, int(row.id))
            if stored is None:
                return
            stored.state = row.state
            stored.attempts = int(row.attempts)
            stored.last_error = row.last_error
            stored.sent_at = row.sent_at
            await durable.commit()
    except OperationalError as exc:
        # A shared in-memory connection cannot open a second transaction.
        # The caller's own commit still records the outcome there.
        if "within a transaction" not in str(exc):
            raise
        return
    session.expunge(row)


async def flush_reports(session: AsyncSession, bot: Bot, *, limit: int = 20) -> tuple[int, int]:
    """Deliver committed reports. Returns (sent, failed).

    Runs outside any business transaction — from the scheduler, or right after
    a handler commits.
    """
    settings_row = await session.get(ReportSettings, REPORT_SETTINGS_ROW_ID)
    if settings_row is None or not settings_row.enabled or settings_row.chat_id is None:
        return 0, 0

    rows = list(
        (
            await session.execute(
                select(ReportOutbox)
                .where(ReportOutbox.state == OutboxState.pending)
                .order_by(ReportOutbox.id)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0, 0

    topics = dict(settings_row.topics or {})
    sent = failed = 0
    bot_username = ""
    if any(row.link_user_id for row in rows):
        bot_username = await _bot_username(bot)
    for row in rows:
        thread_id = topics.get(row.topic_key)
        markup = _customer_markup(bot_username, row.link_user_id)
        chat_id = int(settings_row.chat_id)
        thread = int(thread_id) if thread_id else None
        try:
            if markup is None:
                await bot.send_message(chat_id=chat_id, message_thread_id=thread, text=row.body)
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=thread,
                    text=row.body,
                    reply_markup=markup,
                )
        except TelegramRetryAfter as exc:
            # A flood-wait is Telegram saying «later», not a delivery failure: the
            # rest of the batch would hit the same wall, and counting it would
            # retire reports for good after five minutes of limiting.
            logger.warning("Report flush paused by Telegram for %ss", exc.retry_after)
            break
        except Exception as exc:  # noqa: BLE001 - every failure mode is retryable here
            row.attempts += 1
            row.last_error = str(exc)[:255]
            if row.attempts >= MAX_ATTEMPTS:
                row.state = OutboxState.failed
                logger.warning("Report %s gave up after %s attempts", row.event_key, row.attempts)
            failed += 1
            await _persist_report(session, row)
            continue
        row.state = OutboxState.sent
        row.sent_at = datetime.now(UTC)
        sent += 1
        await _persist_report(session, row)
    await session.commit()
    return sent, failed


async def _bot_username(bot: Bot) -> str:
    """Best-effort handle for the customer-card link. A missing one drops the button."""
    try:
        me = await bot.get_me()
    except Exception:  # noqa: BLE001 - the report text still has to leave
        return ""
    return str(getattr(me, "username", "") or "").strip().lstrip("@")


def _customer_markup(bot_username: str, user_id: int | None) -> InlineKeyboardMarkup | None:
    if not user_id or not bot_username:
        return None
    return report_customer_link_keyboard(int(user_id), bot_username=bot_username)

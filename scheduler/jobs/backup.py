"""Nightly database dump → gzip → Telegram backup topic.

Off-site copy of the wallet/orders DB. Local file is kept on send failure;
oversized dumps stay local and notify the owner. Never raises into the
scheduler loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.config import Settings
from bot.texts import texts
from database.models.report import TOPIC_BACKUP, TOPIC_ERRORS
from services.db_backup import (
    TELEGRAM_MAX_DOCUMENT_BYTES,
    BackupError,
    build_caption,
    count_core_rows,
    create_gzipped_dump,
    format_size_fa,
    prune_backup_dir,
    verify_gzip_readable,
)
from services.report_group import get_or_create_settings

logger = logging.getLogger(__name__)

# One dump at a time. A second cron fire while the first is mid-gzip must not
# start another dump of the same database.
_backup_lock = asyncio.Lock()
_backup_running = False


def backup_is_running() -> bool:
    return _backup_running


async def run_database_backup(
    session_factory: async_sessionmaker,
    bot: Bot,
    settings: Settings,
    *,
    max_document_bytes: int = TELEGRAM_MAX_DOCUMENT_BYTES,
) -> Path | None:
    """Dump, optionally send, prune. Returns the dump path (kept on disk)."""
    global _backup_running
    if not settings.backup_enabled:
        logger.info("Backup skipped (BACKUP_ENABLED=false)")
        return None
    if _backup_running:
        logger.info("Backup already running; skip this fire")
        return None
    if _backup_lock.locked():
        logger.info("Backup already running; skip this fire")
        return None

    async with _backup_lock:
        _backup_running = True
        try:
            return await _run_backup(
                session_factory, bot, settings, max_document_bytes=max_document_bytes
            )
        except Exception:
            logger.exception("Backup job crashed")
            try:
                await _notify_errors(
                    session_factory,
                    bot,
                    texts.admin_backup_failed.format(reason="خطای داخلی بکاپ"),
                )
            except Exception:
                logger.exception("Failed to report backup crash to errors topic")
            return None
        finally:
            _backup_running = False


async def _run_backup(
    session_factory: async_sessionmaker,
    bot: Bot,
    settings: Settings,
    *,
    max_document_bytes: int,
) -> Path | None:
    backup_dir = Path(settings.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"neo_bot_{stamp}.gz"

    try:
        # Blocking sqlite/gzip/pg_dump work: off the event loop, or every bot
        # update waits for the whole dump.
        await asyncio.to_thread(create_gzipped_dump, settings.database_url, dest)
        users, orders, ledger = await asyncio.to_thread(count_core_rows, settings.database_url)
    except BackupError as exc:
        logger.exception("Backup dump failed")
        await _notify_errors(
            session_factory, bot, texts.admin_backup_failed.format(reason=str(exc))
        )
        return None

    try:
        await asyncio.to_thread(verify_gzip_readable, dest)
    except BackupError as exc:
        # Never ship (or keep) an archive that cannot be restored.
        logger.exception("Backup %s failed its integrity check", dest)
        dest.unlink(missing_ok=True)
        await _notify_errors(
            session_factory, bot, texts.admin_backup_failed.format(reason=str(exc))
        )
        return None

    size = dest.stat().st_size
    caption = build_caption(
        when=datetime.now(UTC),
        size_bytes=size,
        users=users,
        orders=orders,
        ledger=ledger,
    )

    if size > max_document_bytes:
        # Keep local dump — degraded but better than nothing.
        msg = texts.admin_backup_too_large.format(size=format_size_fa(size), path=str(dest))
        await _send_backup_text(session_factory, bot, msg, topic=TOPIC_BACKUP)
        prune_backup_dir(backup_dir, settings.backup_retain_daily)
        return dest

    sent_ok = await _send_backup_document(session_factory, bot, dest, caption=caption)
    if not sent_ok:
        # Local dump stays. Tell errors topic.
        await _notify_errors(
            session_factory,
            bot,
            texts.admin_backup_failed.format(reason="ارسال فایل به تلگرام شکست خورد"),
        )
    prune_backup_dir(backup_dir, settings.backup_retain_daily)
    return dest


async def _send_backup_document(
    session_factory: async_sessionmaker,
    bot: Bot,
    path: Path,
    *,
    caption: str,
) -> bool:
    async with session_factory() as session:
        row = await get_or_create_settings(session)
        await session.commit()
    if row.chat_id is None or not row.enabled:
        logger.warning("Backup dump ready but report group is not enabled")
        return False
    topics = dict(row.topics or {})
    thread_id = topics.get(TOPIC_BACKUP)
    try:
        await bot.send_document(
            chat_id=int(row.chat_id),
            document=FSInputFile(path),
            caption=caption,
            message_thread_id=int(thread_id) if thread_id is not None else None,
        )
    except Exception:
        logger.exception("Backup send_document failed; keeping local dump at %s", path)
        return False
    return True


async def _send_backup_text(
    session_factory: async_sessionmaker,
    bot: Bot,
    text: str,
    *,
    topic: str,
) -> None:
    async with session_factory() as session:
        row = await get_or_create_settings(session)
        await session.commit()
    if row.chat_id is None:
        return
    topics = dict(row.topics or {})
    thread_id = topics.get(topic)
    try:
        await bot.send_message(
            chat_id=int(row.chat_id),
            text=text,
            message_thread_id=int(thread_id) if thread_id is not None else None,
        )
    except Exception:
        logger.exception("Backup text send to %s failed", topic)


async def _notify_errors(session_factory: async_sessionmaker, bot: Bot, text: str) -> None:
    await _send_backup_text(session_factory, bot, text, topic=TOPIC_ERRORS)

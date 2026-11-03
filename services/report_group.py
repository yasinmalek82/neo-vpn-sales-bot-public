"""Connecting the bot to a report group, and proving it actually works.

The setup is deliberately evidence-based: a topic is «آماده» only after a real
message has been delivered to it. «تنظیم شد» is not proof — a wrong thread id,
a bot without permission, or a group with forum mode off all look fine in the
database and fail silently at the moment a real report matters.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from database.models.report import (
    REPORT_SETTINGS_ROW_ID,
    TOPIC_ICON_COLORS,
    TOPIC_KEYS,
    TOPIC_TITLES_FA,
    OutboxState,
    ReportOutbox,
    ReportSettings,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ReportGroupError",
    "SetupResult",
    "reconcile_topics",
    "requeue_failed_reports",
    "run_full_setup",
    "TopicStatus",
    "GroupReadiness",
    "attach_group",
    "get_or_create_settings",
    "get_readiness",
    "set_enabled",
    "set_topic",
    "verify_topic",
]


class ReportGroupError(Exception):
    """A setup step that cannot proceed, in Persian."""


# One-tap setup makes up to 16 calls on one group. Sent back to back, the last
# test messages hit «Flood control exceeded … Retry after N» and the group read
# «نیمه‌آماده» for no fault of the owner's.
SEND_GAP_SECONDS = 1.0
# The most one setup will sleep on Telegram's «retry after», all topics
# together. A longer wait is reported instead: the owner is watching a screen.
FLOOD_WAIT_BUDGET_SECONDS = 60

REASON_FLOOD_FA = "تلگرام ارسال پشت‌سرهم را محدود کرد. یک دقیقهٔ دیگر «🧪 تست» همین تاپیک را بزن."
REASON_NO_RIGHTS_FA = (
    "ربات اجازهٔ ارسال یا مدیریت تاپیک در این گروه ندارد. ربات را ادمین کن و دوباره تلاش کن."
)
REASON_TOPIC_GONE_FA = (
    "این تاپیک پیدا نشد یا بسته است. «🔗 وصل کردن گروه» را دوباره بزن تا از نو ساخته شود."
)
REASON_UNKNOWN_FA = "علت نامشخص است. کمی بعد دوباره تلاش کن."

_NO_RIGHTS_MARKS = ("not enough rights", "have no rights", "write_forbidden", "admin_required")
_TOPIC_GONE_MARKS = ("thread not found", "topic_id_invalid", "topic_deleted", "topic_closed")
# What `editForumTopic` answers, read off the live Bot API on 2026-10-03.
_TOPIC_UNCHANGED_MARK = "topic_not_modified"  # a rename to the name it already has
_TOPIC_DELETED_MARKS = ("topic_id_invalid", "thread not found", "topic_deleted")


def failure_reason_fa(exc: BaseException) -> str:
    """Why a Telegram call failed, for the admin screen. Never the raw text.

    The screen used to print ``str(exc)`` cut at 120 characters: English, and
    ending mid-word.
    """
    if isinstance(exc, TelegramRetryAfter):
        return REASON_FLOOD_FA
    detail = str(exc).lower()
    if isinstance(exc, TelegramForbiddenError) or any(m in detail for m in _NO_RIGHTS_MARKS):
        return REASON_NO_RIGHTS_FA
    if any(m in detail for m in _TOPIC_GONE_MARKS):
        return REASON_TOPIC_GONE_FA
    return REASON_UNKNOWN_FA


class _FloodPacer:
    """Honours ``TelegramRetryAfter`` once per call, within one shared budget."""

    def __init__(self, budget_seconds: int = FLOOD_WAIT_BUDGET_SECONDS) -> None:
        self._budget = budget_seconds

    async def call(self, make_call: Callable[[], Awaitable[Any]]) -> Any:
        try:
            return await make_call()
        except TelegramRetryAfter as exc:
            wait = int(exc.retry_after) + 1
            if wait > self._budget:
                raise
            self._budget -= wait
            logger.info("Report group setup: flood wait %ss, then one retry", wait)
            await asyncio.sleep(wait)
            return await make_call()

    @staticmethod
    async def gap() -> None:
        await asyncio.sleep(SEND_GAP_SECONDS)


@dataclass(frozen=True)
class TopicStatus:
    key: str
    title_fa: str
    thread_id: int | None
    verified_at: datetime | None

    @property
    def configured(self) -> bool:
        return self.thread_id is not None

    @property
    def ready(self) -> bool:
        """Configured *and* proven by a real delivery."""
        return self.configured and self.verified_at is not None


@dataclass(frozen=True)
class GroupReadiness:
    chat_id: int | None
    chat_title: str | None
    enabled: bool
    topics: list[TopicStatus]

    @property
    def attached(self) -> bool:
        return self.chat_id is not None

    @property
    def all_ready(self) -> bool:
        return self.attached and all(t.ready for t in self.topics)

    @property
    def ready_count(self) -> int:
        return sum(1 for t in self.topics if t.ready)


async def get_or_create_settings(session: AsyncSession) -> ReportSettings:
    row = await session.get(ReportSettings, REPORT_SETTINGS_ROW_ID)
    if row is None:
        row = ReportSettings(id=REPORT_SETTINGS_ROW_ID, topics={}, verified={}, enabled=False)
        session.add(row)
        await session.flush()
    return row


async def _fresh_settings(session: AsyncSession) -> ReportSettings:
    """The row as the database has it now, not as this session last saw it."""
    row = await get_or_create_settings(session)
    await session.refresh(row)
    return row


async def _save_settings(session: AsyncSession, change: Callable[[ReportSettings], None]) -> None:
    """Apply ``change`` to the settings row in a short transaction of its own.

    Seen live: «database is locked» on ``UPDATE report_settings`` 19 seconds
    into «🔗 وصل کردن گروه». The setup had read the row (a WAL read snapshot),
    then talked to Telegram; a scheduler job committed meanwhile, and SQLite
    refuses at once to turn a stale read snapshot into a write
    (SQLITE_BUSY_SNAPSHOT; ``busy_timeout`` does not apply). Eight topics the
    bot had just created were never saved, and the retry made eight more.

    So every caller here ends its transaction (``session.commit()``) before
    its first Telegram call, and writes through this: re-read, change, commit,
    with nothing awaited on the network in between. If a commit still lands in
    that gap, the re-read is repeated once; ``change`` touches only its own
    keys, so running it again on a fresh row is safe.
    """
    try:
        change(await _fresh_settings(session))
        await session.commit()
    except OperationalError:
        logger.info("report_settings write raced a commit; re-reading once")
        await session.rollback()
        change(await _fresh_settings(session))
        await session.commit()


async def get_readiness(session: AsyncSession, *, create: bool = True) -> GroupReadiness:
    """The group and each topic's state. ``create=False`` is for a page that only
    looks (the Mini App): with no settings row it reads «nothing attached» and
    writes nothing, where the bot's screen creates the row and commits."""
    row = (
        await get_or_create_settings(session)
        if create
        else await session.get(ReportSettings, REPORT_SETTINGS_ROW_ID)
    )
    topics = dict(row.topics or {}) if row is not None else {}
    verified = dict(row.verified or {}) if row is not None else {}
    statuses = []
    for key in TOPIC_KEYS:
        raw = verified.get(key)
        stamp: datetime | None = None
        if raw:
            try:
                stamp = datetime.fromisoformat(str(raw))
            except ValueError:
                stamp = None
        statuses.append(
            TopicStatus(
                key=key,
                title_fa=TOPIC_TITLES_FA[key],
                thread_id=int(topics[key]) if topics.get(key) else None,
                verified_at=stamp,
            )
        )
    return GroupReadiness(
        chat_id=int(row.chat_id) if row is not None and row.chat_id else None,
        chat_title=row.chat_title if row is not None else None,
        enabled=bool(row.enabled) if row is not None else False,
        topics=statuses,
    )


def supergroup_id_candidates(chat_id: int) -> list[int]:
    """The id as sent, then its ``-100…`` form.

    Telegram shows one group under several ids: Bot API wants
    ``-1004336742277``, while Telegram Web's address bar and some bots show
    ``-4336742277`` or the bare ``4336742277``. The owner pasted a short form,
    ``get_chat`` said «chat not found», and the bot reported «no access» for a
    group where it was an admin with every right.
    """
    candidates = [chat_id]
    digits = str(abs(chat_id))
    if not digits.startswith("100"):
        candidates.append(int(f"-100{digits}"))
    return candidates


async def attach_group(
    session: AsyncSession,
    bot: Bot,
    *,
    chat_id: int,
) -> GroupReadiness:
    """Bind a group after checking the three things that actually break.

    Checked live rather than trusted: the bot can see the chat, the chat is a
    supergroup with forum topics on, and the bot is an administrator. Each
    failure names its own fix.
    """
    await session.commit()  # no transaction is held open across the Telegram calls
    chat = None
    last_exc: Exception | None = None
    for candidate in supergroup_id_candidates(chat_id):
        try:
            chat = await bot.get_chat(candidate)
        except Exception as exc:  # noqa: BLE001 - surfaced as Persian guidance
            logger.warning("get_chat(%s) failed: %s", candidate, exc)
            last_exc = exc
            continue
        chat_id = candidate
        break
    if chat is None:
        raise ReportGroupError(
            "این گروه پیدا نشد. یا ربات عضو گروه نیست، یا شناسه کامل نیست.\n"
            "شناسهٔ سوپرگروه با -100 شروع می‌شه (مثل -1001234567890)."
        ) from last_exc

    if getattr(chat, "type", None) not in {"supergroup", "group"}:
        raise ReportGroupError("این یک گروه نیست. شناسهٔ یک گروه (نه کانال یا چت خصوصی) بفرست.")

    if not getattr(chat, "is_forum", False):
        raise ReportGroupError(
            "قابلیت Topics این گروه خاموش است.\n"
            "تنظیمات گروه ← Topics را روشن کن، بعد دوباره تلاش کن."
        )

    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
    except Exception as exc:  # noqa: BLE001
        raise ReportGroupError("وضعیت ربات در گروه خوانده نشد. دوباره تلاش کن.") from exc

    status = getattr(member, "status", None)
    if status not in {"administrator", "creator"}:
        raise ReportGroupError(
            "ربات در این گروه ادمین نیست.\n"
            "ربات را ادمین کن (با دسترسی «مدیریت Topics»)، بعد دوباره تلاش کن."
        )
    # Creating topics needs this specific right; the creator implicitly has it.
    if status == "administrator" and not getattr(member, "can_manage_topics", False):
        raise ReportGroupError(
            "ربات دسترسی «مدیریت Topics» ندارد.\n"
            "در تنظیمات ادمین گروه، گزینهٔ Manage Topics را برای ربات روشن کن."
        )

    def bind(row: ReportSettings) -> None:
        if row.chat_id != chat_id:
            # A different group means the old thread ids mean nothing.
            row.topics = {}
            row.verified = {}
        row.chat_id = chat_id
        row.chat_title = getattr(chat, "title", None)

    await _save_settings(session, bind)
    return await get_readiness(session)


async def set_topic(session: AsyncSession, *, topic_key: str, thread_id: int) -> GroupReadiness:
    """Point one report kind at a topic. Changing it clears its proof."""
    if topic_key not in TOPIC_KEYS:
        raise ReportGroupError("این نوع گزارش تعریف نشده است.")
    row = await get_or_create_settings(session)
    if row.chat_id is None:
        raise ReportGroupError("اول گروه گزارش را وصل کن.")
    topics = dict(row.topics or {})
    verified = dict(row.verified or {})
    if topics.get(topic_key) != thread_id:
        verified.pop(topic_key, None)
    topics[topic_key] = int(thread_id)
    row.topics = topics
    row.verified = verified
    await session.flush()
    return await get_readiness(session)


async def verify_topic(
    session: AsyncSession,
    bot: Bot,
    *,
    topic_key: str,
    pacer: _FloodPacer | None = None,
) -> GroupReadiness:
    """Send a real message to the topic and record whether it arrived.

    Only a delivery proves a route. On failure the topic stays unverified and
    the reason is handed back in Persian (``failure_reason_fa``).

    Without a ``pacer`` a flood limit fails at once: the single «🧪 تست» button
    is a tap waiting for its answer, so it never sleeps.
    """
    row = await get_or_create_settings(session)
    if row.chat_id is None:
        raise ReportGroupError("اول گروه گزارش را وصل کن.")
    thread_id = dict(row.topics or {}).get(topic_key)
    if not thread_id:
        raise ReportGroupError("برای این نوع گزارش هنوز تاپیکی تعیین نشده است.")

    title = TOPIC_TITLES_FA.get(topic_key, topic_key)
    chat_id = int(row.chat_id)
    await session.commit()  # no transaction is held open across the Telegram call

    def record(row: ReportSettings, *, proven: bool) -> None:
        verified = dict(row.verified or {})
        if proven:
            verified[topic_key] = datetime.now(UTC).isoformat()
        else:
            verified.pop(topic_key, None)
        row.verified = verified

    async def send_test() -> Any:
        return await bot.send_message(
            chat_id=chat_id,
            message_thread_id=int(thread_id),
            text=f"✅ تست مسیر گزارش — {title}\nاین تاپیک آمادهٔ دریافت گزارش است.",
        )

    try:
        if pacer is None:
            await send_test()
        else:
            await pacer.call(send_test)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Report topic test failed (%s): %s", topic_key, exc)
        await _save_settings(session, partial(record, proven=False))
        raise ReportGroupError(f"ارسال تست ناموفق بود. {failure_reason_fa(exc)}") from exc

    await _save_settings(session, partial(record, proven=True))
    return await get_readiness(session)


async def set_enabled(session: AsyncSession, *, enabled: bool) -> GroupReadiness:
    """Turn reporting on. Refused until at least one topic is proven, so the
    owner cannot switch it on and believe reports are flowing when none are."""
    row = await get_or_create_settings(session)
    if enabled:
        readiness = await get_readiness(session)
        if not readiness.attached:
            raise ReportGroupError("اول گروه گزارش را وصل کن.")
        if readiness.ready_count == 0:
            raise ReportGroupError(
                "هیچ تاپیکی تست نشده است. حداقل یک تاپیک را تست کن تا مطمئن شویم گزارش می‌رسد."
            )
    row.enabled = bool(enabled)
    await session.flush()
    return await get_readiness(session)


@dataclass(frozen=True)
class SetupResult:
    """What one-tap setup managed to do, so the UI can report it honestly."""

    readiness: GroupReadiness
    created: list[str]
    reused: list[str]
    verified: list[str]
    failures: list[tuple[str, str]]
    # Reports that had given up while the route was broken, put back in the queue.
    requeued: int = 0

    @property
    def ok(self) -> bool:
        return not self.failures and bool(self.verified)


async def reconcile_topics(
    session: AsyncSession, bot: Bot, *, pacer: _FloodPacer | None = None
) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """Bring the group's topics in line with ours, without ever duplicating.

    For every route, in order:

    1. A thread we already recorded → ``editForumTopic`` forces our canonical
       title onto it. That is how a topic someone renamed comes back, and it
       also proves the thread still exists. Telegram answers
       ``TOPIC_NOT_MODIFIED`` when the title was already ours: that is the
       same proof, not a failure.
    2. Telegram saying the thread is gone (``TOPIC_ID_INVALID``) → the stale id
       is dropped and a fresh topic created. Any other failure is reported and
       the saved thread kept: recreating on a timeout would duplicate it.
    3. No recorded thread → create one.

    So re-running setup on the same group renames rather than duplicates, and a
    topic deleted by hand is rebuilt.

    **The one thing that cannot be done:** the Bot API has no method to list a
    group's topics (there is no ``getForumTopics``), so a topic this bot did not
    create is invisible to it — it cannot be found by title, adopted or removed.
    Point the bot at a fresh group, or delete stray topics yourself.

    Returns (created, adopted, failures).
    """
    row = await get_or_create_settings(session)
    if row.chat_id is None:
        raise ReportGroupError("اول گروه گزارش را وصل کن.")

    chat_id = int(row.chat_id)
    topics = dict(row.topics or {})
    await session.commit()  # no transaction is held open across the Telegram calls
    created: list[str] = []
    adopted: list[str] = []
    failures: list[tuple[str, str]] = []
    pacer = pacer or _FloodPacer()

    def point(row: ReportSettings, *, key: str, thread_id: int | None) -> None:
        """Record one route's new thread (or that it has none); its proof goes."""
        saved = dict(row.topics or {})
        verified = dict(row.verified or {})
        if thread_id is None:
            saved.pop(key, None)
        else:
            saved[key] = thread_id
        verified.pop(key, None)
        row.topics = saved
        row.verified = verified

    for key in TOPIC_KEYS:
        title = TOPIC_TITLES_FA[key]
        thread_id = topics.get(key)

        if thread_id:
            try:
                await pacer.call(
                    partial(
                        bot.edit_forum_topic,
                        chat_id=chat_id,
                        message_thread_id=int(thread_id),
                        name=title,
                    )
                )
            except TelegramRetryAfter as exc:
                # Throttled is not deleted: recreating here would duplicate the topic.
                failures.append((key, failure_reason_fa(exc)))
                continue
            except Exception as exc:  # noqa: BLE001 - sorted by what Telegram said
                detail = str(exc).lower()
                if _TOPIC_UNCHANGED_MARK in detail:
                    # Telegram refuses a rename to the name the topic already
                    # has. The thread exists, which is all the edit was for.
                    adopted.append(key)
                    continue
                if not any(mark in detail for mark in _TOPIC_DELETED_MARKS):
                    # A timeout or a server error is not proof the topic is
                    # gone: recreating on it would duplicate the topic.
                    logger.warning("Topic %s (%s) could not be checked: %s", key, thread_id, exc)
                    failures.append((key, failure_reason_fa(exc)))
                    continue
                logger.info("Topic %s (%s) was deleted in Telegram; recreating", key, thread_id)
            else:
                adopted.append(key)
                continue

        try:
            topic = await pacer.call(
                partial(
                    bot.create_forum_topic,
                    chat_id=chat_id,
                    name=title,
                    icon_color=TOPIC_ICON_COLORS[key],
                )
            )
        except Exception as exc:  # noqa: BLE001 - reported per topic, not fatal
            logger.warning("Report topic %s could not be created: %s", key, exc)
            failures.append((key, failure_reason_fa(exc)))
            if thread_id:
                await _save_settings(session, partial(point, key=key, thread_id=None))
            continue
        # Saved now, not after the loop: a topic Telegram has made and we have
        # not recorded is an orphan, and the next setup would make it again.
        await _save_settings(
            session, partial(point, key=key, thread_id=int(topic.message_thread_id))
        )
        created.append(key)

    return created, adopted, failures


REQUEUE_WINDOW_DAYS = 7


async def requeue_failed_reports(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Put reports that gave up during an outage back in the queue.

    A broken route (wrong thread, bot removed) burns a report's five attempts
    and retires it as ``failed``; fixing the route would otherwise leave those
    events lost. Only the last week, and attempts start again from zero.
    """
    since = (now or datetime.now(UTC)) - timedelta(days=REQUEUE_WINDOW_DAYS)
    result = await session.execute(
        update(ReportOutbox)
        .where(ReportOutbox.state == OutboxState.failed, ReportOutbox.created_at >= since)
        .values(state=OutboxState.pending, attempts=0, last_error=None)
    )
    await session.flush()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def run_full_setup(session: AsyncSession, bot: Bot, *, chat_id: int) -> SetupResult:
    """Attach, create every topic, prove each one, and switch reporting on.

    One tap instead of eleven. Each step still records its own outcome, so a
    group where only four of five topics could be created reports exactly that
    rather than claiming success.
    """
    await attach_group(session, bot, chat_id=chat_id)
    pacer = _FloodPacer()
    created, reused, failures = await reconcile_topics(session, bot, pacer=pacer)

    verified: list[str] = []
    for index, key in enumerate(created + reused):
        if index:
            await pacer.gap()
        try:
            await verify_topic(session, bot, topic_key=key, pacer=pacer)
        except ReportGroupError as exc:
            failures.append((key, str(exc)))
            continue
        verified.append(key)

    if verified:
        await set_enabled(session, enabled=True)

    requeued = 0
    if verified and not failures:
        requeued = await requeue_failed_reports(session)

    return SetupResult(
        requeued=requeued,
        readiness=await get_readiness(session),
        created=created,
        reused=reused,
        verified=verified,
        failures=failures,
    )

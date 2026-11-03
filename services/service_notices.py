"""Claim a lifecycle message once, then send it (never the other way round).

Every message the lifecycle jobs send to a customer (volume warning, warning
before deletion, «your service was deleted») has an ``event_key`` that names
the transition and the period, and a row in ``service_notices``:

* ``queue_notice`` writes the row, unsent (``sent_at`` NULL), inside a business
  transaction. The deletion queues its customer notice this way.
* ``claim_notice`` marks it sent with one conditional UPDATE and returns True
  only for the caller that changed the row, so a retry or a second run cannot
  claim it again. The caller **commits the claim before the Telegram send**.
* ``release_notice`` puts it back to unsent when the send failed in a way a
  later run may retry.

Claim-then-send means a crash between the two loses a notice. A notice that goes
out twice is the worse mistake for a customer, so that is the side it errs on
(same shape as ``mark_reminder_sent``'s idempotency, one step stricter).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.engine import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.base import end_read_transaction
from database.models.lifecycle import ServiceNotice
from database.models.report import TOPIC_ERRORS
from services.report_outbox import enqueue_report
from services.user_offers import OfferSpec, offer_line, void_offer, write_offer

logger = logging.getLogger(__name__)


async def queue_notice(
    session: AsyncSession, *, event_key: str, kind: str, account_id: int | None, user_id: int
) -> int | None:
    """Add an unsent notice. Returns its id, or None when the key already exists."""
    existing = await session.scalar(
        select(ServiceNotice.id).where(ServiceNotice.event_key == event_key)
    )
    if existing is not None:
        return None
    row = ServiceNotice(event_key=event_key, kind=kind, account_id=account_id, user_id=user_id)
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        return None
    return int(row.id)


async def _known_state(
    session: AsyncSession, event_key: str
) -> Row[tuple[datetime | None, datetime | None, bool]] | None:
    """``(sent_at, delivered_at, undelivered)`` of the notice, or None when there is none."""
    return (
        await session.execute(
            select(
                ServiceNotice.sent_at, ServiceNotice.delivered_at, ServiceNotice.undelivered
            ).where(ServiceNotice.event_key == event_key)
        )
    ).first()


async def claim_notice(
    session: AsyncSession,
    *,
    event_key: str,
    kind: str,
    account_id: int | None,
    user_id: int,
    now: datetime | None = None,
    offer: OfferSpec | None = None,
) -> bool:
    """Queue the notice if needed and claim it. True only for the caller that got it.

    A notice already claimed answers False from a read alone: a service that stays
    low for weeks is looked at every run, and an idle run writes nothing.

    With ``offer``, the personal offer the message carries is written in this same
    session, keyed by ``event_key``, only for the caller that got the claim: the
    caller's commit makes both durable together.
    """
    moment = now or datetime.now(UTC)
    known = await _known_state(session, event_key)
    if known is not None and known[0] is not None and not _reclaimable(known, moment):
        return False
    if known is None:
        await queue_notice(
            session, event_key=event_key, kind=kind, account_id=account_id, user_id=user_id
        )
    claimed = await claim_queued(session, event_key=event_key, now=now)
    if claimed and offer is not None:
        await write_offer(session, user_id=user_id, event_key=event_key, spec=offer, now=moment)
    return claimed


async def is_claimed(session: AsyncSession, *, event_key: str, now: datetime | None = None) -> bool:
    """True when ``claim_notice`` would answer False from a read alone."""
    moment = now or datetime.now(UTC)
    known = await _known_state(session, event_key)
    return known is not None and known[0] is not None and not _reclaimable(known, moment)


# A claim with no delivery mark older than this is a crash between claim and send.
STALE_CLAIM = timedelta(minutes=10)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _reclaimable(known: Any, now: datetime) -> bool:
    """Claimed, never delivered, not refused for good, and long enough ago: send it again."""
    sent_at, delivered_at, undelivered = known
    return (
        sent_at is not None
        and delivered_at is None
        and not undelivered
        and now - _aware(sent_at) > STALE_CLAIM
    )


async def claim_queued(
    session: AsyncSession, *, event_key: str, now: datetime | None = None
) -> bool:
    moment = now or datetime.now(UTC)
    result = await session.execute(
        update(ServiceNotice)
        .where(
            ServiceNotice.event_key == event_key,
            or_(
                ServiceNotice.sent_at.is_(None),
                and_(
                    ServiceNotice.delivered_at.is_(None),
                    ServiceNotice.undelivered.is_(False),
                    ServiceNotice.sent_at < moment - STALE_CLAIM,
                ),
            ),
        )
        .values(sent_at=moment)
    )
    return int(result.rowcount or 0) == 1  # type: ignore[attr-defined]


async def release_notice(session: AsyncSession, *, event_key: str) -> None:
    """The send failed and a later run may retry it."""
    await session.execute(
        update(ServiceNotice).where(ServiceNotice.event_key == event_key).values(sent_at=None)
    )


async def forget_notices(session: AsyncSession, *, account_id: int, kind: str) -> int:
    """Re-arm: drop one account's markers of a kind. Writes only when there are some."""
    present = await session.scalar(
        select(ServiceNotice.id)
        .where(ServiceNotice.account_id == account_id, ServiceNotice.kind == kind)
        .limit(1)
    )
    if present is None:
        return 0
    result = await session.execute(
        delete(ServiceNotice).where(
            ServiceNotice.account_id == account_id, ServiceNotice.kind == kind
        )
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def mark_delivered(
    session: AsyncSession, *, event_key: str, refused_for_good: bool = False
) -> None:
    """The message reached Telegram, or the RECIPIENT refused it for good (blocked)."""
    await session.execute(
        update(ServiceNotice)
        .where(ServiceNotice.event_key == event_key)
        .values(delivered_at=datetime.now(UTC), undelivered=refused_for_good)
    )


async def send_notice_once(
    session_factory: async_sessionmaker[Any],
    bot: Bot,
    *,
    event_key: str,
    kind: str,
    account_id: int | None,
    user_id: int,
    chat_id: int,
    text: str,
    markup: Any,
    now: datetime | None = None,
    offer: OfferSpec | None = None,
) -> bool:
    """Claim, commit, THEN send. True only when the message was sent.

    With ``offer``, the claim also writes the personal offer (same transaction) and
    the message gains the offer line. The offer is live only while the message is out:
    a recipient who refuses it for good has it voided, and so does a transient failure
    (together with the released claim). Without ``offer`` nothing here changes.

    A transient failure puts the claim back (a later run retries). A permanent one
    (the customer blocked the bot) keeps the claim and marks it ``undelivered``, so
    it is not retried each run and a later deletion can say the warning never arrived.
    """
    async with session_factory() as session:
        claimed = await claim_notice(
            session,
            event_key=event_key,
            kind=kind,
            account_id=account_id,
            user_id=user_id,
            now=now,
            offer=offer,
        )
        if not claimed:
            await end_read_transaction(session)
            return False
        await session.commit()
    if offer is not None:
        text = f"{text}\n\n{offer_line(offer)}"
    try:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
    except Exception as exc:
        logger.warning("Notice %s not sent (%s)", event_key, type(exc).__name__)
        async with session_factory() as session:
            if is_refused_by_recipient(exc):
                await mark_delivered(session, event_key=event_key, refused_for_good=True)
                if offer is not None:
                    await void_offer(session, event_key=event_key)
            else:
                await release_notice(session, event_key=event_key)
                if offer is not None:
                    # The message is not out, so the discount is not either: the retry
                    # (``write_offer`` on the reclaimed key) makes it live again.
                    await void_offer(session, event_key=event_key)
                if isinstance(exc, TelegramBadRequest):
                    # Not the recipient's doing: OUR message is malformed. It does not count
                    # as warned, it is retried, and the owner is told once.
                    logger.exception("Notice %s refused as a bad request", event_key)
                    await enqueue_report(
                        session,
                        event_key=f"notice_bug:{event_key.split(':', maxsplit=1)[0]}:{datetime.now(UTC).date()}",
                        topic_key=TOPIC_ERRORS,
                        body=f"⚠️ یه پیام خودکار به مشتری رد شد (خطای درخواست). کلید: {event_key}",
                    )
            await session.commit()
        return False
    async with session_factory() as session:
        await mark_delivered(session, event_key=event_key)
        await session.commit()
    return True


def is_refused_by_recipient(exc: BaseException) -> bool:
    """The user blocked the bot, was deactivated, or the chat is gone: nothing we can fix.

    A generic ``TelegramBadRequest`` is NOT this: that is our own bug.
    """
    if isinstance(exc, TelegramForbiddenError):
        return True
    text = str(exc).lower()
    return isinstance(exc, TelegramBadRequest) and (
        "chat not found" in text or "user is deactivated" in text
    )


def is_permanent_send_failure(exc: BaseException) -> bool:
    """Kept for the callers that only decide whether a claim may be put back."""
    return is_refused_by_recipient(exc)

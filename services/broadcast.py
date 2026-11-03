"""Sending one message to many customers, one committed recipient at a time.

A restart does not replay a recipient whose outcome was committed. The gap
between Telegram accepting a send and that commit is still one chat: this is
not an exactly-once guarantee.

Three things make this harder than a loop over ``send_message``:

* **Rate limits.** Telegram throttles bulk sends and answers with a
  ``retry_after``. Ignoring it gets the bot limited for longer, so the worker
  paces itself and obeys the number Telegram gives back.
* **Blocked users.** Someone who blocked the bot will never receive anything;
  retrying them forever burns the budget that paying customers need.
* **Interruptions.** A send to a few thousand people spans minutes. Progress
  lives in the database, per recipient, so a restart resumes rather than
  starting over or giving up.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from sqlalchemy import Select, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import Order, OrderStatus, User
from database.models.broadcast import (
    Broadcast,
    BroadcastAudience,
    BroadcastState,
    BroadcastTarget,
    DeliveryState,
)
from database.models.pasarguard_account import PasarGuardAccount
from services.accounts import live_account_clause

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_BODY_CHARS",
    "SEND_INTERVAL_SECONDS",
    "BroadcastError",
    "BroadcastProgress",
    "AUDIENCE_TITLES_FA",
    "cancel_broadcast",
    "count_audience",
    "create_broadcast",
    "get_progress",
    "run_pending_broadcasts",
]

# Telegram tolerates roughly 30 messages a second overall. Twenty leaves room
# for the ordinary traffic a live bot is still serving mid-broadcast.
SEND_INTERVAL_SECONDS = 0.05
MAX_BODY_CHARS = 3_500
_EXPIRING_WINDOW_DAYS = 7

AUDIENCE_TITLES_FA: dict[BroadcastAudience, str] = {
    BroadcastAudience.all_customers: "همهٔ مشتری‌ها",
    BroadcastAudience.with_service: "دارندگان سرویس",
    BroadcastAudience.expiring_soon: "سرویس‌های رو به انقضا",
    BroadcastAudience.without_service: "بدون سرویس",
}


class BroadcastError(Exception):
    """A broadcast that must not be created or started, in Persian."""


@dataclass(frozen=True)
class BroadcastProgress:
    broadcast_id: int
    state: BroadcastState
    total: int
    sent: int
    blocked: int
    failed: int

    @property
    def pending(self) -> int:
        return self.total - self.sent - self.blocked - self.failed

    @property
    def done(self) -> bool:
        return self.pending == 0


def expiring_users_exists() -> Any:
    """Correlated EXISTS: the user has an ACTIVE order expiring inside the window.

    The panel owns live expiry, but ACTIVE orders carry the date the renewal
    reminder job already works from, so a campaign, a reminder and the users
    list's «در حال انقضا» filter all agree about who is about to expire.
    """
    now = datetime.now(UTC)
    cutoff = now + timedelta(days=_EXPIRING_WINDOW_DAYS)
    return (
        select(Order.user_id)
        .where(
            Order.user_id == User.id,
            Order.status == OrderStatus.ACTIVE,
            Order.expires_at.is_not(None),
            Order.expires_at > now,
            Order.expires_at <= cutoff,
        )
        .exists()
    )


def _audience_query(audience: BroadcastAudience) -> Select[tuple[User]]:
    """Users to aim at. Admins and banned users are never included.

    Banned users cannot use the bot, and an admin does not need a marketing
    message about their own product.
    """
    base = select(User).where(User.is_banned.is_(False), User.is_admin.is_(False))
    has_account = select(PasarGuardAccount.user_id).where(
        PasarGuardAccount.user_id == User.id, live_account_clause()
    )
    if audience is BroadcastAudience.with_service:
        return base.where(has_account.exists())
    if audience is BroadcastAudience.without_service:
        return base.where(~has_account.exists())
    if audience is BroadcastAudience.expiring_soon:
        return base.where(expiring_users_exists())
    return base


async def count_audience(session: AsyncSession, audience: BroadcastAudience) -> int:
    """How many people a broadcast would reach, for the confirmation screen."""
    subq = _audience_query(audience).subquery()
    return int(await session.scalar(select(func.count()).select_from(subq)) or 0)


async def create_broadcast(
    session: AsyncSession,
    *,
    body: str,
    audience: BroadcastAudience,
    admin_telegram_id: int | None = None,
) -> Broadcast:
    """Freeze the audience into rows and queue the campaign.

    Snapshotting here rather than at send time means a customer who signs up
    mid-send does not receive a campaign that was never aimed at them.
    """
    text = body.strip()
    if not text:
        raise BroadcastError("متن پیام خالی است.")
    if len(text) > MAX_BODY_CHARS:
        raise BroadcastError(f"متن پیام نباید بیش از {MAX_BODY_CHARS} کاراکتر باشد.")

    users = list((await session.execute(_audience_query(audience))).scalars().all())
    if not users:
        raise BroadcastError("هیچ مشتری‌ای در این گروه نیست.")

    broadcast = Broadcast(
        body=text,
        audience=audience,
        state=BroadcastState.sending,
        created_by_admin_id=admin_telegram_id,
        started_at=datetime.now(UTC),
    )
    session.add(broadcast)
    await session.flush()

    session.add_all(
        BroadcastTarget(
            broadcast_id=broadcast.id,
            user_id=int(user.id),
            telegram_id=int(user.telegram_id),
            state=DeliveryState.pending,
        )
        for user in users
    )
    await session.flush()
    return broadcast


async def get_progress(session: AsyncSession, broadcast_id: int) -> BroadcastProgress:
    broadcast = await session.get(Broadcast, broadcast_id)
    if broadcast is None:
        raise BroadcastError("این ارسال یافت نشد.")
    rows = await session.execute(
        select(BroadcastTarget.state, func.count())
        .where(BroadcastTarget.broadcast_id == broadcast_id)
        .group_by(BroadcastTarget.state)
    )
    counts = {state: int(n) for state, n in rows.all()}
    return BroadcastProgress(
        broadcast_id=broadcast_id,
        state=broadcast.state,
        total=sum(counts.values()),
        sent=counts.get(DeliveryState.sent, 0),
        blocked=counts.get(DeliveryState.blocked, 0),
        failed=counts.get(DeliveryState.failed, 0),
    )


async def cancel_broadcast(session: AsyncSession, broadcast_id: int) -> BroadcastProgress:
    """Stop a running campaign. Messages already delivered stay delivered."""
    broadcast = await session.get(Broadcast, broadcast_id)
    if broadcast is None:
        raise BroadcastError("این ارسال یافت نشد.")
    if broadcast.state is not BroadcastState.sending:
        raise BroadcastError("این ارسال در حال اجرا نیست.")
    broadcast.state = BroadcastState.cancelled
    broadcast.finished_at = datetime.now(UTC)
    await session.flush()
    return await get_progress(session, broadcast_id)


_RECIPIENT_GONE_MARKERS = ("chat not found", "user is deactivated", "bot was blocked")


def _is_recipient_gone(exc: TelegramBadRequest) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _RECIPIENT_GONE_MARKERS)


async def _deliver(bot: Bot, target: BroadcastTarget, body: str) -> None:
    """Send to one recipient, translating Telegram's failures into states."""
    try:
        await bot.send_message(chat_id=int(target.telegram_id), text=body)
    except TelegramRetryAfter as exc:
        # Obey Telegram rather than pushing harder; the row stays pending.
        await asyncio.sleep(float(exc.retry_after) + 1)
        raise
    except TelegramForbiddenError as exc:
        # Blocked the bot, or deleted the account. Retrying never succeeds.
        target.state = DeliveryState.blocked
        target.last_error = str(exc)[:255]
        return
    except TelegramBadRequest as exc:
        # Only «the recipient is gone» means blocked. Any other BadRequest (text
        # too long, bad markup) is about the message, not the person: calling it
        # «blocked» would hide a broken campaign as a list of unreachable users.
        target.state = DeliveryState.blocked if _is_recipient_gone(exc) else DeliveryState.failed
        target.last_error = str(exc)[:255]
        return
    target.state = DeliveryState.sent
    target.sent_at = datetime.now(UTC)


async def _persist_target(session: AsyncSession, target: BroadcastTarget) -> None:
    """Commit one recipient on its own connection.

    The caller's batch commit can still crash. Telegram has already answered
    for this chat, so that answer must survive the rollback. A recipient that
    failed and stays pending is written too: its attempt count is what retires
    it after three tries.

    Once written here the row leaves the caller's session. On a SQLite file the
    caller still holds a read snapshot from before this write, and rewriting
    the same row from it is refused at once as «database is locked».
    """
    if target.state is DeliveryState.pending and int(target.attempts) == 0:
        return
    bind = session.bind
    if bind is None:
        return
    maker = async_sessionmaker(bind, expire_on_commit=False)
    try:
        async with maker() as durable:
            row = await durable.get(BroadcastTarget, int(target.id))
            if row is None:
                return
            row.state = target.state
            row.sent_at = target.sent_at
            row.attempts = int(target.attempts)
            row.last_error = target.last_error
            await durable.commit()
    except OperationalError as exc:
        # A shared in-memory connection cannot open a second transaction.
        # The caller's own commit still records the outcome there.
        if "within a transaction" not in str(exc):
            raise
        return
    session.expunge(target)


async def run_pending_broadcasts(
    session: AsyncSession, bot: Bot, *, batch: int = 40
) -> BroadcastProgress | None:
    """Push one batch of the oldest running campaign forward.

    Deliberately batched rather than looping to completion: the worker returns
    control between batches, so a long campaign never starves the bot's ordinary
    traffic and a cancellation takes effect within seconds.
    """
    broadcast = await session.scalar(
        select(Broadcast)
        .where(Broadcast.state == BroadcastState.sending)
        .order_by(Broadcast.id)
        .limit(1)
    )
    if broadcast is None:
        return None

    targets = list(
        (
            await session.execute(
                select(BroadcastTarget)
                .where(
                    BroadcastTarget.broadcast_id == broadcast.id,
                    BroadcastTarget.state == DeliveryState.pending,
                )
                .order_by(BroadcastTarget.id)
                .limit(batch)
            )
        )
        .scalars()
        .all()
    )

    if not targets:
        broadcast.state = BroadcastState.done
        broadcast.finished_at = datetime.now(UTC)
        await session.commit()
        return await get_progress(session, int(broadcast.id))

    banned = set(
        (
            await session.execute(
                select(User.id).where(
                    User.id.in_([int(t.user_id) for t in targets]), User.is_banned.is_(True)
                )
            )
        )
        .scalars()
        .all()
    )

    for target in targets:
        if int(target.user_id) in banned:
            # Targets are frozen at create time; a ban since then must still win.
            target.state = DeliveryState.skipped
            target.last_error = "banned after the campaign started"
            await _persist_target(session, target)
            continue
        target.attempts += 1
        try:
            await _deliver(bot, target, broadcast.body)
        except TelegramRetryAfter:
            # Already slept inside _deliver; stop this batch and resume later.
            await _persist_target(session, target)
            break
        except Exception as exc:  # noqa: BLE001 - one bad recipient must not stop the rest
            target.last_error = str(exc)[:255]
            if target.attempts >= 3:
                target.state = DeliveryState.failed
            logger.warning("Broadcast %s to %s failed", broadcast.id, target.telegram_id)
        await _persist_target(session, target)
        await asyncio.sleep(SEND_INTERVAL_SECONDS)

    await session.commit()
    return await get_progress(session, int(broadcast.id))

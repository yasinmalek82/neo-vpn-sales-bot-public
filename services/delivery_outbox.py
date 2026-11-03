"""Commit delivery intent with business state; send after releasing the DB.

Leases prevent overlapping workers and expire after a crashed sender. Telegram
has no idempotency key: a crash after acceptance but before acknowledgement may
repeat one notification. It can never repeat the financial operation.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.media import send_stored_media
from database.models.delivery import DeliveryOutbox


async def enqueue_delivery(
    session: AsyncSession, *, event_key: str, kind: str, chat_id: int, payload: dict[str, Any]
) -> DeliveryOutbox:
    existing = await session.scalar(
        select(DeliveryOutbox).where(DeliveryOutbox.event_key == event_key)
    )
    if existing is not None:
        return existing
    await session.flush()
    row = DeliveryOutbox(event_key=event_key, kind=kind, chat_id=chat_id, payload=payload)
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        existing = await session.scalar(
            select(DeliveryOutbox).where(DeliveryOutbox.event_key == event_key)
        )
        if existing is None:
            raise
        return existing
    return row


async def _send(
    row: DeliveryOutbox,
    bot: Bot,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    if row.kind == "service":
        from bot.delivery import deliver_provisioned_service_card
        from bot.texts import texts
        from database.base import end_read_transaction
        from database.models import PasarGuardAccount, User
        from services.ux_format import to_persian_digits

        async with session_factory() as session:
            user = await session.get(User, row.payload["user_id"])
            account = await session.get(PasarGuardAccount, row.payload["account_id"])
            if (
                user is None
                or account is None
                or account.user_id != user.id
                or user.telegram_id != row.chat_id
            ):
                raise PermissionError("delivery_owner_changed")
            await end_read_transaction(session)
            delivered = await deliver_provisioned_service_card(
                session,
                settings,
                user_id=row.payload["user_id"],
                account_id=row.payload["account_id"],
                bot=bot,
                chat_id=row.chat_id,
                intro=row.payload.get("intro")
                or texts.payment_ready_intro.format(
                    order_id=to_persian_digits(row.payload["order_id"])
                ),
                show_renew=row.payload.get("show_renew"),
            )
            if not delivered:
                raise ValueError("delivery_overview_unavailable")
        return
    if row.kind != "receipt":
        raise ValueError("unsupported_delivery_kind")
    # Revoked administrators must not receive a delayed payment receipt.
    if row.chat_id not in settings.admin_telegram_ids:
        raise PermissionError("recipient_no_longer_admin")
    await send_stored_media(
        bot,
        chat_id=row.chat_id,
        file_id=row.payload["file_id"],
        media_type=row.payload["media_type"],
        caption=row.payload["caption"],
        reply_markup=InlineKeyboardMarkup.model_validate(row.payload["markup"]),
    )


async def flush_deliveries(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    settings: Settings,
    *,
    event_prefix: str | None = None,
    limit: int = 20,
) -> tuple[int, int]:
    now = datetime.now(UTC)
    ready = (
        DeliveryOutbox.state == "pending",
        DeliveryOutbox.next_attempt_at <= now,
        or_(DeliveryOutbox.claimed_until.is_(None), DeliveryOutbox.claimed_until < now),
    )
    async with session_factory() as session:
        query = (
            select(DeliveryOutbox.id)
            .where(*ready)
            .order_by(DeliveryOutbox.next_attempt_at, DeliveryOutbox.id)
            .limit(limit)
        )
        if event_prefix is not None:
            query = query.where(DeliveryOutbox.event_key.startswith(event_prefix, autoescape=True))
        ids = list((await session.scalars(query)).all())
    sent = failed = 0
    for row_id in ids:
        token = uuid4().hex
        async with session_factory() as session:
            claim = await session.execute(
                update(DeliveryOutbox)
                .where(DeliveryOutbox.id == row_id, *ready)
                .values(
                    claim_token=token,
                    claimed_until=datetime.now(UTC) + timedelta(minutes=2),
                    attempts=DeliveryOutbox.attempts + 1,
                )
            )
            await session.commit()
            if claim.rowcount != 1:
                continue
            row = await session.get(DeliveryOutbox, row_id)
            assert row is not None
            session.expunge(row)
        # No transaction or lazy relationship survives the network await.
        state, error, delay = "sent", None, 0
        try:
            async with asyncio.timeout(45):
                await _send(row, bot, settings, session_factory)
        except TelegramRetryAfter as exc:
            state, error, delay = "pending", type(exc).__name__, max(1, exc.retry_after)
        except (TelegramForbiddenError, PermissionError) as exc:
            state, error = "failed", type(exc).__name__
        except Exception as exc:
            state, error = "pending", type(exc).__name__
        completed = datetime.now(UTC)
        async with session_factory() as session:
            await session.execute(
                update(DeliveryOutbox)
                .where(
                    DeliveryOutbox.id == row_id,
                    DeliveryOutbox.claim_token == token,
                )
                .values(
                    state=state,
                    last_error=error,
                    claim_token=None,
                    claimed_until=None,
                    sent_at=completed if state == "sent" else None,
                    next_attempt_at=completed + timedelta(seconds=delay),
                )
            )
            await session.commit()
        sent += state == "sent"
        failed += state != "sent"
    return sent, failed

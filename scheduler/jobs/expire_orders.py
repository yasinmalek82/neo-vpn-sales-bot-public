"""Expire stale PENDING_PAYMENT orders and notify customers."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import selectinload

from bot.config import Settings
from bot.texts import texts
from database.models import Order
from services.automation import expire_stale_orders
from services.brand import with_support
from services.ux_format import to_persian_digits

logger = logging.getLogger(__name__)


async def run_expire_stale_orders(
    *,
    session_factory: async_sessionmaker[Any],
    expiry_minutes: int,
    bot: Bot | None = None,
    settings: Settings | None = None,
) -> int:
    async with session_factory() as session:
        try:
            expired = await expire_stale_orders(session, expiry_minutes=expiry_minutes)
            await session.commit()
            if expired:
                logger.info("Expired %s stale pending orders", len(expired))
            if bot is not None and expired:
                ids = [o.id for o in expired]
                result = await session.execute(
                    select(Order).where(Order.id.in_(ids)).options(selectinload(Order.user))
                )
                for order in result.scalars().all():
                    if order.user is None:
                        continue
                    body = texts.payment_order_expired.format(order_id=to_persian_digits(order.id))
                    try:
                        await bot.send_message(
                            chat_id=order.user.telegram_id,
                            text=(with_support(body, settings) if settings is not None else body),
                        )
                    except Exception:
                        logger.exception("Failed to DM expired order %s", order.id)
            return len(expired)
        except Exception:
            await session.rollback()
            logger.exception("expire_stale_orders job failed")
            return 0

"""Send polished renewal reminder DMs (service card + renew CTA) for ACTIVE orders."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.config import Settings
from bot.delivery import deliver_provisioned_service_card
from bot.keyboards.account import AccountCB, service_detail_keyboard
from bot.keyboards.lifecycle import buy_keyboard
from bot.keyboards.main_menu import MenuCallback
from bot.texts import texts
from database.base import end_read_transaction
from database.models import PasarGuardAccount
from database.models.lifecycle import NOTICE_TRIAL_ENDING
from services.account_orders import is_trial_account
from services.accounts import find_account_id_for_order
from services.automation import (
    find_renewal_reminder_candidates,
    find_trial_ending_candidates,
    mark_reminder_sent,
)
from services.lifecycle_settings import load_config
from services.service_notices import send_notice_once
from services.ux_format import to_persian_digits

logger = logging.getLogger(__name__)


def _fallback_reminder_keyboard(
    *, account_id: int | None, is_trial: bool = False
) -> InlineKeyboardMarkup:
    if account_id is not None:
        return service_detail_keyboard(
            account_id, has_link=False, show_renew=True, is_trial=is_trial
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 خرید / تمدید", callback_data=MenuCallback.BUY)],
            [InlineKeyboardButton(text="📱 سرویس‌های من", callback_data=MenuCallback.ACCOUNT)],
            [InlineKeyboardButton(text="⬅️ منوی اصلی", callback_data=AccountCB.BACK)],
        ]
    )


async def _trial_ending_messages(
    session: Any, session_factory: async_sessionmaker[Any], bot: Bot
) -> int:
    """One message to a customer whose free trial ends within a day: it says so and that a
    plan can be bought. No renew button (a trial cannot be renewed). Once per trial account,
    claimed in ``service_notices`` before the send."""
    candidates = await find_trial_ending_candidates(session)
    await end_read_transaction(session)
    sent = 0
    for item in candidates:
        try:
            if await send_notice_once(
                session_factory,
                bot,
                event_key=f"trialend:{item.account_id}",
                kind=NOTICE_TRIAL_ENDING,
                account_id=item.account_id,
                user_id=int(item.user.id),
                chat_id=int(item.user.telegram_id),
                text=texts.notify_trial_ending,
                markup=buy_keyboard(),
            ):
                sent += 1
        except Exception:
            logger.exception("Trial-ending message failed for account %s", item.account_id)
    return sent


async def run_renewal_reminders(
    *,
    session_factory: async_sessionmaker[Any],
    bot: Bot,
    reminder_days: list[int],
    settings: Settings,
) -> int:
    """``reminder_days`` (the env list) only seeds the first settings row: the owner's
    own days from «⚙️ تنظیمات» are what every run uses (``services/lifecycle_settings.py``)."""
    async with session_factory() as session:
        try:
            config = await load_config(session, seed_days=reminder_days)
            candidates = await find_renewal_reminder_candidates(
                session, reminder_days=list(config.warn_days)
            )
            sent = 0
            for item in candidates:
                try:
                    account_id = await find_account_id_for_order(session, item.order)
                    # No read snapshot across the send. A commit by another job
                    # during it made the mark below fail («database is
                    # locked»), the pass rolled back, and the next pass sent
                    # every reminder of this one again.
                    await end_read_transaction(session)
                    intro = texts.renewal_reminder_intro.format(
                        days=to_persian_digits(item.days_left)
                    )
                    delivered = False
                    if account_id is not None:
                        try:
                            delivered = await deliver_provisioned_service_card(
                                session,
                                settings,
                                user_id=item.user.id,
                                account_id=account_id,
                                bot=bot,
                                chat_id=item.user.telegram_id,
                                intro=intro,
                                show_renew=True,
                            )
                        except Exception:
                            logger.exception(
                                "Polished renewal card failed for order %s; text fallback",
                                item.order.id,
                            )
                            delivered = False
                    if not delivered:
                        account = (
                            await session.get(PasarGuardAccount, account_id)
                            if account_id is not None
                            else None
                        )
                        fallback_is_trial = account is not None and await is_trial_account(
                            session, account
                        )
                        await end_read_transaction(session)
                        await bot.send_message(
                            chat_id=item.user.telegram_id,
                            text=texts.renewal_reminder.format(
                                order_id=to_persian_digits(item.order.id),
                                days=to_persian_digits(item.days_left),
                            ),
                            reply_markup=_fallback_reminder_keyboard(
                                account_id=account_id, is_trial=fallback_is_trial
                            ),
                        )
                    # The card delivery read the database (QR background) and then
                    # talked to Telegram: that read snapshot is stale by now, and the
                    # mark below would die with «database is locked» if a job committed
                    # during the send. Found by the e2e run with a commit at every
                    # network call: no e2e scenario ran this job before section ۱۶.
                    await end_read_transaction(session)
                    await mark_reminder_sent(session, order_id=item.order.id)
                    # Per reminder, not per pass: a sent reminder stays marked
                    # whatever happens to the ones after it.
                    await session.commit()
                    sent += 1
                except Exception:
                    logger.exception("Failed renewal reminder for order %s", item.order.id)
            await session.commit()
            sent += await _trial_ending_messages(session, session_factory, bot)
            if sent:
                logger.info("Sent %s renewal reminders", sent)
            return sent
        except Exception:
            await session.rollback()
            logger.exception("renewal_reminders job failed")
            return 0

"""Post-provision delivery: QR + overview caption + service detail keyboard."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api_client.factory import get_pasarguard_client
from bot.config import Settings
from bot.keyboards.account import service_detail_keyboard
from database.base import end_read_transaction
from database.models import PasarGuardAccount
from services.account_orders import is_trial_account
from services.accounts import AccountOverview, get_account_overview
from services.brand import resolve_bot_username
from services.qr_brand import load_qr_background, render_branded_qr
from services.service_card import build_service_card_caption
from services.subscription_qr import render_subscription_qr_png
from services.ux_format import is_expiring_soon

logger = logging.getLogger(__name__)


async def service_qr_png(
    session: AsyncSession, settings: Settings, *, url: str, bot: Bot | None
) -> bytes:
    """The service QR on the brand template (or the owner's background).

    Falls back to the plain code on any failure: a broken upload or a missing
    handle must never cost a customer the link they just paid for. The render
    runs off the event loop — it takes ~150 ms, every other tap would wait.
    """
    try:
        handle: str | None = None
        if isinstance(bot, Bot):
            try:
                handle = await resolve_bot_username(bot, settings) or None
            except Exception:  # noqa: BLE001 - the handle is decoration, never a blocker
                logger.warning("Could not resolve the bot handle for the QR")
        background = await load_qr_background(session)
        return await asyncio.to_thread(render_branded_qr, url, background=background, handle=handle)
    except Exception:
        logger.exception("Branded QR failed; sending the plain code")
        return render_subscription_qr_png(url)


def _warn_days(settings: Settings) -> int:
    return int(settings.service_expire_warn_days)


def _detail_markup(
    overview: AccountOverview,
    *,
    warn_days: int,
    show_renew: bool | None = None,
    is_trial: bool = False,
) -> InlineKeyboardMarkup | None:
    account_id = overview.account_id
    if account_id is None:
        return None
    if show_renew is None:
        show_renew = is_expiring_soon(overview.expire, warn_days=warn_days)
    url = (overview.subscription_url or "").strip()
    return service_detail_keyboard(
        account_id,
        has_link=bool(url),
        subscription_url=url,
        show_renew=bool(show_renew),
        is_trial=is_trial,
    )


async def load_overview_for_delivery(
    session: AsyncSession,
    settings: Settings,
    *,
    user_id: int,
    account_id: int,
) -> AccountOverview:
    """Load live (or stale) overview for the newly provisioned account."""
    client = await get_pasarguard_client(session, settings)
    try:
        return await get_account_overview(
            session,
            user_id=user_id,
            client=client,
            account_id=account_id,
        )
    finally:
        await client.aclose()


async def send_service_card(
    *,
    overview: AccountOverview,
    settings: Settings,
    intro: str | None = None,
    message: Message | None = None,
    bot: Bot | None = None,
    chat_id: int | None = None,
    show_renew: bool | None = None,
    is_trial: bool = False,
    qr_png: bytes | None = None,
) -> None:
    """Send ONE service-card message: QR photo + caption + detail keyboard.

    Prefer ``message.answer_photo`` when ``message`` is set (purchase/trial).
    Prefer ``bot.send_photo(chat_id=...)`` for admin notify paths.
    Falls back to text if QR render/send fails (same as account handler).
    """
    if message is None and (bot is None or chat_id is None):
        raise RuntimeError("send_service_card requires message or bot+chat_id")

    warn_days = _warn_days(settings)
    caption = build_service_card_caption(
        overview,
        warn_days=warn_days,
        timezone=settings.display_timezone,
        intro=intro,
    )
    markup = _detail_markup(overview, warn_days=warn_days, show_renew=show_renew, is_trial=is_trial)
    url = (overview.subscription_url or "").strip()

    async def _send_text() -> None:
        if message is not None:
            await message.answer(caption, reply_markup=markup)
            return
        assert bot is not None and chat_id is not None
        await bot.send_message(chat_id=chat_id, text=caption, reply_markup=markup)

    if not url:
        await _send_text()
        return

    png: bytes | None
    try:
        png = qr_png if qr_png is not None else render_subscription_qr_png(url)
    except Exception:
        logger.exception("Failed to render subscription QR for account %s", overview.account_id)
        png = None

    if png is None:
        await _send_text()
        return

    photo = BufferedInputFile(png, filename="subscription-qr.png")
    try:
        if message is not None:
            await message.answer_photo(photo, caption=caption, reply_markup=markup)
            return
        assert bot is not None and chat_id is not None
        await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption, reply_markup=markup)
    except Exception:
        logger.exception("send_photo failed for account %s; text fallback", overview.account_id)
        await _send_text()


async def deliver_provisioned_service_card(
    session: AsyncSession,
    settings: Settings,
    *,
    user_id: int,
    order_id: int | None = None,
    account_id: int,
    intro: str | None = None,
    message: Message | None = None,
    bot: Bot | None = None,
    chat_id: int | None = None,
    show_renew: bool | None = None,
) -> bool:
    """Load overview for ``account_id`` then deliver the polished service card.

    Returns True when a card (photo or text) was sent. The card carries
    a direct link to the subscription page when available.
    """
    if order_id is not None:
        # Activation already committed the intent. The handler only supplies
        # its presentation and asks the same leased worker to deliver now.
        from database.models.delivery import DeliveryOutbox
        from services.delivery_outbox import flush_deliveries

        actual_bot = bot or _bot_of(message)
        if actual_bot is None:
            raise RuntimeError("delivery requires a bound bot")
        if not await end_read_transaction(session):
            raise RuntimeError("delivery requires committed business state")
        factory = async_sessionmaker(session.bind, expire_on_commit=False)
        key = f"service:{order_id}:customer"
        async with factory() as delivery_session:
            row = await delivery_session.scalar(
                select(DeliveryOutbox).where(DeliveryOutbox.event_key == key)
            )
            if row is None:
                raise RuntimeError("delivery_intent_missing")
            if row.payload["user_id"] != user_id or row.payload["account_id"] != account_id:
                raise ValueError("delivery_intent_mismatch")
            if row.state == "sent":
                return True
            if row.state == "pending" and row.claim_token is None:
                row.payload = {**row.payload, "intro": intro, "show_renew": show_renew}
                await delivery_session.commit()
        sent, _ = await flush_deliveries(factory, actual_bot, settings, event_prefix=key)
        return sent == 1
    overview = await load_overview_for_delivery(
        session, settings, user_id=user_id, account_id=account_id
    )
    if not overview.has_account or overview.account_id is None:
        logger.warning(
            "deliver_provisioned_service_card: no overview for user=%s account=%s",
            user_id,
            account_id,
        )
        return False
    url = (overview.subscription_url or "").strip()
    qr_png = (
        await service_qr_png(session, settings, url=url, bot=bot or _bot_of(message))
        if url
        else None
    )
    account = await session.get(PasarGuardAccount, int(overview.account_id))
    is_trial = account is not None and await is_trial_account(session, account)
    await end_read_transaction(session)
    await send_service_card(
        overview=overview,
        settings=settings,
        intro=intro,
        message=message,
        bot=bot,
        chat_id=chat_id,
        show_renew=show_renew,
        is_trial=is_trial,
        qr_png=qr_png,
    )
    return True


def _bot_of(message: Message | None) -> Bot | None:
    """``message.bot``, or None for a message not bound to a bot (tests)."""
    if message is None:
        return None
    try:
        bot = message.bot
    except Exception:  # noqa: BLE001 - aiogram raises when the message is unbound
        return None
    return bot if isinstance(bot, Bot) else None

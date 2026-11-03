"""Payment receipt upload + admin notification (Phase 4 + hardening)."""

from __future__ import annotations

import logging
from html import escape
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.keyboards.admin_payment import late_receipt_keyboard, payment_review_keyboard
from bot.keyboards.customer import receipt_waiting_keyboard
from bot.media import send_stored_media
from bot.states import PurchaseStates
from bot.texts import texts
from database.models import OrderStatus, User
from services.bank_invoices import reserve_bank_invoice
from services.delivery_outbox import enqueue_delivery, flush_deliveries
from services.orders import get_open_order
from services.payment_settings import PaymentInfo, get_payment_info
from services.payments import PaymentError, record_late_receipt, submit_card_receipt
from services.ux_format import fa_toman as _fmt_toman
from services.ux_format import format_jalali, format_toman, to_persian_digits

logger = logging.getLogger(__name__)
router = Router(name="payment")


def format_payment_instructions(info: PaymentInfo, *, order_id: int, amount_toman: int) -> str:
    bank_line = ""
    if info.bank_name and info.bank_name.strip():
        bank_line = f"🏛 بانک: {info.bank_name.strip()}\n"
    extra = ""
    if info.instructions and info.instructions.strip():
        extra = f"\n{info.instructions.strip()}\n"
    return texts.payment_instructions.format(
        order_id=to_persian_digits(order_id),
        amount=_fmt_toman(amount_toman),
        card_number=info.card_number or "—",
        holder=info.card_holder_name or "—",
        bank_line=bank_line,
        extra=extra,
    )


async def begin_payment_wait(
    message: Message,
    state: FSMContext,
    settings: Settings,
    *,
    session: AsyncSession,
    order_id: int,
    amount_toman: int,
) -> None:
    info = await get_payment_info(session, settings)
    invoice = await reserve_bank_invoice(session, settings, order_id=order_id)
    if invoice is not None:
        amount_toman = int(invoice.amount_toman)
    await session.commit()
    await state.set_state(PurchaseStates.waiting_for_receipt)
    await state.update_data(pending_order_id=order_id, pending_amount=amount_toman)
    body = format_payment_instructions(info, order_id=order_id, amount_toman=amount_toman)
    if invoice is not None:
        body += "\n\n" + texts.payment_unique_amount.format(suffix=_fmt_toman(invoice.suffix_toman))
    await message.answer(body)


def _receipt_file_id(message: Message) -> str | None:
    """Images only: Telegram photo or image/* document."""
    if message.photo:
        return message.photo[-1].file_id
    if (
        message.document
        and message.document.mime_type
        and message.document.mime_type.startswith("image/")
    ):
        return message.document.file_id
    return None


class _OrderAwaitingReceipt(BaseFilter):
    """A customer with no FSM state whose one open order still waits for its receipt.

    The state is a convenience, not the record: a bottom-keyboard tap clears it
    (and so does a restart, the storage being in memory), while the order stays
    PENDING_PAYMENT until it expires. The unique open-order index makes «the»
    order unambiguous.
    """

    # aiogram always passes the event first, so ``message`` has to be there.
    async def __call__(
        self,
        message: Message,  # noqa: ARG002
        session: AsyncSession,
        db_user: User | None,
    ) -> bool | dict[str, Any]:
        if db_user is None:
            return False
        order = await get_open_order(session, int(db_user.id))
        if order is None or order.status is not OrderStatus.PENDING_PAYMENT:
            return False
        if int(order.price_toman) - int(order.wallet_applied_toman or 0) <= 0:
            return False
        return {"receipt_order_id": int(order.id)}


@router.message(PurchaseStates.waiting_for_receipt, F.photo | F.document)
async def receive_receipt(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    db_user: User | None,
) -> None:
    data = await state.get_data()
    order_id = data.get("pending_order_id")
    await _accept_receipt(
        message, state, session, settings, bot, db_user, int(order_id) if order_id else None
    )


@router.message(StateFilter(None), F.photo | F.document, _OrderAwaitingReceipt())
async def receive_receipt_without_state(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    db_user: User | None,
    receipt_order_id: int,
) -> None:
    await _accept_receipt(message, state, session, settings, bot, db_user, receipt_order_id)


async def _accept_receipt(  # noqa: PLR0911 - explicit receipt lifecycle outcomes
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    db_user: User | None,
    order_id: int | None,
) -> None:
    if db_user is None:
        await message.answer(texts.payment_submit_error)
        return

    file_id = _receipt_file_id(message)
    if not file_id:
        await message.answer(texts.payment_need_photo)
        return

    if not order_id:
        await message.answer(texts.payment_submit_error)
        return

    info = await get_payment_info(session, settings)
    try:
        payment = await submit_card_receipt(
            session,
            order_id=int(order_id),
            user_id=db_user.id,
            receipt_file_id=file_id,
            receipt_media_type="photo" if message.photo else "document",
            card_number_display=info.card_number or "",
        )
        await session.commit()
    except PaymentError as exc:
        if str(exc) in {
            "invalid_status:PAYMENT_APPROVED",
            "invalid_status:PROVISIONING",
            "invalid_status:ACTIVE",
            "invalid_status:PROVISION_FAILED",
        }:
            await session.rollback()
            await state.clear()
            await message.answer(texts.payment_already_confirmed)
            return
        if str(exc) == "invalid_status:EXPIRED":
            # Nothing was written (the status check comes first), and a rollback
            # would expire `db_user`, which the notice below reads.
            await _late_receipt(message, session, settings, bot, db_user, int(order_id), file_id)
            return
        await session.rollback()
        logger.exception("submit_card_receipt failed for order %s", order_id)
        await message.answer(texts.payment_submit_error)
        return
    except Exception:
        logger.exception("unexpected receipt submit failure")
        await session.rollback()
        await message.answer(texts.payment_submit_error)
        return

    await state.clear()
    await message.answer(
        texts.payment_receipt_received.format(order_id=to_persian_digits(order_id)),
        reply_markup=receipt_waiting_keyboard(),
    )

    user_label = escape(db_user.username or db_user.first_name or str(db_user.telegram_id))
    caption = texts.payment_admin_caption.format(
        order_id=order_id,
        user_label=user_label,
        amount=_fmt_toman(payment.amount_toman),
        card=info.card_number or "—",
    )
    keyboard = payment_review_keyboard(int(order_id))
    for admin_tg_id in settings.admin_telegram_ids:
        try:
            await send_stored_media(
                bot,
                chat_id=admin_tg_id,
                file_id=payment.receipt_file_id,
                media_type=payment.receipt_media_type,
                caption=caption,
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("Failed to notify admin %s", admin_tg_id)


async def _late_receipt(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    db_user: User,
    order_id: int,
    file_id: str,
) -> None:
    """The customer paid after the order expired.

    The order stays EXPIRED. Financial deduplication and delivery are separate:
    each admin has a durable attachment intent which a retry can still deliver.
    """
    try:
        late = await record_late_receipt(session, order_id=order_id, user_id=db_user.id)
        user_label = escape(db_user.username or db_user.first_name or str(db_user.telegram_id))
        caption = texts.admin_late_receipt_caption.format(
            order_id=order_id,
            user_label=user_label,
            amount=format_toman(late.card_due_toman, persian_digits=True),
            expired=format_jalali(late.expired_at, persian_digits=True),
        )
        keyboard = late_receipt_keyboard(order_id, late.card_due_toman)
        prefix = f"late_receipt:{order_id}:admin:"
        for admin_tg_id in settings.admin_telegram_ids:
            await enqueue_delivery(
                session,
                event_key=f"{prefix}{admin_tg_id}",
                kind="receipt",
                chat_id=admin_tg_id,
                payload={
                    "file_id": file_id,
                    "media_type": "photo" if message.photo else "document",
                    "caption": caption,
                    "markup": keyboard.model_dump(mode="json"),
                },
            )
        await session.commit()
    except PaymentError:
        logger.exception("late receipt refused for order %s", order_id)
        await session.rollback()
        await message.answer(texts.payment_submit_error)
        return
    except Exception:
        logger.exception("unexpected late receipt failure")
        await session.rollback()
        await message.answer(texts.payment_submit_error)
        return

    # The state stays: clearing it made the same photo sent again meet the
    # «نفهمیدم» fallback. The next photo comes back here, gets this notice, and
    # The per-recipient delivery intent keeps successful sends from repeating.
    await message.answer(
        texts.notify_late_receipt.format(
            order_id=to_persian_digits(order_id),
            amount=format_toman(late.card_due_toman, persian_digits=True),
        ),
        reply_markup=receipt_waiting_keyboard(),
    )
    await flush_deliveries(
        async_sessionmaker(session.bind, expire_on_commit=False),
        bot,
        settings,
        event_prefix=prefix,
    )


@router.message(PurchaseStates.waiting_for_receipt)
async def receipt_need_photo(message: Message) -> None:
    await message.answer(texts.payment_need_photo)

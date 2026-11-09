"""Admin wallet adjustments, zeroing, and crediting a late receipt.

Financial operations remain in services.customer_lookup and keep their existing
transaction and idempotency protections. Customer-card presentation is shared
with the existing admin router until that card has its own module.
"""

from __future__ import annotations

import contextlib
from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.handlers.admin import (
    _customer_id_from,
    _customer_name,
    _edit_or_answer_admin,
    _fmt_toman,
    _show_customer,
    _tg_message,
)
from bot.handlers.admin_access import _deny_if_not_admin, _is_admin
from bot.keyboards.admin import (
    AdminCB,
    admin_back_home_keyboard,
    admin_cancel_input_keyboard,
    back_to_customer_keyboard,
    wallet_confirm_keyboard,
    wallet_zero_confirm_keyboard,
)
from bot.states import AdminStates
from bot.texts import texts
from database.base import end_read_transaction
from database.models import Order, OrderStatus, User
from services.customer_lookup import (
    ManualAdjustError,
    WalletAdjustRefused,
    apply_wallet_adjustment,
    get_customer_card,
    preview_wallet_adjustment,
    zero_wallet,
)
from services.payments import late_receipt_credited, late_receipt_order_id, late_receipt_stamp

router = Router(name="admin_wallet")


@router.callback_query(F.data.startswith(AdminCB.WALLET_ZERO_OK))
async def admin_wallet_zero_apply(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    user_id = await _customer_id_from(callback, AdminCB.WALLET_ZERO_OK)
    if user_id is None:
        return
    tg_id = callback.from_user.id if callback.from_user else 0
    try:
        removed = await zero_wallet(session, user_id=user_id, admin_telegram_id=int(tg_id))
        await session.commit()
    except ManualAdjustError as exc:
        await session.rollback()
        await callback.answer(str(exc)[:190], show_alert=True)
        return
    await callback.answer(texts.admin_wallet_zero_done.format(amount=_fmt_toman(removed)))
    message = _tg_message(callback)
    if message is not None:
        await _show_customer(message, session, user_id)


@router.callback_query(F.data.startswith(AdminCB.WALLET_ZERO))
async def admin_wallet_zero_ask(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """Zeroing a wallet is taking money away — it gets a confirmation."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if (callback.data or "").startswith(AdminCB.WALLET_ZERO_OK):
        return
    user_id = await _customer_id_from(callback, AdminCB.WALLET_ZERO)
    if user_id is None:
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    card = await get_customer_card(session, user_id)
    await end_read_transaction(session)
    if card.wallet_balance_toman <= 0:
        await _edit_or_answer_admin(
            message, texts.admin_wallet_zero_already, back_to_customer_keyboard(user_id)
        )
        return
    body = texts.admin_wallet_zero_confirm.format(
        name=escape(_customer_name(card.user)),
        amount=_fmt_toman(card.wallet_balance_toman),
    )
    await _edit_or_answer_admin(message, body, wallet_zero_confirm_keyboard(user_id))


@router.callback_query(F.data.startswith(AdminCB.WALLET_ADD))
async def admin_wallet_start(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(AdminCB.WALLET_ADD) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_invalid_id, show_alert=True)
        return
    target = await session.get(User, int(raw))
    await end_read_transaction(session)
    if target is None:
        await callback.answer(texts.admin_user_not_found, show_alert=True)
        return
    await state.clear()
    await state.update_data(wallet_user_id=int(raw))
    await state.set_state(AdminStates.waiting_wallet_amount)
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _edit_or_answer_admin(
        message,
        texts.admin_wallet_prompt.format(
            name=escape(_customer_name(target)),
            wallet=_fmt_toman(int(target.wallet_balance_toman or 0)),
        ),
        admin_cancel_input_keyboard(),
    )


@router.message(AdminStates.waiting_wallet_amount)
async def admin_wallet_amount(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None, settings, db_user):
        await state.clear()
        await message.answer(texts.admin_action_denied, reply_markup=admin_back_home_keyboard())
        return
    data = await state.get_data()
    user_id = data.get("wallet_user_id")
    if not user_id:
        await state.clear()
        await message.answer(texts.admin_action_error, reply_markup=admin_back_home_keyboard())
        return
    try:
        preview = await preview_wallet_adjustment(
            session, user_id=int(user_id), raw_amount=message.text or ""
        )
    except WalletAdjustRefused as refused:
        await end_read_transaction(session)
        if refused.kind == "user_not_found":
            await state.clear()
            await message.answer(str(refused), reply_markup=admin_back_home_keyboard())
        else:
            await message.answer(str(refused), reply_markup=admin_cancel_input_keyboard())
        return
    target, amount, balance = preview.user, preview.amount_toman, preview.balance_toman
    await end_read_transaction(session)
    # Hold the amount until the confirmation tap; nothing is written yet.
    stamp = f"{message.message_id}"
    await state.update_data(wallet_amount=amount, wallet_stamp=stamp)
    await message.answer(
        texts.admin_wallet_confirm.format(
            name=escape(_customer_name(target)),
            verb=(texts.admin_wallet_verb_credit if amount > 0 else texts.admin_wallet_verb_debit),
            amount=_fmt_toman(abs(amount)),
            after=_fmt_toman(balance + amount),
        ),
        reply_markup=wallet_confirm_keyboard(int(user_id)),
    )


def _late_receipt_order_id(data: str | None, prefix: str) -> int | None:
    raw = (data or "")[len(prefix) :]
    return int(raw) if raw.isdigit() else None


@router.callback_query(F.data.startswith(AdminCB.LATE_CREDIT))
async def admin_late_receipt_credit(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """«🏦 شارژ کیف پول» under a late receipt: the manual wallet flow, pre-filled.

    Nothing is written here. It loads the amount and the stamp the normal confirm
    step (`admin_wallet_apply` → `adjust_wallet_manually`) already uses, then shows
    that same confirm screen. The amount is the order's card due, read from the
    order, never from the button.
    """
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    order_id = _late_receipt_order_id(callback.data, AdminCB.LATE_CREDIT)
    if order_id is None:
        await callback.answer(texts.admin_invalid_id, show_alert=True)
        return
    order = await session.get(Order, order_id)
    card_due = (
        max(0, int(order.price_toman) - int(order.wallet_applied_toman or 0))
        if order is not None
        else 0
    )
    if order is None or order.status != OrderStatus.EXPIRED or card_due <= 0:
        await end_read_transaction(session)
        await callback.answer(texts.admin_late_receipt_gone, show_alert=True)
        return
    if await late_receipt_credited(session, user_id=int(order.user_id), order_id=order_id):
        await end_read_transaction(session)
        await callback.answer(texts.admin_late_receipt_already, show_alert=True)
        return
    target = await session.get(User, int(order.user_id))
    await end_read_transaction(session)
    if target is None:
        await callback.answer(texts.admin_user_not_found, show_alert=True)
        return
    await state.clear()
    await state.update_data(
        wallet_user_id=int(target.id),
        wallet_amount=card_due,
        wallet_stamp=late_receipt_stamp(order_id),
    )
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    balance = int(target.wallet_balance_toman or 0)
    await message.answer(
        texts.admin_wallet_confirm.format(
            name=escape(_customer_name(target)),
            verb=texts.admin_wallet_verb_credit,
            amount=_fmt_toman(card_due),
            after=_fmt_toman(balance + card_due),
        ),
        reply_markup=wallet_confirm_keyboard(int(target.id)),
    )


@router.callback_query(F.data.startswith(AdminCB.WALLET_OK))
async def admin_wallet_apply(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    bot: Bot,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(AdminCB.WALLET_OK) :]
    data = await state.get_data()
    amount = data.get("wallet_amount")
    stamp = data.get("wallet_stamp")
    if not raw.isdigit() or not amount or not stamp:
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    if data.get("wallet_user_id") != int(raw):
        # The pending amount was typed for another customer; an older card's
        # button must not spend it here.
        await callback.answer(texts.stale_button, show_alert=True)
        return
    admin_id = callback.from_user.id if callback.from_user else 0
    late_order = late_receipt_order_id(str(stamp))
    if late_order is not None and await late_receipt_credited(
        session, user_id=int(raw), order_id=late_order
    ):
        # A late receipt goes to every admin; the stamp is what they share, so a
        # second admin confirming the same one would pay it out twice.
        await end_read_transaction(session)
        await state.clear()
        await callback.answer(texts.admin_late_receipt_already, show_alert=True)
        return
    try:
        await apply_wallet_adjustment(
            session,
            user_id=int(raw),
            amount_toman=int(amount),
            admin_telegram_id=int(admin_id),
            stamp=str(stamp),
        )
    except ManualAdjustError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await state.clear()
    target = await session.get(User, int(raw))
    new_balance = int(target.wallet_balance_toman or 0) if target else 0
    await end_read_transaction(session)
    await callback.answer(texts.admin_wallet_done.format(wallet=_fmt_toman(new_balance)))

    # The customer's money changed; they are told without being told who did it.
    if target is not None:
        tmpl = texts.notify_wallet_credit if int(amount) > 0 else texts.notify_wallet_debit
        with contextlib.suppress(Exception):
            await bot.send_message(
                chat_id=target.telegram_id,
                text=tmpl.format(
                    amount=_fmt_toman(abs(int(amount))), wallet=_fmt_toman(new_balance)
                ),
            )

    message = _tg_message(callback)
    if message is not None:
        await _show_customer(message, session, int(raw))

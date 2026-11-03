"""Ask a reseller who a service is for: name, then phone.

Three flows need this — buying a service, handing out a trial, relabelling an
old one — and all three used to ask for «اسم یا شماره» in a single free-text
box. Two things were wrong with that. A number is only findable by its last
four digits when it was stored as a number, and the same box seeds the panel
username: «رضا 09123456789» in one field drags eleven digits into `reza_…`.
Asked separately, the name seeds the panel name and the phone stays a label.

So it is asked in two steps, here, once. Which flow is running lives in FSM
data under ``label_flow``; each one names its continuation at the bottom of
this file. The continuations are imported *inside* the function on purpose:
`purchase`, `referral` and `account` all start this flow, so a module-level
import would close a cycle.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.states import ResellerStates
from bot.texts import texts
from database.models import User
from services.customer_label import (
    CustomerLabel,
    normalize_phone,
    sanitize_customer_name,
)

logger = logging.getLogger(__name__)
router = Router(name="customer_label")

SKIP_PHONE_CB = "rslabel:nophone"

FLOW_PURCHASE = "purchase"
FLOW_TRIAL = "trial"
FLOW_RENAME = "rename"


def skip_phone_keyboard() -> InlineKeyboardMarkup:
    """«شماره ندارم» — the only way out of step two that is not a number.

    Without it the shop has to type something, and whatever they type becomes a
    phone number that no search will ever match.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.reseller_label_skip_phone, callback_data=SKIP_PHONE_CB
                )
            ]
        ]
    )


async def ask_customer_name(
    message: Message,
    state: FSMContext,
    *,
    flow: str,
    note: str | None = None,
    **extra: object,
) -> None:
    """Start the two-step label. ``note`` is appended to the first prompt."""
    await state.update_data(label_flow=flow, rs_customer_name=None, **extra)
    await state.set_state(ResellerStates.waiting_customer_name)
    body = texts.reseller_label_name_prompt
    if note:
        body = f"{body}\n\n{note}"
    await message.answer(body)


@router.message(ResellerStates.waiting_customer_name)
async def customer_name_entered(message: Message, state: FSMContext) -> None:
    try:
        name = sanitize_customer_name(message.text or "")
    except ValueError:
        await message.answer(texts.reseller_label_name_invalid)
        return
    await state.update_data(rs_customer_name=name)
    await state.set_state(ResellerStates.waiting_customer_phone)
    await message.answer(
        texts.reseller_label_phone_prompt.format(name=name),
        reply_markup=skip_phone_keyboard(),
    )


@router.message(ResellerStates.waiting_customer_phone)
async def customer_phone_entered(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    try:
        phone = normalize_phone(message.text or "")
    except ValueError:
        await message.answer(texts.reseller_label_phone_invalid, reply_markup=skip_phone_keyboard())
        return
    await _finish(message, state, session, settings, db_user, phone=phone)


@router.callback_query(ResellerStates.waiting_customer_phone, F.data == SKIP_PHONE_CB)
async def customer_phone_skipped(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    message = callback.message
    if not isinstance(message, Message):
        # The chat this started in is older than Telegram will let the bot
        # touch. Say so out loud: a «نمی‌خوام» that does nothing at all reads
        # as the bot being broken, and the shop has a customer waiting.
        await state.clear()
        await callback.answer(texts.stale_button, show_alert=True)
        return
    await callback.answer()
    await _finish(message, state, session, settings, db_user, phone=None)


async def _finish(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    *,
    phone: str | None,
) -> None:
    """Hand the finished label to whichever flow asked for it."""
    data = await state.get_data()
    name = str(data.get("rs_customer_name") or "").strip()
    if not name:
        # The state outlived its data — a restart, or a stale button. Start
        # over rather than filing a service under an empty name.
        await state.clear()
        await message.answer(texts.stale_button)
        return
    # Both halves travel, not just the composed string: the name seeds the
    # panel username and the phone must not.
    customer = CustomerLabel(name=name, phone=phone)
    flow = str(data.get("label_flow") or "")

    if flow == FLOW_PURCHASE:
        from bot.handlers.purchase import continue_with_customer_label

        await continue_with_customer_label(
            message, state, session, settings, db_user, customer=customer
        )
        return
    if flow == FLOW_TRIAL:
        from bot.handlers.referral import continue_trial_with_customer_label

        await continue_trial_with_customer_label(
            message, state, session, settings, db_user, customer=customer
        )
        return
    if flow == FLOW_RENAME:
        from bot.handlers.account import continue_rename_with_customer_label

        await continue_rename_with_customer_label(
            message, state, session, settings, db_user, customer=customer
        )
        return

    logger.warning("customer label finished with no flow: %r", flow)
    await state.clear()
    await message.answer(texts.stale_button)

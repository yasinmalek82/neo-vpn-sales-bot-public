"""«⚙️ تنظیمات» → «📣 پیام‌های بازاریابی» (admin).

One screen: the four campaigns, each a toggle that says its state. Seeing the
screen is any admin's; switching a campaign is the store owner's, like the
lifecycle numbers. Switching on stores the moment, and a campaign acts only on
what happens after it. Every control here is read by a job
(``services/marketing_run.py``, ``services/volume_warnings.py``).

A personal offer (percent, then hours) is typed under three of the campaigns, never
the cart: the owner taps its button, the FSM state ``MarketingStates`` waits for one
number, and ``services/marketing_settings.py`` checks the range where it is typed.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.handlers.admin import _deny_if_not_admin, _is_admin, _tg_message
from bot.handlers.common import edit_or_send
from bot.keyboards.marketing import (
    CAMPAIGN_CODES,
    OFFER_NAMES,
    MktCB,
    marketing_input_keyboard,
    marketing_keyboard,
)
from bot.states import MarketingStates
from bot.texts import texts
from database.models import User
from database.models.marketing import MAX_OFFER_HOURS, MAX_OFFER_PERCENT, OFFER_CAMPAIGNS
from services.marketing_settings import (
    MarketingSettingsError,
    load_config,
    parse_offer_hours,
    parse_offer_percent,
    set_campaign,
    set_offer_hours,
    set_offer_percent,
)
from services.marketing_view import screen_text
from services.store_config import is_store_owner
from services.ux_format import to_persian_digits

router = Router(name="marketing_admin")


def _fa(n: int) -> str:
    return to_persian_digits(n)


def _can_edit(settings: Settings, telegram_id: int | None) -> bool:
    return is_store_owner(settings, telegram_id)


async def _show_screen(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    *,
    telegram_id: int | None,
    prefix: str = "",
) -> None:
    config = await load_config(session)
    body = screen_text(config)
    if prefix:
        body = f"{prefix}\n\n{body}"
    await edit_or_send(
        message, body, marketing_keyboard(config, can_edit=_can_edit(settings, telegram_id))
    )


@router.callback_query(F.data == MktCB.OPEN)
async def marketing_open(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await _show_screen(
            message,
            session,
            settings,
            telegram_id=callback.from_user.id if callback.from_user else None,
        )


@router.callback_query(F.data.startswith(MktCB.TOGGLE))
async def marketing_toggle(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    tg_id = callback.from_user.id if callback.from_user else None
    if not _can_edit(settings, tg_id):
        await callback.answer(texts.admin_mkt_not_owner, show_alert=True)
        return
    campaign = CAMPAIGN_CODES.get((callback.data or "")[len(MktCB.TOGGLE) :])
    if campaign is None:
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    await state.clear()
    config = await load_config(session)
    try:
        await set_campaign(session, campaign=campaign, on=not config.is_on(campaign))
    except MarketingSettingsError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await session.commit()
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await _show_screen(
            message, session, settings, telegram_id=tg_id, prefix=texts.admin_mkt_saved
        )


# --- the personal offer's two numbers ---------------------------------------


def _offer_campaign(data: str | None, prefix: str) -> str | None:
    """The campaign a percent/hours button names, only if it may carry an offer."""
    campaign = CAMPAIGN_CODES.get((data or "")[len(prefix) :])
    return campaign if campaign in OFFER_CAMPAIGNS else None


async def _open_offer_prompt(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    *,
    prefix: str,
    waiting: State,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    tg_id = callback.from_user.id if callback.from_user else None
    if not _can_edit(settings, tg_id):
        await callback.answer(texts.admin_mkt_not_owner, show_alert=True)
        return
    campaign = _offer_campaign(callback.data, prefix)
    if campaign is None:
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    await state.clear()
    await state.set_state(waiting)
    await state.update_data(mkt_campaign=campaign)
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    config = await load_config(session)
    if waiting == MarketingStates.waiting_offer_percent:
        percent = config.offer_percent.get(campaign, 0)
        body = texts.admin_mkt_offer_percent_prompt.format(
            campaign=OFFER_NAMES[campaign],
            high=_fa(MAX_OFFER_PERCENT),
            current=f"{_fa(percent)}٪" if percent > 0 else texts.admin_mkt_offer_none,
        )
    else:
        body = texts.admin_mkt_offer_hours_prompt.format(
            campaign=OFFER_NAMES[campaign],
            high=_fa(MAX_OFFER_HOURS),
            current=_fa(config.offer_hours.get(campaign, 0)),
        )
    await edit_or_send(message, body, marketing_input_keyboard())


@router.callback_query(F.data.startswith(MktCB.PERCENT))
async def marketing_offer_percent(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    await _open_offer_prompt(
        callback,
        state,
        session,
        settings,
        db_user,
        prefix=MktCB.PERCENT,
        waiting=MarketingStates.waiting_offer_percent,
    )


@router.callback_query(F.data.startswith(MktCB.HOURS))
async def marketing_offer_hours(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    await _open_offer_prompt(
        callback,
        state,
        session,
        settings,
        db_user,
        prefix=MktCB.HOURS,
        waiting=MarketingStates.waiting_offer_hours,
    )


async def _typed_offer_value(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    *,
    hours: bool,
) -> None:
    tg_id = message.from_user.id if message.from_user else None
    if not _is_admin(tg_id, settings, db_user) or not _can_edit(settings, tg_id):
        await state.clear()
        await message.answer(texts.admin_mkt_not_owner, reply_markup=marketing_input_keyboard())
        return
    campaign = (await state.get_data()).get("mkt_campaign")
    if campaign not in OFFER_CAMPAIGNS:
        await state.clear()
        await message.answer(texts.admin_action_error, reply_markup=marketing_input_keyboard())
        return
    raw = message.text or ""
    try:
        if hours:
            await set_offer_hours(session, campaign=str(campaign), hours=parse_offer_hours(raw))
        else:
            await set_offer_percent(
                session, campaign=str(campaign), percent=parse_offer_percent(raw)
            )
    except MarketingSettingsError as exc:
        # Stay in the state: the next message is another try.
        await message.answer(
            texts.admin_mkt_offer_input_error.format(reason=exc),
            reply_markup=marketing_input_keyboard(),
        )
        return
    await session.commit()
    await state.clear()
    await _show_screen(message, session, settings, telegram_id=tg_id, prefix=texts.admin_mkt_saved)


@router.message(MarketingStates.waiting_offer_percent)
async def marketing_typed_percent(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    await _typed_offer_value(message, state, session, settings, db_user, hours=False)


@router.message(MarketingStates.waiting_offer_hours)
async def marketing_typed_hours(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    await _typed_offer_value(message, state, session, settings, db_user, hours=True)

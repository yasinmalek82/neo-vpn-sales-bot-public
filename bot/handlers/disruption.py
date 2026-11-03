"""«📡 گزارش اختلال»: ISP, then city, then one row for the owner to count.

Two taps for the common case (ISP button, city button). A city not on the list
is typed. The bot adds which services the customer has, so the owner can tell a
direct outage from a tunnel one without asking.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.keyboards.disruption import (
    DisruptionCB,
    city_keyboard,
    done_keyboard,
    isp_keyboard,
)
from bot.states import DisruptionStates
from bot.texts import texts
from database.models import User
from services.brand import with_support
from services.disruption import CITIES, DisruptionError, isp_label, record_disruption

logger = logging.getLogger(__name__)
router = Router(name="disruption")


def _tg_message(callback: CallbackQuery) -> Message | None:
    return callback.message if isinstance(callback.message, Message) else None


@router.callback_query(F.data == DisruptionCB.START)
async def disruption_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    message = _tg_message(callback)
    if message is not None:
        await message.answer(texts.disruption_pick_isp, reply_markup=isp_keyboard())


@router.callback_query(F.data.startswith(DisruptionCB.ISP_PREFIX))
async def disruption_isp(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    message = _tg_message(callback)
    isp = (callback.data or "").removeprefix(DisruptionCB.ISP_PREFIX)
    if message is None:
        return
    if isp_label(isp) is None:
        await message.answer(texts.disruption_lost, reply_markup=done_keyboard())
        return
    await state.clear()
    await state.update_data(disruption_isp=isp)
    await message.answer(texts.disruption_pick_city, reply_markup=city_keyboard())


@router.callback_query(F.data == DisruptionCB.CITY_WRITE)
async def disruption_write_city(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    if not (await state.get_data()).get("disruption_isp"):
        await message.answer(texts.disruption_lost, reply_markup=done_keyboard())
        return
    await state.set_state(DisruptionStates.waiting_city)
    await message.answer(texts.disruption_write_city, reply_markup=done_keyboard())


@router.callback_query(F.data.startswith(DisruptionCB.CITY_PREFIX))
async def disruption_city_button(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    await callback.answer()
    message = _tg_message(callback)
    raw = (callback.data or "").removeprefix(DisruptionCB.CITY_PREFIX)
    if message is None:
        return
    if not raw.isdigit() or int(raw) >= len(CITIES):
        await message.answer(texts.disruption_lost, reply_markup=done_keyboard())
        return
    await _save(message, state, session, settings, db_user, CITIES[int(raw)])


@router.message(DisruptionStates.waiting_city)
async def disruption_city_typed(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    await _save(message, state, session, settings, db_user, message.text or "")


async def _save(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    city: str,
) -> None:
    isp = (await state.get_data()).get("disruption_isp")
    if db_user is None or not isp:
        await state.clear()
        await message.answer(texts.disruption_lost, reply_markup=done_keyboard())
        return
    try:
        await record_disruption(session, user=db_user, isp=str(isp), city=city)
        await session.commit()
    except DisruptionError as exc:
        await session.rollback()
        code = str(exc)
        if code == "bad_city":
            # Stay in the state: the customer only has to retype the city.
            await message.answer(texts.disruption_bad_city, reply_markup=done_keyboard())
            return
        await state.clear()
        if code == "too_soon":
            await message.answer(
                with_support(texts.disruption_too_soon, settings), reply_markup=done_keyboard()
            )
        else:
            await message.answer(texts.disruption_lost, reply_markup=done_keyboard())
        return
    await state.clear()
    await message.answer(texts.disruption_thanks, reply_markup=done_keyboard())

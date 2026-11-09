"""Admin customer browsing: list, filters, search, and opening a card.

The card and admin access checks stay shared with the existing admin router;
financial actions and service changes intentionally remain there for now.
"""

from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.handlers.admin import (
    _edit_or_answer_admin,
    _show_customer,
    _tg_message,
)
from bot.handlers.admin_access import _deny_if_not_admin, _is_admin
from bot.keyboards.admin import (
    AdminCB,
    admin_back_home_keyboard,
    admin_cancel_input_keyboard,
    customer_results_keyboard,
    users_list_keyboard,
)
from bot.states import AdminStates
from bot.texts import texts
from database.models import User
from services.customer_lookup import (
    USER_FILTER_LABELS,
    ManualAdjustError,
    UserFilter,
    find_customers,
    list_users,
)
from services.ux_format import to_persian_digits

router = Router(name="admin_customers")

_USER_FILTER_ROW = [(f.value, USER_FILTER_LABELS[f]) for f in UserFilter]


async def _show_users_page(
    message: Message,
    session: AsyncSession,
    offset: int,
    user_filter: UserFilter = UserFilter.ALL,
) -> None:
    page = await list_users(session, offset=offset, user_filter=user_filter)
    keyboard = users_list_keyboard(page, filters=_USER_FILTER_ROW)
    if not page.rows:
        # «هیچ کاربری با این فیلتر نیست» is a different fact from «هنوز کاربری
        # نداریم», and an owner who confuses the two thinks the panel is broken.
        body = (
            texts.admin_users_empty
            if user_filter is UserFilter.ALL
            else texts.admin_users_filter_empty.format(label=USER_FILTER_LABELS[user_filter])
        )
        await _edit_or_answer_admin(message, body, keyboard)
        return
    body = texts.admin_users_title.format(
        label=USER_FILTER_LABELS[user_filter],
        total=to_persian_digits(page.total),
        first=to_persian_digits(page.offset + 1),
        last=to_persian_digits(page.offset + len(page.rows)),
    )
    await _edit_or_answer_admin(message, body, keyboard)


@router.callback_query(F.data == AdminCB.CUSTOMER)
async def admin_users_list(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """The section opens on a **list** now.

    It used to jump straight to a search prompt, so an admin who did not
    already know a name or id had no way in — and no way to answer «چند تا
    کاربر داریم؟». Search is still here, one tap inside the list.
    """
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _show_users_page(message, session, 0)


def _parse_user_filter(raw: str) -> UserFilter:
    """Unknown or stale values fall back to «همه» rather than erroring — a
    filter from an older version of the keyboard should still open a screen."""
    try:
        return UserFilter(raw)
    except ValueError:
        return UserFilter.ALL


@router.callback_query(F.data.startswith(AdminCB.CUSTOMER_PAGE))
async def admin_users_page(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(AdminCB.CUSTOMER_PAGE) :]
    filter_value, _, offset_raw = raw.partition(":")
    if not offset_raw.isdigit():
        await callback.answer(texts.admin_panel_invalid, show_alert=True)
        return
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _show_users_page(message, session, int(offset_raw), _parse_user_filter(filter_value))


@router.callback_query(F.data.startswith(AdminCB.CUSTOMER_FILTER))
async def admin_users_filter(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """Switching filter starts at the top: staying on page 3 of a list that now
    has four rows would show an empty screen and look broken."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(AdminCB.CUSTOMER_FILTER) :]
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _show_users_page(message, session, 0, _parse_user_filter(raw))


@router.callback_query(F.data == AdminCB.CUSTOMER_SEARCH)
async def admin_customer_start(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_customer_query)
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _edit_or_answer_admin(message, texts.admin_customer_prompt, admin_cancel_input_keyboard())


@router.message(AdminStates.waiting_customer_query)
async def admin_customer_search(
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
    query = (message.text or "").strip()
    matches = await find_customers(session, query)
    if not matches:
        await message.answer(
            texts.admin_customer_none.format(query=escape(query)),
            reply_markup=admin_cancel_input_keyboard(),
        )
        return
    await state.clear()
    if len(matches) == 1:
        await _show_customer(message, session, int(matches[0].id))
        return
    await message.answer(
        texts.admin_customer_results.format(
            count=to_persian_digits(len(matches)), query=escape(query)
        ),
        reply_markup=customer_results_keyboard(matches),
    )


@router.callback_query(F.data.startswith(AdminCB.CUSTOMER_PICK))
async def admin_customer_pick(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(AdminCB.CUSTOMER_PICK) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_invalid_id, show_alert=True)
        return
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    try:
        await _show_customer(message, session, int(raw))
    except ManualAdjustError as exc:
        await _edit_or_answer_admin(message, str(exc), admin_back_home_keyboard())

"""Admin customer desk: link an existing VPN identity after explicit review."""

from dataclasses import asdict
from html import escape
from secrets import token_hex

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from api_client.factory import get_pasarguard_client
from bot.config import Settings
from bot.handlers.admin import _deny_if_not_admin, _is_admin, _tg_message
from bot.keyboards.account_link import LinkCB, account_link_keyboard
from bot.states import AccountLinkStates
from bot.texts import texts
from database.base import end_read_transaction
from database.models import User
from services.account_linking import (
    AccountLinkPreview,
    LinkError,
    confirm_account_link,
    preview_account_link,
)
from services.panel_settings import panel_error_reason_fa
from services.ux_format import to_persian_digits

router = Router(name="account_link")


def _error(exc: Exception) -> str:
    if isinstance(exc, LinkError):
        return {
            "other_owner": texts.admin_existing_link_other_owner,
            "identity_conflict": texts.admin_existing_link_other_owner,
            "infrastructure_account": texts.admin_existing_link_infrastructure,
            "invalid_username": texts.admin_existing_link_invalid_name,
            "account_archived": texts.admin_existing_link_archived,
            "panel_snapshot_changed": texts.admin_existing_link_changed,
        }.get(str(exc), texts.admin_existing_link_failed)
    return panel_error_reason_fa(exc)


@router.callback_query(F.data.startswith(LinkCB.START))
async def start_account_link(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "").removeprefix(LinkCB.START)
    if not raw.isascii() or not raw.isdecimal():
        await callback.answer(texts.admin_panel_invalid, show_alert=True)
        return
    user_id = int(raw)
    user = await session.get(User, user_id)
    await end_read_transaction(session)
    if user is None:
        await callback.answer(texts.admin_customer_not_found, show_alert=True)
        return
    await callback.answer()
    await state.clear()
    await state.set_state(AccountLinkStates.waiting_name)
    await state.update_data(link_user_id=user_id)
    message = _tg_message(callback)
    if message is not None:
        await message.answer(
            texts.admin_existing_link_ask, reply_markup=account_link_keyboard(user_id)
        )


@router.message(AccountLinkStates.waiting_name)
async def read_account_link_name(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, settings, db_user):
        await message.answer(texts.admin_only, reply_markup=account_link_keyboard())
        return
    data = await state.get_data()
    user_id = data.get("link_user_id")
    if not user_id:
        await state.clear()
        await message.answer(
            texts.admin_existing_link_expired, reply_markup=account_link_keyboard()
        )
        return
    try:
        client = await get_pasarguard_client(session, settings)
        try:
            preview = await preview_account_link(
                session,
                client,
                settings,
                actor_telegram_id=message.from_user.id,
                user_id=int(user_id),
                panel_username=message.text or "",
            )
        finally:
            await client.aclose()
    except Exception as exc:
        await session.rollback()
        await message.answer(_error(exc), reply_markup=account_link_keyboard(int(user_id)))
        return
    nonce = token_hex(8)
    await state.update_data(
        link_preview=asdict(preview), link_preview_sha=preview.sha256, link_nonce=nonce
    )
    await message.answer(
        texts.admin_existing_link_preview.format(
            username=escape(preview.panel_username),
            telegram_id=to_persian_digits(preview.telegram_id),
        ),
        reply_markup=account_link_keyboard(int(user_id), nonce=nonce),
    )


@router.callback_query(F.data.startswith(LinkCB.CONFIRM))
async def confirm_existing_account_link(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    data = await state.get_data()
    raw = (callback.data or "").removeprefix(LinkCB.CONFIRM)
    expected = f"{data.get('link_user_id')}:{data.get('link_nonce')}"
    if raw != expected or not data.get("link_preview"):
        await callback.answer(texts.admin_existing_link_expired, show_alert=True)
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is None or callback.from_user is None:
        return
    user_id = int(data["link_user_id"])
    try:
        preview = AccountLinkPreview(**data["link_preview"])
        client = await get_pasarguard_client(session, settings)
        try:
            await confirm_account_link(
                session,
                client,
                settings,
                actor_telegram_id=callback.from_user.id,
                preview=preview,
                confirm_sha256=str(data["link_preview_sha"]),
            )
        finally:
            await client.aclose()
        await session.commit()
    except Exception as exc:
        await session.rollback()
        await message.answer(_error(exc), reply_markup=account_link_keyboard(user_id))
        return
    await state.clear()
    await message.answer(
        texts.admin_existing_link_done, reply_markup=account_link_keyboard(user_id)
    )

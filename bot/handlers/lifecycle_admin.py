"""«⚙️ تنظیمات» → «🗓 هشدار و حذف سرویس» (admin).

One screen with a status block and one button per number. A number is typed
(FSM state ``LifecycleStates.waiting_value``) and checked where it is typed,
with the range stated. Changing the numbers is the store owner's; another admin
sees the screen. «روشن» goes through a confirm and is refused by
``services/lifecycle_settings.set_delete_mode`` unless a rehearsal reported
within the last seven days. Every control here is read by a job
(``scheduler/jobs``): there is no toggle nothing consumes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from api_client.factory import get_pasarguard_client
from bot.config import Settings
from bot.handlers.admin import _deny_if_not_admin, _is_admin, _render_service, _tg_message
from bot.handlers.common import edit_or_send
from bot.keyboards.lifecycle import (
    FIELD_CODES,
    LIST_PAGE_SIZE,
    LifeCB,
    approve_confirm_keyboard,
    batch_list_keyboard,
    card_flow_rows,
    lifecycle_back_keyboard,
    lifecycle_input_keyboard,
    lifecycle_keyboard,
    lifecycle_mode_confirm_keyboard,
)
from bot.states import LifecycleStates
from bot.texts import texts
from database.base import end_read_transaction, get_session_factory
from database.models import PasarGuardAccount, User
from database.models.lifecycle import DELETE_MODE_OFF, DELETE_MODE_ON, DELETE_MODE_REHEARSAL
from services.accounts import get_live_account
from services.lifecycle_batches import (
    BATCH_KIND_PREVIEW,
    batch_rows,
    current_batch,
    get_batch,
    item_count,
    void_reason,
)
from services.lifecycle_run import (
    ApproveResult,
    BatchRefused,
    approve_batch,
    build_preview,
    deleted_line,
    make_batch_from_preview,
    skip_reason_fa,
)
from services.lifecycle_settings import (
    SettingsError,
    load_config,
    parse_field,
    rehearsal_is_fresh,
    save_number,
    saved_line,
    set_delete_mode,
)
from services.lifecycle_settings import can_edit as owner_can_edit
from services.lifecycle_view import current_value_text, screen_text
from services.service_ops import ServiceOpError
from services.ux_format import format_jalali, to_persian_digits

router = Router(name="lifecycle_admin")


def _fa(n: int) -> str:
    return to_persian_digits(n)


def _can_edit(settings: Settings, telegram_id: int | None) -> bool:
    """The numbers decide what is deleted from the panel: the owner changes them."""
    return owner_can_edit(settings, telegram_id)


async def _show_screen(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    *,
    telegram_id: int | None,
    prefix: str = "",
) -> None:
    config = await load_config(session, seed_days=settings.renewal_reminder_days)
    body = screen_text(config)
    waiting = await current_batch(session)
    batch: tuple[int, int] | None = None
    if waiting is not None:
        count = await item_count(session, int(waiting.id))
        batch = (int(waiting.id), count)
        body += texts.admin_life_batch_line.format(n=_fa(count))
    if prefix:
        body = f"{prefix}\n\n{body}"
    await edit_or_send(
        message,
        body,
        lifecycle_keyboard(config, can_edit=_can_edit(settings, telegram_id), batch=batch),
    )


@router.callback_query(F.data == LifeCB.OPEN)
async def lifecycle_open(
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


async def _deny_if_not_owner(callback: CallbackQuery, settings: Settings) -> bool:
    tg_id = callback.from_user.id if callback.from_user else None
    if _can_edit(settings, tg_id):
        return False
    await callback.answer(texts.admin_life_not_owner, show_alert=True)
    return True


@router.callback_query(F.data.startswith(LifeCB.EDIT))
async def lifecycle_edit(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner(callback, settings):
        return
    field = FIELD_CODES.get((callback.data or "")[len(LifeCB.EDIT) :])
    if field is None:
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    await state.clear()
    await state.set_state(LifecycleStates.waiting_value)
    await state.update_data(life_field=field)
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    config = await load_config(session, seed_days=settings.renewal_reminder_days)
    await edit_or_send(
        message,
        texts.admin_life_prompts[field].format(current=current_value_text(field, config)),
        lifecycle_input_keyboard(),
    )


@router.message(LifecycleStates.waiting_value)
async def lifecycle_typed_value(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    tg_id = message.from_user.id if message.from_user else None
    if not _is_admin(tg_id, settings, db_user) or not _can_edit(settings, tg_id):
        await state.clear()
        await message.answer(texts.admin_life_not_owner, reply_markup=lifecycle_back_keyboard())
        return
    field = (await state.get_data()).get("life_field")
    if field not in FIELD_CODES.values():
        await state.clear()
        await message.answer(texts.admin_action_error, reply_markup=lifecycle_back_keyboard())
        return
    raw = message.text or ""
    before = await load_config(session, seed_days=settings.renewal_reminder_days)
    was_on = before.delete_mode == DELETE_MODE_ON
    try:
        value = parse_field(str(field), raw)
        await save_number(
            session, seed_days=settings.renewal_reminder_days, field=str(field), value=value
        )
    except SettingsError as exc:
        # Stay in the state: the next message is another try.
        await message.answer(
            texts.admin_life_input_error.format(reason=exc), reply_markup=lifecycle_input_keyboard()
        )
        return
    await session.commit()
    await state.clear()
    prefix = saved_line(str(field), was_on=was_on)
    await _show_screen(message, session, settings, telegram_id=tg_id, prefix=prefix)


async def _apply_mode(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    mode: str,
) -> None:
    message = _tg_message(callback)
    try:
        await set_delete_mode(session, seed_days=settings.renewal_reminder_days, mode=mode)
    except SettingsError as exc:
        await callback.answer()
        if message is not None:
            await edit_or_send(message, f"❌ {exc}", lifecycle_back_keyboard())
        return
    await session.commit()
    await callback.answer()
    if message is not None:
        await _show_screen(
            message,
            session,
            settings,
            telegram_id=callback.from_user.id if callback.from_user else None,
            prefix=texts.admin_life_mode_set.format(mode=texts.admin_life_mode_names[mode]),
        )


@router.callback_query(F.data.startswith(LifeCB.MODE))
async def lifecycle_mode(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner(callback, settings):
        return
    await state.clear()
    mode = (callback.data or "")[len(LifeCB.MODE) :]
    if mode == DELETE_MODE_ON:
        # Refuse before the confirm: a button that asks «are you sure?» and
        # then refuses is worse than one that says why up front.
        config = await load_config(session, seed_days=settings.renewal_reminder_days)
        message = _tg_message(callback)
        if config.delete_mode == DELETE_MODE_ON:
            await callback.answer()
            return
        if not rehearsal_is_fresh(config, _now()):
            await _apply_mode(callback, session, settings, DELETE_MODE_ON)  # says why
            return
        await callback.answer()
        if message is not None:
            await edit_or_send(
                message, texts.admin_life_mode_confirm, lifecycle_mode_confirm_keyboard()
            )
        return
    if mode not in (DELETE_MODE_OFF, DELETE_MODE_REHEARSAL):
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    await _apply_mode(callback, session, settings, mode)


@router.callback_query(F.data == LifeCB.MODE_ON_OK)
async def lifecycle_mode_on_confirmed(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner(callback, settings):
        return
    await state.clear()
    await _apply_mode(callback, session, settings, DELETE_MODE_ON)


def _now():  # noqa: ANN202 - one place to stub the clock

    return datetime.now(UTC)


# --- the deletion list, the preview and the approval ----------------------------------------------


def _split(raw: str) -> list[int] | None:
    try:
        return [int(part) for part in raw.split(":")]
    except ValueError:
        return None


async def _show_list(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    batch_id: int,
    page: int,
    *,
    prefix: str = "",
) -> None:
    batch = await get_batch(session, batch_id)
    if batch is None:
        await edit_or_send(message, texts.admin_life_refused["missing"], lifecycle_back_keyboard())
        return
    config = await load_config(session, seed_days=settings.renewal_reminder_days)
    preview = batch.kind == BATCH_KIND_PREVIEW
    problem = void_reason(batch, config, _now())
    total = await item_count(session, batch_id)
    deletes = await item_count(session, batch_id, action="delete")
    warns = total - deletes
    pages = max(1, -(-total // LIST_PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    rows = await batch_rows(session, batch_id, offset=page * LIST_PAGE_SIZE, limit=LIST_PAGE_SIZE)
    page_text = texts.admin_life_list_page.format(page=_fa(page + 1), pages=_fa(pages))
    if total == 0:
        body = texts.admin_life_list_empty
    elif preview:
        body = texts.admin_life_preview_head.format(
            deletes=_fa(deletes), warns=_fa(warns), pages=page_text
        )
    else:
        body = texts.admin_life_list_head.format(
            deletes=_fa(deletes),
            created=format_jalali(batch.created_at, persian_digits=True),
            pages=page_text,
        )
    if problem is not None and not preview:
        body += texts.admin_life_list_void.format(why=texts.admin_life_void_reasons[problem])
    if prefix:
        body = f"{prefix}\n\n{body}"
    await edit_or_send(
        message,
        body,
        batch_list_keyboard(
            rows,
            batch_id=batch_id,
            page=page,
            total=total,
            delete_count=deletes,
            warn_count=warns,
            approvable=problem is None,
            voided=problem is not None and not preview,
            preview=preview,
        ),
    )


async def _owner_gate(callback: CallbackQuery, settings: Settings, db_user: User | None) -> bool:
    """True when the press must stop here (not an admin, or not the owner)."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return True
    return await _deny_if_not_owner(callback, settings)


@router.callback_query(F.data == LifeCB.PREVIEW)
async def lifecycle_preview(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """Today's decisions, read live from the panel. Deletes and sends nothing."""
    if await _owner_gate(callback, settings, db_user):
        return
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    config = await load_config(session, seed_days=settings.renewal_reminder_days)
    await edit_or_send(message, texts.admin_life_preview_working, lifecycle_back_keyboard())
    client = await get_pasarguard_client(session, settings)
    await end_read_transaction(session)
    try:
        batch_id = await build_preview(get_session_factory(), client, config, _now())
    finally:
        await client.aclose()
    await _show_list(message, session, settings, batch_id, 0)


@router.callback_query(F.data.startswith(LifeCB.LIST))
async def lifecycle_list(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _owner_gate(callback, settings, db_user):
        return
    parts = _split((callback.data or "")[len(LifeCB.LIST) :])
    if parts is None or len(parts) != 2:
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await _show_list(message, session, settings, parts[0], parts[1])


@router.callback_query(F.data.startswith(LifeCB.ROW))
async def lifecycle_row(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """A row of the list opens that service's existing admin card, live from the panel."""
    if await _owner_gate(callback, settings, db_user):
        return
    parts = _split((callback.data or "")[len(LifeCB.ROW) :])
    if parts is None or len(parts) != 3:
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    batch_id, account_id, page = parts
    await state.clear()
    account = await get_live_account(session, account_id)
    if account is None:
        await callback.answer(texts.admin_life_skip_reasons["gone"], show_alert=True)
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    try:
        await _render_service(
            message,
            session,
            settings,
            account_id,
            extra_rows=card_flow_rows(
                batch_id, account_id, page, exempt=bool(account.delete_exempt)
            ),
        )
    except ServiceOpError as exc:
        await edit_or_send(message, str(exc), lifecycle_back_keyboard())


@router.callback_query(F.data.startswith(LifeCB.EXEMPT))
async def lifecycle_exempt(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """«🛡 حذف نشود» / «↩️ حذف شود»: a per-service exemption the job always honours."""
    if await _owner_gate(callback, settings, db_user):
        return
    parts = _split((callback.data or "")[len(LifeCB.EXEMPT) :])
    if parts is None or len(parts) != 3:
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    batch_id, account_id, page = parts
    account = await get_live_account(session, account_id)
    if account is None:
        await callback.answer(texts.admin_life_skip_reasons["gone"], show_alert=True)
        return
    await session.execute(
        update(PasarGuardAccount)
        .where(PasarGuardAccount.id == account_id)
        .values(delete_exempt=not bool(account.delete_exempt))
    )
    await session.commit()
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    fresh = await get_live_account(session, account_id)
    try:
        await _render_service(
            message,
            session,
            settings,
            account_id,
            extra_rows=card_flow_rows(
                batch_id, account_id, page, exempt=bool(fresh.delete_exempt if fresh else False)
            ),
        )
    except ServiceOpError as exc:
        await edit_or_send(message, str(exc), lifecycle_back_keyboard())


def _refusal(code: str) -> str:
    return texts.admin_life_refused.get(code, texts.admin_life_refused["missing"])


@router.callback_query(F.data.startswith(LifeCB.APPROVE_OK))
async def lifecycle_approve_confirmed(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    bot: Bot,
) -> None:
    """Delete ONLY this list's ids, each re-read and re-decided right before its delete."""
    if await _owner_gate(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(LifeCB.APPROVE_OK) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await end_read_transaction(session)
    try:
        result = await approve_batch(get_session_factory(), bot, settings, int(raw))
    except BatchRefused as refused:
        await edit_or_send(message, _refusal(refused.code), lifecycle_back_keyboard())
        return
    await edit_or_send(message, _result_text(result), lifecycle_back_keyboard())


def _result_text(result: ApproveResult) -> str:
    body = texts.admin_life_approve_result.format(
        deleted=_fa(len(result.deleted)),
        skipped=_fa(len(result.skipped)),
        warned=_fa(len(result.warned)),
    )
    if result.deleted:
        lines = [deleted_line(o) for o in result.deleted[:20]]
        if len(result.deleted) > 20:
            lines.append(texts.admin_life_report_more.format(n=_fa(len(result.deleted) - 20)))
        body += "\n\n" + texts.admin_life_result_deleted_head + "\n" + "\n".join(lines)
    if result.skipped:
        lines = [
            texts.admin_life_result_row.format(
                title=o.title or "—", reason=skip_reason_fa(o.reason)
            )
            for o in result.skipped[:20]
        ]
        if len(result.skipped) > 20:
            lines.append(texts.admin_life_report_more.format(n=_fa(len(result.skipped) - 20)))
        body += "\n\n" + texts.admin_life_result_skipped_head + "\n" + "\n".join(lines)
    if result.warned:
        lines = [
            texts.admin_life_result_row.format(
                title=o.title or "—", reason=skip_reason_fa(o.reason)
            )
            for o in result.warned[:20]
        ]
        body += "\n\n" + texts.admin_life_result_warned_head + "\n" + "\n".join(lines)
    return body


@router.callback_query(F.data.startswith(LifeCB.APPROVE))
async def lifecycle_approve_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """The confirm screen: it repeats the count. Nothing is deleted here."""
    if await _owner_gate(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(LifeCB.APPROVE) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    batch = await get_batch(session, int(raw))
    config = await load_config(session, seed_days=settings.renewal_reminder_days)
    if batch is None:
        await edit_or_send(message, _refusal("missing"), lifecycle_back_keyboard())
        return
    problem = void_reason(batch, config, _now())
    if problem is None and config.delete_mode != "on":
        problem = "mode"
    if problem is not None:
        await edit_or_send(message, _refusal(problem), lifecycle_back_keyboard())
        return
    count = await item_count(session, int(batch.id), action="delete")
    if count == 0:
        count = await item_count(session, int(batch.id))
        body = texts.admin_life_approve_warn_confirm.format(n=_fa(count))
    else:
        body = texts.admin_life_approve_confirm.format(n=_fa(count))
    await edit_or_send(message, body, approve_confirm_keyboard(int(batch.id), count))


@router.callback_query(F.data.startswith(LifeCB.FRESH))
async def lifecycle_fresh(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """Build a new approvable list from today's live decisions (the owner's own act)."""
    if await _owner_gate(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(LifeCB.FRESH) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    config = await load_config(session, seed_days=settings.renewal_reminder_days)
    source = await get_batch(session, int(raw))
    await end_read_transaction(session)
    factory = get_session_factory()
    preview_id = int(raw)
    if source is None or source.kind != BATCH_KIND_PREVIEW:
        # A void list: today's decisions are read again; nothing old is reused.
        await edit_or_send(message, texts.admin_life_preview_working, lifecycle_back_keyboard())
        client = await get_pasarguard_client(session, settings)
        await end_read_transaction(session)
        try:
            preview_id = await build_preview(factory, client, config, _now())
        finally:
            await client.aclose()
    batch_id = await make_batch_from_preview(factory, preview_id, config, _now())
    await _show_list(message, session, settings, batch_id, 0, prefix=texts.admin_life_fresh_done)

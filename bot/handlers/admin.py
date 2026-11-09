"""Admin panel + Phase 4 payment approve/reject callbacks."""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from html import escape
from typing import Any

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api_client.client import PasarGuardClient
from api_client.exceptions import PasarGuardNotFoundError
from api_client.factory import get_pasarguard_client
from api_client.models import GroupResponse, GroupSimple
from bot.admin_home import admin_home_screen
from bot.config import Settings
from bot.delivery import deliver_provisioned_service_card, service_qr_png
from bot.handlers.admin_access import _deny_if_not_admin, _is_admin
from bot.handlers.common import edit_or_send
from bot.keyboards.admin import (
    PAGE_SIZE,
    AdminCB,
    admin_back_home_keyboard,
    admin_cancel_input_keyboard,
    admin_people_keyboard,
    admin_resellers_keyboard,
    admin_sales_keyboard,
    admin_settings_keyboard,
    announcement_alternative_keyboard,
    announcement_home_keyboard,
    announcement_since_keyboard,
    back_to_customer_keyboard,
    broadcast_audience_keyboard,
    broadcast_confirm_keyboard,
    broadcast_home_keyboard,
    broadcast_progress_keyboard,
    confirm_keyboard,
    customer_ban_confirm_keyboard,
    customer_card_keyboard,
    customer_order_label,
    customer_orders_keyboard,
    customer_referrals_keyboard,
    customer_services_keyboard,
    gift_confirm_keyboard,
    gift_groups_keyboard,
    gift_plans_keyboard,
    gift_retry_keyboard,
    order_detail_keyboard,
    orders_filter_keyboard,
    orders_list_keyboard,
    panel_cancel_keyboard,
    panel_groups_keyboard,
    panel_settings_keyboard,
    panel_status_keyboard,
    parse_order_cb,
    pending_list_keyboard,
    qr_background_keyboard,
    qr_background_reset_keyboard,
    report_group_keyboard,
    reseller_custom_rate_keyboard,
    reseller_matrix_keyboard,
    reseller_panel_keyboard,
    reseller_pct_preview_keyboard,
    reseller_revoke_confirm_keyboard,
    reseller_trial_settings_keyboard,
    service_data_confirm_keyboard,
    service_devices_keyboard,
    service_devices_reset_confirm_keyboard,
    service_link_back_keyboard,
    service_link_keyboard,
    service_link_revoke_confirm_keyboard,
    service_ops_keyboard,
    service_toggle_confirm_keyboard,
)
from bot.keyboards.admin_payment import (
    REJECT_QUICK_REASONS,
    PayCB,
    payment_resolved_keyboard,
    reject_reason_keyboard,
)
from bot.keyboards.customer import provision_failed_keyboard
from bot.keyboards.main_menu import MenuCallback
from bot.keyboards.nav import nav_rows
from bot.keyboards.purchase import parse_duration_cb
from bot.keyboards.store_admin import plan_button_label
from bot.media import answer_stored_media
from bot.states import AdminStates
from bot.texts import texts
from database.base import end_read_transaction
from database.models import Order, OrderStatus, User
from database.models.broadcast import Broadcast, BroadcastAudience, BroadcastState
from database.models.pasarguard_account import PasarGuardAccount
from database.models.payment import PaymentStatus
from database.models.report import TOPIC_KEYS, TOPIC_TITLES_FA
from services.admin import (
    AdminError,
    dashboard_counts,
    get_next_pending_payment,
    get_order_detail,
    list_orders,
    list_pending_payments,
    retry_provision,
    set_user_banned,
    set_user_banned_by_telegram_id,
)
from services.admin_orders import (
    AdminServiceError,
    create_service_for_customer,
    find_order_by_key,
    group_label,
    new_request_key,
    published_catalog_view,
    resolve_offer,
    sellable_groups,
    sellable_plans,
    wallet_option_available,
)
from services.announcements import (
    AnnouncementError,
    clean_field,
    compose_outage,
    compose_restore,
    is_skip,
    now_hhmm,
)
from services.brand import resolve_bot_username, with_signature, with_support
from services.broadcast import (
    AUDIENCE_TITLES_FA,
    MAX_BODY_CHARS,
    BroadcastError,
    BroadcastProgress,
    cancel_broadcast,
    count_audience,
    create_broadcast,
    get_progress,
)
from services.compensation import UnusedValue, unused_value_for_account
from services.customer_card import CardFacts, card_facts, customer_name, role_label
from services.customer_lookup import (
    CustomerCard,
    ManualAdjustError,
    count_customer_orders,
    count_customer_referrals,
    get_customer_card,
    list_customer_orders,
    list_customer_referrals,
    parse_signed_toman,
    reset_trial,
)
from services.finance import is_terminal_wallet_provision_rejection
from services.orders import get_open_order, order_is_delivered
from services.panel_group_list import fetch_panel_groups as _fetch_panel_groups
from services.panel_settings import (
    TRIAL_FIELDS,
    TRIAL_LIMITS,
    check_panel_connection,
    clear_to_env,
    get_effective_pasarguard_config,
    get_settings_row,
    get_trial_hwid_limit,
    public_settings_snapshot,
    save_pasarguard_settings,
    save_trial_shape,
)
from services.panel_status import (
    READ_FAILED_LINE,
    PanelStatusView,
    load_panel_status,
    reconnect_node_result,
)
from services.payments import (
    PaymentError,
    approve_payment,
    reject_payment,
)
from services.provisioning import provision_order
from services.report_group import (
    GroupReadiness,
    ReportGroupError,
    SetupResult,
    get_readiness,
    run_full_setup,
    set_topic,
    verify_topic,
)
from services.report_group import set_enabled as set_reporting_enabled
from services.reporting import get_finance_report
from services.reseller import (
    ResellerError,
    clear_plan_price,
    get_plan_price,
    get_rate,
    list_plan_prices,
    parse_percent_input,
    set_plan_price,
    set_rate,
)
from services.reseller_admin import (
    RATE_FIELD_ATTRS,
    Cut,
    apply_cuts,
    cut_preview_text,
    is_ready_plan,
    parse_reseller_input,
    preview_cut,
    screen_cells,
    status_block,
    undo_ops,
    undo_screen_cut,
)
from services.service_names import sanitize_service_name
from services.service_ops import (
    MAX_MANUAL_GB,
    ServiceOpError,
    ServiceSnapshot,
    SupportExtras,
    add_data,
    extend_days,
    get_snapshot,
    get_support_extras,
    list_devices,
    reset_devices,
    revoke_link,
    set_enabled,
    set_user_services_enabled,
)
from services.store_admin import get_catalog_view
from services.store_catalog import flat_plan_list_enabled
from services.store_config import is_store_owner
from services.store_pricing import (
    DEFAULT_TIER,
    category_durations,
    category_tiers,
    duration_from_package_options,
    get_price,
)
from services.trial import get_reseller_trial_quota, reseller_month_sales
from services.ux_format import (
    format_jalali,
    format_relative_age,
    format_toman,
    is_stale_pending,
    parse_positive_int,
    time_left_fa,
    to_persian_digits,
)
from services.wallet import credit_provision_failed

logger = logging.getLogger(__name__)
router = Router(name="admin")


def _sanitize_error_detail(exc: BaseException, *, limit: int = 240) -> str:
    if isinstance(exc, PasarGuardNotFoundError):
        return "سرویس در پنل پیدا نشد"
    raw = str(exc).strip() or exc.__class__.__name__
    # Drop obvious secrets
    lowered = raw.lower()
    for token in ("api_key", "pg_key_", "password", "token", "secret", "authorization"):
        if token in lowered:
            return exc.__class__.__name__
    return raw[:limit]


def _tg_message(callback: CallbackQuery) -> Message | None:
    message = callback.message
    if isinstance(message, Message):
        return message
    return None


async def _edit_or_send(message: Message, body: str, markup: Any) -> None:
    """Replace the current screen, falling back to a new message.

    Editing keeps confirmation screens from piling up under the one they guard.
    """
    await edit_or_send(message, body, markup)


def _fmt_toman(value: int) -> str:
    """Money on admin screens, in Persian numerals.

    It used to emit ASCII digits while the counts beside it were Persian, so a
    single card read «کیف پول: 180٬770» next to «سفارش‌ها: ۰». Two numeral
    systems in one message is the sloppiness the owner notices first.
    """
    return format_toman(int(value), persian_digits=True)


def _status_fa(status: str) -> str:
    return texts.account_status_labels.get(status, status)


def _user_label(user: User | None) -> str:
    if user is None:
        return "—"
    name = user.first_name or user.username or str(user.telegram_id)
    uname = f" @{user.username}" if user.username else ""
    return f"{escape(name)}{escape(uname)} · آیدی <code>{user.telegram_id}</code>"


_PAYMENT_STATUS_FA: dict[PaymentStatus, str] = {
    PaymentStatus.pending: "در انتظار بررسی",
    PaymentStatus.verified: "تأیید شده",
    PaymentStatus.rejected: "رد شده",
}


def payment_status_fa(status: PaymentStatus) -> str:
    """Persian for every payment state. No raw fallback: a new enum value fails
    `test_every_payment_status_has_a_persian_label` instead of reaching the owner."""
    return _PAYMENT_STATUS_FA[status]


def format_order_detail(order: Order) -> str:
    user = order.user
    payment = order.payment
    # Set by the order loaders in services/admin.py; never ``plan.name``.
    plan_name = getattr(order, "product_label_fa", None) or "—"
    pay_status = payment_status_fa(payment.status) if payment else "—"
    banned = "بله" if (user and user.is_banned) else "خیر"
    return texts.admin_order_detail.format(
        order_id=order.id,
        status=_status_fa(order.status.value),
        price=_fmt_toman(order.price_toman),
        user_label=_user_label(user),
        banned=banned,
        plan=escape(plan_name),
        payment_status=pay_status,
        panel=escape(order.panel_username or "—"),
    )


def _receipt_file_id(order: Order) -> str | None:
    payment = order.payment
    if payment is None:
        return None
    file_id = (payment.receipt_file_id or "").strip()
    return file_id or None


def _reject_reason_label(reason_key: str) -> str:
    mapping = getattr(texts, "admin_reject_reasons", {}) or {}
    return mapping.get(reason_key) or reason_key


async def _edit_text_or_caption(message: Message, body: str, markup: Any) -> None:
    """Edit a screen in place, whichever kind it is.

    An order card with a receipt is a photo whose body is the caption, and
    Telegram refuses ``edit_text`` on it («there is no text in the message to
    edit»).
    """
    if message.photo or message.document:
        await message.edit_caption(caption=body[:1024], reply_markup=markup)
    else:
        await message.edit_text(body, reply_markup=markup)


async def _mark_review_message_resolved(
    callback: CallbackQuery,
    *,
    resolved_line: str,
    has_next: bool,
) -> None:
    """Update caption/text after approve/reject and swap action buttons."""
    message = _tg_message(callback)
    if message is None:
        return
    base = (message.caption or message.text or "").rstrip()
    # Avoid stacking duplicate resolved markers on re-clicks
    new_body = base if resolved_line in base else f"{base}\n\n{resolved_line}"
    markup = payment_resolved_keyboard(has_next=has_next)
    try:
        await _edit_text_or_caption(message, new_body, markup)
    except Exception:
        with contextlib.suppress(Exception):
            await message.edit_reply_markup(reply_markup=markup)


async def _deliver_order_detail(
    message: Message,
    order: Order,
    *,
    prefer_edit: bool = True,
    back_cb: str | None = None,
) -> None:
    """Show order detail; send the stored receipt with its caption when file_id is present."""
    text = format_order_detail(order)
    markup = order_detail_keyboard(order, back_cb=back_cb)
    file_id = _receipt_file_id(order)
    if file_id:
        try:
            await answer_stored_media(
                message,
                file_id=file_id,
                media_type=order.payment.receipt_media_type,
                caption=text,
                reply_markup=markup,
            )
            return
        except Exception:
            logger.exception(
                "Failed to send receipt photo for order %s; falling back to text",
                order.id,
            )
    if prefer_edit:
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=markup)


async def _show_home(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings
) -> None:
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    body, markup = await admin_home_screen(session, settings.web_panel_link)
    await edit_or_send(message, body, markup)


# ---- home / menu -----------------------------------------------------------


@router.callback_query(F.data == MenuCallback.ADMIN)
@router.callback_query(F.data == AdminCB.HOME)
async def admin_home(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await _show_home(callback, state, session, settings)


_HOME_SECTIONS = {
    AdminCB.SEC_SALES: (lambda: texts.admin_home_sec_sales, admin_sales_keyboard),
    AdminCB.SEC_PEOPLE: (lambda: texts.admin_home_sec_people, admin_people_keyboard),
    AdminCB.SEC_SETTINGS: (lambda: texts.admin_home_sec_settings, admin_settings_keyboard),
}


@router.callback_query(F.data.in_(set(_HOME_SECTIONS)))
async def admin_home_section(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    title, keyboard = _HOME_SECTIONS[callback.data or ""]
    await _edit_or_answer_admin(message, title(), keyboard())


# ---- pending payments ------------------------------------------------------


def _parse_pending_filter(data: str) -> tuple[str, int]:
    """Parse pending callbacks → (filter_key, offset). Defaults: all, 0."""
    if data.startswith(AdminCB.PENDING_FILTER):
        raw = data[len(AdminCB.PENDING_FILTER) :]
        parts = raw.split(":")
        filter_key = parts[0] if parts and parts[0] in {"all", "stale"} else "all"
        offset = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        return filter_key, offset
    return "all", 0


@router.callback_query(F.data == AdminCB.PENDING)
@router.callback_query(F.data.startswith(AdminCB.PENDING_FILTER))
async def admin_pending(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    data = callback.data or ""
    filter_key, offset = _parse_pending_filter(data)
    stale_only = filter_key == "stale"
    page = await list_pending_payments(
        session, offset=offset, limit=PAGE_SIZE, stale_only=stale_only
    )
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    if not page.items:
        text = texts.admin_pending_filter_stale_empty if stale_only else texts.admin_pending_empty
    else:
        lines = [
            texts.admin_pending_title.format(
                total=to_persian_digits(page.total), page=to_persian_digits(page.page + 1)
            )
        ]
        for order in page.items:
            age = format_relative_age(order.created_at)
            stale = is_stale_pending(order.created_at)
            receipt = ""
            if _receipt_file_id(order):
                receipt = texts.admin_pending_receipt_badge
            lines.append(
                texts.admin_pending_line.format(
                    order_id=order.id,
                    amount=_fmt_toman(order.price_toman),
                    age=age,
                    stale_mark=texts.admin_pending_stale_mark if stale else "",
                    user_label=_user_label(order.user),
                    receipt=receipt,
                )
            )
        text = "\n".join(lines)
    markup = pending_list_keyboard(
        page.items,
        offset=page.offset,
        limit=page.limit,
        total=page.total,
        filter_key=filter_key,
    )
    await edit_or_send(message, text, markup)


# ---- orders filter / list --------------------------------------------------


@router.callback_query(F.data == AdminCB.ORDERS)
async def admin_orders_menu(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    try:
        await message.edit_text(texts.admin_orders_filter, reply_markup=orders_filter_keyboard())
    except Exception:
        await message.answer(texts.admin_orders_filter, reply_markup=orders_filter_keyboard())


@router.callback_query(F.data.startswith(AdminCB.ORDERS_FILTER))
async def admin_orders_list(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    # adm:of:{STATUS|ALL}:{offset}
    raw = (callback.data or "")[len(AdminCB.ORDERS_FILTER) :]
    parts = raw.split(":")
    status_key = parts[0] if parts else "ALL"
    offset = int(parts[1]) if len(parts) > 1 else 0
    status: OrderStatus | None = None
    if status_key != "ALL":
        try:
            status = OrderStatus(status_key)
        except ValueError:
            await callback.answer(texts.admin_action_error, show_alert=True)
            return
    page = await list_orders(session, status=status, offset=offset, limit=PAGE_SIZE)
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    label = _status_fa(status_key) if status_key != "ALL" else texts.admin_orders_all
    if not page.items:
        text = texts.admin_orders_empty.format(filter=label)
    else:
        lines = [
            texts.admin_orders_title.format(
                filter=label,
                total=to_persian_digits(page.total),
                page=to_persian_digits(page.page + 1),
            )
        ]
        for order in page.items:
            lines.append(
                texts.admin_order_line.format(
                    order_id=order.id,
                    status=_status_fa(order.status.value),
                    amount=_fmt_toman(order.price_toman),
                )
            )
        text = "\n".join(lines)
    markup = orders_list_keyboard(
        page.items,
        status_key=status_key,
        offset=page.offset,
        limit=page.limit,
        total=page.total,
    )
    await edit_or_send(message, text, markup)


# ---- order detail ----------------------------------------------------------


@router.callback_query(F.data.startswith(AdminCB.ORDER))
async def admin_order_detail(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    order_id, back_cb = parse_order_cb(callback.data or "")
    if order_id is None:
        await callback.answer(texts.stale_button, show_alert=True)
        return
    try:
        order = await get_order_detail(session, order_id)
    except AdminError:
        await callback.answer(texts.admin_order_not_found, show_alert=True)
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _deliver_order_detail(message, order, prefer_edit=True, back_cb=back_cb)


@router.callback_query(F.data.startswith(AdminCB.SHOW_RECEIPT))
async def admin_show_receipt(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    """Re-send the stored receipt media for an order (admin convenience)."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    order_id = int((callback.data or "").split(":")[-1])
    try:
        order = await get_order_detail(session, order_id)
    except AdminError:
        await callback.answer(texts.admin_order_not_found, show_alert=True)
        return
    file_id = _receipt_file_id(order)
    if not file_id:
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    try:
        await answer_stored_media(
            message,
            file_id=file_id,
            media_type=order.payment.receipt_media_type,
            caption=format_order_detail(order),
            reply_markup=order_detail_keyboard(order),
        )
    except Exception:
        logger.exception("Re-send receipt failed for order %s", order_id)
        await message.answer(
            format_order_detail(order),
            reply_markup=order_detail_keyboard(order),
        )


# ---- lookup / ban FSM ------------------------------------------------------


@router.callback_query(F.data == AdminCB.LOOKUP)
async def admin_lookup_start(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.set_state(AdminStates.waiting_order_id)
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await message.answer(
            texts.admin_prompt_order_id, reply_markup=admin_cancel_input_keyboard()
        )


@router.callback_query(F.data == AdminCB.BAN)
async def admin_ban_start(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.set_state(AdminStates.waiting_ban_telegram_id)
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await message.answer(
            texts.admin_prompt_ban_telegram_id, reply_markup=admin_cancel_input_keyboard()
        )


@router.callback_query(F.data == AdminCB.UNBAN)
async def admin_unban_start(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.set_state(AdminStates.waiting_unban_telegram_id)
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await message.answer(
            texts.admin_prompt_unban_telegram_id, reply_markup=admin_cancel_input_keyboard()
        )


@router.callback_query(F.data == AdminCB.CANCEL_INPUT)
async def admin_cancel_input(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await _show_home(callback, state, session, settings)


@router.message(AdminStates.waiting_order_id)
async def admin_lookup_order_message(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    tg_id = message.from_user.id if message.from_user else None
    if not _is_admin(tg_id, settings, db_user):
        await message.answer(texts.admin_action_denied, reply_markup=admin_back_home_keyboard())
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer(texts.admin_invalid_id, reply_markup=admin_cancel_input_keyboard())
        return
    order_id = int(raw)
    try:
        order = await get_order_detail(session, order_id)
    except AdminError:
        await message.answer(
            texts.admin_order_not_found, reply_markup=admin_cancel_input_keyboard()
        )
        return
    await state.clear()
    await _deliver_order_detail(message, order, prefer_edit=False)


@router.message(AdminStates.waiting_ban_telegram_id)
async def admin_ban_message(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    tg_id = message.from_user.id if message.from_user else None
    if not _is_admin(tg_id, settings, db_user):
        await message.answer(texts.admin_action_denied, reply_markup=admin_back_home_keyboard())
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer(texts.admin_invalid_id, reply_markup=admin_cancel_input_keyboard())
        return
    try:
        user = await set_user_banned_by_telegram_id(
            session, int(raw), True, admin_telegram_ids=settings.admin_telegram_ids
        )
        await session.commit()
    except AdminError as exc:
        await session.rollback()
        body = (
            texts.admin_cannot_ban_admin
            if str(exc) == "cannot_ban_admin"
            else texts.admin_user_not_found
        )
        await message.answer(body, reply_markup=admin_cancel_input_keyboard())
        return
    await state.clear()
    await message.answer(
        texts.admin_ban_ok.format(telegram_id=user.telegram_id),
        reply_markup=admin_back_home_keyboard(),
    )


@router.message(AdminStates.waiting_unban_telegram_id)
async def admin_unban_message(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    tg_id = message.from_user.id if message.from_user else None
    if not _is_admin(tg_id, settings, db_user):
        await message.answer(texts.admin_action_denied, reply_markup=admin_back_home_keyboard())
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer(texts.admin_invalid_id, reply_markup=admin_cancel_input_keyboard())
        return
    try:
        user = await set_user_banned_by_telegram_id(session, int(raw), False)
        await session.commit()
    except AdminError:
        await session.rollback()
        await message.answer(texts.admin_user_not_found, reply_markup=admin_cancel_input_keyboard())
        return
    await state.clear()
    await message.answer(
        texts.admin_unban_ok.format(telegram_id=user.telegram_id),
        reply_markup=admin_back_home_keyboard(),
    )


# ---- ban / unban from order ------------------------------------------------


@router.callback_query(F.data.startswith(AdminCB.BAN_FROM_ORDER))
async def admin_ban_from_order(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    user_id = int((callback.data or "").split(":")[-1])
    try:
        user = await set_user_banned(
            session, user_id, True, admin_telegram_ids=settings.admin_telegram_ids
        )
        await session.commit()
    except AdminError as exc:
        await session.rollback()
        body = (
            texts.admin_cannot_ban_admin
            if str(exc) == "cannot_ban_admin"
            else texts.admin_user_not_found
        )
        await callback.answer(body, show_alert=True)
        return
    await callback.answer(texts.admin_action_ok)
    message = _tg_message(callback)
    if message is not None:
        await message.answer(
            texts.admin_ban_ok.format(telegram_id=user.telegram_id),
            reply_markup=admin_back_home_keyboard(),
        )


@router.callback_query(F.data.startswith(AdminCB.UNBAN_FROM_ORDER))
async def admin_unban_from_order(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    user_id = int((callback.data or "").split(":")[-1])
    try:
        user = await set_user_banned(session, user_id, False)
        await session.commit()
    except AdminError:
        await session.rollback()
        await callback.answer(texts.admin_user_not_found, show_alert=True)
        return
    await callback.answer(texts.admin_action_ok)
    message = _tg_message(callback)
    if message is not None:
        await message.answer(
            texts.admin_unban_ok.format(telegram_id=user.telegram_id),
            reply_markup=admin_back_home_keyboard(),
        )


# ---- retry provision -------------------------------------------------------


@router.callback_query(F.data.startswith(AdminCB.RETRY))
async def admin_retry_provision(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    order_id = int((callback.data or "").split(":")[-1])
    account = None
    referral_reward = None
    already_delivered = await order_is_delivered(session, order_id)
    try:
        client = await get_pasarguard_client(session, settings)
        try:
            account, referral_reward = await retry_provision(
                session,
                client,
                order_id=order_id,
            )
        finally:
            await client.aclose()
        await session.commit()
    except AdminError as exc:
        logger.warning("retry_provision failed order=%s: %s", order_id, exc)
        await session.rollback()
        if str(exc) == "insufficient_wallet_for_retry":
            await callback.answer(texts.admin_retry_need_wallet, show_alert=True)
        else:
            await callback.answer(texts.admin_retry_failed, show_alert=True)
        return
    except Exception:
        logger.exception("retry_provision unexpected order=%s", order_id)
        try:
            await session.commit()
        except Exception:
            await session.rollback()
        await callback.answer(texts.admin_retry_failed, show_alert=True)
        return

    result = await session.execute(
        select(Order).where(Order.id == order_id).options(selectinload(Order.user))
    )
    order = result.scalar_one()
    if already_delivered:
        # A repeated tap: the order was ACTIVE before this call, so the card and
        # the referral notice went out the first time.
        referral_reward = None
    elif account is not None:
        try:
            await deliver_provisioned_service_card(
                session,
                settings,
                user_id=order.user_id,
                account_id=account.id,
                order_id=order_id,
                bot=bot,
                chat_id=order.user.telegram_id,
                intro=texts.payment_ready_intro.format(order_id=to_persian_digits(order_id)),
            )
        except Exception:
            logger.exception("Failed to notify customer after retry order %s", order_id)
    else:
        logger.warning("retry_provision ok but no account for order %s", order_id)

    if referral_reward is not None:
        try:
            ref_res = await session.execute(
                select(User).where(User.id == referral_reward.referrer_id)
            )
            referrer = ref_res.scalar_one_or_none()
            if referrer is not None:
                await bot.send_message(
                    chat_id=referrer.telegram_id,
                    text=texts.referral_reward_notify.format(
                        value=to_persian_digits(referral_reward.reward_value)
                    ),
                )
        except Exception:
            logger.exception("Failed to notify referrer after retry order %s", order_id)

    await callback.answer(texts.admin_retry_ok)
    message = _tg_message(callback)
    # A repeated tap changed nothing, and re-editing the screen to the text it
    # already shows is a "message is not modified" error.
    if message is not None and not already_delivered:
        try:
            refreshed = await get_order_detail(session, order_id)
            await _edit_text_or_caption(
                message,
                format_order_detail(refreshed),
                order_detail_keyboard(refreshed),
            )
        except Exception:
            logger.exception("Could not refresh order detail after retry order=%s", order_id)


# ---- stats -----------------------------------------------------------------


@router.callback_query(F.data == AdminCB.STATS)
async def admin_stats(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    counts = await dashboard_counts(session)
    report = await get_finance_report(session)
    rows = "\n".join(
        texts.admin_stats_row.format(
            label=w.label_fa,
            revenue=_fmt_toman(w.revenue_toman),
            orders=to_persian_digits(w.orders),
            average=_fmt_toman(w.average_toman),
        )
        for w in (report.today, report.week, report.month, report.jalali_month, report.all_time)
    )
    attention = (
        texts.admin_stats_attention.format(
            review=to_persian_digits(report.awaiting_review),
            failed=to_persian_digits(report.provision_failed),
        )
        if report.awaiting_review or report.provision_failed
        else texts.admin_stats_attention_none
    )
    text = texts.admin_stats.format(
        badge=texts.admin_stats_note,
        rows=rows,
        liability=_fmt_toman(report.wallet_liability_toman),
        credited=_fmt_toman(report.wallet_credited_toman),
        spent=_fmt_toman(report.wallet_spent_toman),
        active_services=to_persian_digits(report.active_services),
        customers=to_persian_digits(report.paying_customers),
        users=to_persian_digits(counts.users),
        banned=to_persian_digits(counts.banned_users),
        attention=attention,
    )
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    try:
        await message.edit_text(text, reply_markup=admin_back_home_keyboard())
    except Exception:
        await message.answer(text, reply_markup=admin_back_home_keyboard())


# ---- Phase 4 pay:approve / pay:reject (KEEP WORKING) -----------------------


def _provision_failed_admin_keyboard(order_id: int) -> InlineKeyboardMarkup:
    """The provision-failed DM: straight to the order (where retry lives), or home."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 جزئیات سفارش", callback_data=f"{AdminCB.ORDER}{order_id}"
                )
            ],
            *nav_rows(),
        ]
    )


@router.callback_query(F.data.startswith(PayCB.APPROVE_PREFIX))
async def pay_approve(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    db_user: User | None,
) -> None:
    if not callback.from_user or not _is_admin(callback.from_user.id, settings, db_user):
        await callback.answer(texts.admin_action_denied, show_alert=True)
        return

    if db_user is None:
        await callback.answer(texts.admin_action_error, show_alert=True)
        return

    order_id = int((callback.data or "").split(":")[-1])
    account = None
    referral_reward = None
    already_delivered = False
    try:
        await approve_payment(session, order_id=order_id, admin_user_id=db_user.id)
        await session.flush()
        already_delivered = await order_is_delivered(session, order_id)

        client = await get_pasarguard_client(session, settings)
        try:
            account, referral_reward = await provision_order(
                session,
                client,
                order_id=order_id,
            )
        finally:
            await client.aclose()
        await session.commit()
    except PaymentError as exc:
        logger.warning("approve failed order=%s: %s", order_id, exc)
        await session.rollback()
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    except Exception as exc:
        # provision_order sets PROVISION_FAILED before re-raising — keep that + approve
        logger.exception("provision failed order=%s", order_id)
        detail = _sanitize_error_detail(exc)
        # Refund only when the panel proved it refused the create. A timeout or
        # 5xx may follow a write that succeeded: crediting it would pay the
        # customer back and then deliver on the retry. The order stays for retry.
        refund_proven = is_terminal_wallet_provision_rejection(exc)
        credited = False
        try:
            # Reload order/user for wallet credit + DM
            result = await session.execute(
                select(Order).where(Order.id == order_id).options(selectinload(Order.user))
            )
            failed_order = result.scalar_one_or_none()
            if (
                refund_proven
                and failed_order is not None
                and failed_order.status == OrderStatus.PROVISION_FAILED
            ):
                entry, _created = await credit_provision_failed(
                    session,
                    user_id=failed_order.user_id,
                    order_id=failed_order.id,
                    amount_toman=int(failed_order.price_toman),
                    max_n=settings.wallet_provision_fail_credit_max_n,
                )
                credited = entry is not None
            await session.commit()
            if credited and failed_order is not None and failed_order.user is not None:
                try:
                    await bot.send_message(
                        chat_id=failed_order.user.telegram_id,
                        text=texts.payment_provision_failed_user.format(
                            order_id=to_persian_digits(order_id),
                            amount=_fmt_toman(int(failed_order.price_toman)),
                        ),
                        reply_markup=provision_failed_keyboard(),
                    )
                except Exception:
                    logger.exception("Failed to DM customer provision fail order %s", order_id)
        except Exception:
            logger.exception("wallet credit / commit after provision fail order=%s", order_id)
            try:
                await session.commit()
            except Exception:
                await session.rollback()
        admin_text = texts.payment_provision_failed_admin_detail.format(
            order_id=order_id, detail=escape(detail)
        )
        if not credited:
            admin_text += texts.admin_provision_failed_no_refund
        for admin_tg_id in settings.admin_telegram_ids:
            try:
                await bot.send_message(
                    chat_id=admin_tg_id,
                    text=admin_text,
                    reply_markup=_provision_failed_admin_keyboard(order_id),
                )
            except Exception:
                logger.exception("notify admin provision fail")
        await callback.answer(texts.admin_action_error, show_alert=True)
        if callback.message:
            with contextlib.suppress(Exception):
                await callback.message.edit_reply_markup(reply_markup=None)
        return

    result = await session.execute(
        select(Order).where(Order.id == order_id).options(selectinload(Order.user))
    )
    order = result.scalar_one()
    if already_delivered:
        # A replayed tap: the order was ACTIVE before this call, so the card and
        # the referral notice went out the first time.
        referral_reward = None
    elif account is not None:
        try:
            await deliver_provisioned_service_card(
                session,
                settings,
                user_id=order.user_id,
                account_id=account.id,
                order_id=order_id,
                bot=bot,
                chat_id=order.user.telegram_id,
                intro=texts.payment_ready_intro.format(order_id=to_persian_digits(order_id)),
            )
        except Exception:
            logger.exception("Failed to notify customer for order %s", order_id)
    else:
        logger.warning("pay_approve provision ok but no account for order %s", order_id)

    if referral_reward is not None:
        try:
            ref_res = await session.execute(
                select(User).where(User.id == referral_reward.referrer_id)
            )
            referrer = ref_res.scalar_one_or_none()
            if referrer is not None:
                await bot.send_message(
                    chat_id=referrer.telegram_id,
                    text=texts.referral_reward_notify.format(
                        value=to_persian_digits(referral_reward.reward_value)
                    ),
                )
        except Exception:
            logger.exception("Failed to notify referrer for order %s", order_id)

    nxt = await get_next_pending_payment(session, exclude_order_id=order_id)
    await callback.answer(texts.admin_action_ok)
    await _mark_review_message_resolved(
        callback,
        resolved_line=texts.admin_payment_resolved_approved,
        has_next=nxt is not None,
    )


async def _finalize_reject(
    *,
    callback: CallbackQuery | None,
    message: Message | None,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    db_user: User,
    order_id: int,
    reason: str,
    state: FSMContext,
) -> None:
    """Persist reject + note, DM customer, resolve admin message, offer next."""
    reason_clean = (reason or "").strip()
    if not reason_clean:
        target = message or (_tg_message(callback) if callback else None)
        if target is not None:
            await target.answer(
                texts.admin_reject_reason_empty,
                reply_markup=admin_cancel_input_keyboard(),
            )
        if callback is not None:
            await callback.answer(texts.admin_reject_reason_empty, show_alert=True)
        return
    if len(reason_clean) > 200:
        target = message or (_tg_message(callback) if callback else None)
        if target is not None:
            await target.answer(
                texts.admin_reject_reason_too_long,
                reply_markup=admin_cancel_input_keyboard(),
            )
        if callback is not None:
            await callback.answer(texts.admin_reject_reason_too_long, show_alert=True)
        return
    try:
        await reject_payment(
            session,
            order_id=order_id,
            admin_user_id=db_user.id,
            note=reason_clean,
        )
        await session.commit()
    except PaymentError as exc:
        logger.warning("reject failed order=%s: %s", order_id, exc)
        await session.rollback()
        if callback is not None:
            await callback.answer(texts.admin_action_error, show_alert=True)
        elif message is not None:
            await message.answer(texts.admin_action_error, reply_markup=admin_back_home_keyboard())
        return
    except Exception:
        logger.exception("reject unexpected order=%s", order_id)
        await session.rollback()
        if callback is not None:
            await callback.answer(texts.admin_action_error, show_alert=True)
        elif message is not None:
            await message.answer(texts.admin_action_error, reply_markup=admin_back_home_keyboard())
        return

    await state.clear()
    result = await session.execute(
        select(Order).where(Order.id == order_id).options(selectinload(Order.user))
    )
    order = result.scalar_one()
    try:
        await bot.send_message(
            chat_id=order.user.telegram_id,
            text=with_support(
                texts.payment_rejected_user_reason.format(
                    order_id=to_persian_digits(order_id), reason=escape(reason_clean)
                ),
                settings,
            ),
        )
    except Exception:
        logger.exception("Failed to notify customer reject order %s", order_id)

    nxt = await get_next_pending_payment(session, exclude_order_id=order_id)
    resolved = texts.admin_payment_resolved_rejected_reason.format(reason=escape(reason_clean))
    if callback is not None:
        await callback.answer(texts.admin_action_ok)
        await _mark_review_message_resolved(
            callback, resolved_line=resolved, has_next=nxt is not None
        )
    elif message is not None:
        await message.answer(
            resolved,
            reply_markup=payment_resolved_keyboard(has_next=nxt is not None),
        )


@router.callback_query(F.data.startswith(PayCB.REJECT_PREFIX))
async def pay_reject_start(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
) -> None:
    """Ask for reject reason (quick buttons or custom) — do not reject yet."""
    if not callback.from_user or not _is_admin(callback.from_user.id, settings, db_user):
        await callback.answer(texts.admin_action_denied, show_alert=True)
        return
    if db_user is None:
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    # Avoid matching pay:rjr: / pay:rjc: / pay:rjx:
    data = callback.data or ""
    if not data.startswith(PayCB.REJECT_PREFIX):
        return
    rest = data[len(PayCB.REJECT_PREFIX) :]
    if not rest.isdigit():
        await callback.answer(texts.stale_button, show_alert=True)
        return
    order_id = int(rest)
    await state.update_data(reject_order_id=order_id)
    await state.set_state(AdminStates.waiting_reject_reason)
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await message.answer(
            texts.admin_reject_ask_reason.format(order_id=order_id),
            reply_markup=reject_reason_keyboard(order_id),
        )


@router.callback_query(F.data.startswith(PayCB.REJECT_REASON_PREFIX))
async def pay_reject_quick_reason(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if not callback.from_user or not _is_admin(callback.from_user.id, settings, db_user):
        await callback.answer(texts.admin_action_denied, show_alert=True)
        return
    if db_user is None:
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    raw = (callback.data or "")[len(PayCB.REJECT_REASON_PREFIX) :]
    parts = raw.split(":", 1)
    if len(parts) != 2 or not parts[0].isdigit():
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    order_id = int(parts[0])
    reason_key = parts[1]
    allowed = {k for k, _ in REJECT_QUICK_REASONS}
    if reason_key not in allowed:
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    reason = _reject_reason_label(reason_key)
    await _finalize_reject(
        callback=callback,
        message=None,
        session=session,
        settings=settings,
        bot=bot,
        db_user=db_user,
        order_id=order_id,
        reason=reason,
        state=state,
    )


@router.callback_query(F.data.startswith(PayCB.REJECT_CUSTOM_PREFIX))
async def pay_reject_custom_start(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
) -> None:
    if not callback.from_user or not _is_admin(callback.from_user.id, settings, db_user):
        await callback.answer(texts.admin_action_denied, show_alert=True)
        return
    raw = (callback.data or "")[len(PayCB.REJECT_CUSTOM_PREFIX) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    order_id = int(raw)
    await state.set_state(AdminStates.waiting_reject_reason)
    await state.update_data(reject_order_id=order_id)
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await message.answer(
            texts.admin_reject_ask_custom.format(order_id=order_id),
            reply_markup=admin_cancel_input_keyboard(),
        )


@router.callback_query(F.data.startswith(PayCB.REJECT_ABORT_PREFIX))
async def pay_reject_abort(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    await callback.answer(texts.admin_reject_aborted)
    message = _tg_message(callback)
    if message is not None:
        await message.answer(
            texts.admin_reject_aborted,
            reply_markup=admin_back_home_keyboard(),
        )


@router.message(AdminStates.waiting_reject_reason)
async def pay_reject_custom_message(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    db_user: User | None,
) -> None:
    tg_id = message.from_user.id if message.from_user else None
    if not _is_admin(tg_id, settings, db_user) or db_user is None:
        await message.answer(texts.admin_action_denied, reply_markup=admin_back_home_keyboard())
        return
    data = await state.get_data()
    order_id = data.get("reject_order_id")
    if not order_id:
        await state.clear()
        await message.answer(texts.admin_action_error, reply_markup=admin_back_home_keyboard())
        return
    await _finalize_reject(
        callback=None,
        message=message,
        session=session,
        settings=settings,
        bot=bot,
        db_user=db_user,
        order_id=int(order_id),
        reason=(message.text or "").strip(),
        state=state,
    )


@router.callback_query(F.data == AdminCB.NEXT_PENDING)
async def admin_next_pending(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    nxt = await get_next_pending_payment(session)
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    if nxt is None:
        await message.answer(
            texts.admin_next_pending_empty,
            reply_markup=admin_back_home_keyboard(),
        )
        return
    await _deliver_order_detail(
        message,
        nxt,
        prefer_edit=False,
        back_cb=f"{AdminCB.PENDING_FILTER}all:0",
    )


# STORE_ADMIN hub lives in bot.handlers.store_admin (AdminCB.STORE).


# ---- Phase A2: PasarGuard panel settings -----------------------------------


_PANEL_FIELD_STATE = {
    "base_url": AdminStates.panel_waiting_base_url,
    "api_key": AdminStates.panel_waiting_api_key,
    "sub_path": AdminStates.panel_waiting_sub_path,
    "timeout": AdminStates.panel_waiting_timeout,
    "trial_hwid": AdminStates.panel_waiting_trial,
}

_PANEL_PROMPTS = {
    "base_url": texts.admin_panel_prompt_base_url,
    "api_key": texts.admin_panel_prompt_api_key,
    "sub_path": texts.admin_panel_prompt_sub_path,
    "timeout": texts.admin_panel_prompt_timeout,
    "trial_hwid": texts.admin_panel_prompt_trial_hwid,
}


def _fmt_id_list(ids: list[int] | None) -> str:
    if not ids:
        return "—"
    return ", ".join(str(g) for g in ids)


async def _format_panel_settings(session: AsyncSession, settings: Settings) -> str:
    row = await get_settings_row(session)
    effective = await get_effective_pasarguard_config(session, settings)
    snap = public_settings_snapshot(row, settings, effective)
    trial_hwid = await get_trial_hwid_limit(session, settings)
    store_groups = await _store_group_lines(session, settings)
    return texts.admin_panel_title.format(
        base_url=escape(str(snap["base_url"])),
        api_key_masked=snap["api_key_masked"],
        sub_path=escape(str(snap["sub_path"])),
        timeout=snap["timeout"],
        trial_hwid=to_persian_digits(trial_hwid),
        store_groups=store_groups,
        updated_at=snap["updated_at"] or "—",
    )


async def _store_group_lines(session: AsyncSession, settings: Settings) -> str:
    """Each store group's panel group — the only place a service's groups come from.

    A group with none is not for sale, so it says so instead of showing a
    default that no longer exists.
    """
    from services.group_sync import target_groups_for_category

    view = await get_catalog_view(session)
    lines = []
    for cat in view.categories:
        groups = await target_groups_for_category(session, settings, int(cat.id))
        if groups:
            lines.append(
                texts.admin_panel_store_group_line.format(
                    title=escape(str(cat.title_fa)), groups=_fmt_id_list(groups)
                )
            )
        else:
            lines.append(
                texts.admin_panel_store_group_unset.format(title=escape(str(cat.title_fa)))
            )
    return "\n".join(lines) or texts.admin_panel_store_groups_none


_PANEL_GROUP_NAME_MAX = 24
_PANEL_VERIFIED_FIELDS = {"base_url", "api_key", "sub_path", "timeout"}


def _panel_group_line(group: GroupResponse | GroupSimple, stores: list[str]) -> str:
    """«#11 tunnel-a • ۱۲۳ کاربر • ۳ اینباند ⛔ → حجمی ویژه». Only the name is trimmed.

    A ``GroupSimple`` has no counts, so its line states none.
    """
    name = group.name
    if len(name) > _PANEL_GROUP_NAME_MAX:
        name = name[: _PANEL_GROUP_NAME_MAX - 1] + "\u2026"
    stores_part = (
        texts.admin_panel_groups_list_stores.format(stores=escape("\u060c ".join(stores)))
        if stores
        else ""
    )
    if isinstance(group, GroupSimple):
        return texts.admin_panel_groups_list_line_simple.format(
            id=group.id, name=escape(name), stores=stores_part
        )
    return texts.admin_panel_groups_list_line.format(
        id=group.id,
        name=escape(name),
        users=to_persian_digits(group.total_users),
        inbounds=to_persian_digits(len(group.inbound_tags or [])),
        disabled=texts.admin_panel_groups_list_disabled if group.is_disabled else "",
        stores=(
            texts.admin_panel_groups_list_stores.format(stores=escape("\u060c ".join(stores)))
            if stores
            else ""
        ),
    )


async def _store_groups_by_panel_group(
    session: AsyncSession, settings: Settings
) -> dict[int, list[str]]:
    """Which store groups sell from each panel group (same rule as the screen above)."""
    from services.group_sync import target_groups_for_category

    view = await get_catalog_view(session)
    used: dict[int, list[str]] = {}
    for cat in view.categories:
        for gid in await target_groups_for_category(session, settings, int(cat.id)):
            used.setdefault(int(gid), []).append(str(cat.title_fa))
    return used


def _panel_can_edit(settings: Settings, telegram_id: int | None) -> bool:
    """Address and key decide where every order goes, so only the owner writes them."""
    return is_store_owner(settings, telegram_id)


async def _deny_panel_write(callback: CallbackQuery, settings: Settings) -> bool:
    tg_id = callback.from_user.id if callback.from_user else None
    if _panel_can_edit(settings, tg_id):
        return False
    await callback.answer(texts.admin_store_owner_only, show_alert=True)
    return True


async def _panel_screen(
    session: AsyncSession,
    settings: Settings,
    telegram_id: int | None,
    prefix: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    body = await _format_panel_settings(session, settings)
    can_edit = _panel_can_edit(settings, telegram_id)
    if not can_edit:
        body += "\n\n" + texts.admin_panel_view_only
    if prefix:
        body = prefix + "\n\n" + body
    return body, panel_settings_keyboard(can_edit=can_edit)


@router.callback_query(F.data == AdminCB.PANEL)
async def admin_panel_settings(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    text, markup = await _panel_screen(
        session, settings, callback.from_user.id if callback.from_user else None
    )
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _edit_or_answer_admin(message, text, markup)


@router.callback_query(F.data.startswith(AdminCB.PANEL_EDIT))
async def admin_panel_edit_field(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_panel_write(callback, settings):
        return
    field = (callback.data or "").removeprefix(AdminCB.PANEL_EDIT)
    if field not in _PANEL_FIELD_STATE:
        await callback.answer(texts.admin_panel_invalid, show_alert=True)
        return
    await state.set_state(_PANEL_FIELD_STATE[field])
    await state.update_data(panel_field=field)
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await _edit_or_answer_admin(message, _PANEL_PROMPTS[field], panel_cancel_keyboard())


def _is_clear_token(raw: str) -> bool:
    return raw.strip() in {"-", "—", "clear", "پاک", "حذف"}


@router.message(AdminStates.panel_waiting_base_url)
@router.message(AdminStates.panel_waiting_api_key)
@router.message(AdminStates.panel_waiting_sub_path)
@router.message(AdminStates.panel_waiting_timeout)
@router.message(AdminStates.panel_waiting_trial)
async def admin_panel_save_field(  # noqa: PLR0911
    message: Message,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
    db_user: User | None,
) -> None:
    tg_id = message.from_user.id if message.from_user else None
    if not _is_admin(tg_id, settings, db_user):
        return
    if not _panel_can_edit(settings, tg_id):
        await state.clear()
        await message.answer(texts.admin_store_owner_only, reply_markup=admin_back_home_keyboard())
        return
    data = await state.get_data()
    field = data.get("panel_field")
    raw = (message.text or "").strip()
    if not field or not raw:
        await message.answer(texts.admin_panel_invalid, reply_markup=panel_cancel_keyboard())
        return

    admin_id = db_user.id if db_user is not None else None
    clear = _is_clear_token(raw)
    try:
        if field == "base_url":
            await save_pasarguard_settings(
                session, admin_user_id=admin_id, base_url="" if clear else raw
            )
        elif field == "api_key":
            if clear:
                await save_pasarguard_settings(session, admin_user_id=admin_id, clear_api_key=True)
            else:
                await save_pasarguard_settings(session, admin_user_id=admin_id, api_key=raw)
            # Delete the API key message so it does not linger in chat history when possible
            with contextlib.suppress(Exception):
                await message.delete()
        elif field == "sub_path":
            await save_pasarguard_settings(
                session, admin_user_id=admin_id, sub_path="" if clear else raw
            )
        elif field == "timeout":
            if clear:
                await save_pasarguard_settings(session, admin_user_id=admin_id, clear_timeout=True)
            else:
                timeout = float(raw.replace(",", "."))
                if timeout <= 0:
                    await session.rollback()
                    await message.answer(
                        texts.admin_panel_invalid, reply_markup=panel_cancel_keyboard()
                    )
                    return
                await save_pasarguard_settings(session, admin_user_id=admin_id, timeout=timeout)
        elif field in TRIAL_FIELDS:
            arg = TRIAL_FIELDS[field]
            if clear:
                await save_trial_shape(session, admin_user_id=admin_id, clear=arg)
            else:
                parsed = parse_positive_int(raw)
                if parsed is None or parsed > TRIAL_LIMITS[arg]:
                    await session.rollback()
                    await message.answer(
                        texts.admin_panel_invalid,
                        reply_markup=panel_cancel_keyboard(),
                    )
                    return
                await save_trial_shape(session, admin_user_id=admin_id, **{arg: parsed})
        else:
            await message.answer(texts.admin_panel_invalid, reply_markup=admin_back_home_keyboard())
            return
        await session.commit()
    except ValueError:
        await session.rollback()
        await message.answer(texts.admin_panel_invalid, reply_markup=panel_cancel_keyboard())
        return
    except Exception:
        logger.exception("panel settings save failed field=%s", field)
        await session.rollback()
        await message.answer(texts.admin_action_error, reply_markup=admin_back_home_keyboard())
        await state.clear()
        return

    await state.clear()
    prefix = texts.admin_panel_saved
    if field in _PANEL_VERIFIED_FIELDS:
        # A wrong address or key breaks every purchase, so say so now rather
        # than when the next customer pays.
        ok, err = await check_panel_connection(session, settings)
        prefix = (
            texts.admin_panel_saved_ok
            if ok
            else texts.admin_panel_saved_fail.format(error=escape(err))
        )
    text, markup = await _panel_screen(session, settings, tg_id, prefix)
    # One message, not two: never echo the value, and keep the screen navigable.
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == AdminCB.PANEL_TEST)
async def admin_panel_test(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    await callback.answer()
    ok, err = await check_panel_connection(session, settings)
    # err is already sanitized — still never include credentials.
    prefix = (
        texts.admin_panel_test_ok if ok else texts.admin_panel_test_fail.format(error=escape(err))
    )
    message = _tg_message(callback)
    if message is None:
        return
    text, markup = await _panel_screen(
        session, settings, callback.from_user.id if callback.from_user else None, prefix
    )
    await _edit_or_answer_admin(message, text, markup)


async def _render_panel_status(
    callback: CallbackQuery, session: AsyncSession, settings: Settings, prefix: str | None = None
) -> None:
    """Read the panel live and edit the screen in place. Any admin may look."""
    view: PanelStatusView
    try:
        async with _panel(session, settings) as client:
            view = await load_panel_status(client)
    except Exception:
        # No client at all (no key, bad URL): the screen still renders.
        logger.warning("Panel status: no panel client")
        failed = "\u26a0\ufe0f " + READ_FAILED_LINE
        view = PanelStatusView(panel=failed, users="", traffic="", nodes="")
    body = view.text if view.users or view.traffic or view.nodes else view.panel
    if prefix:
        body = prefix + "\n\n" + body
    message = _tg_message(callback)
    if message is None:
        return
    await _edit_or_answer_admin(message, body, panel_status_keyboard(view.reconnectable))


@router.callback_query(F.data == AdminCB.PANEL_STATUS)
async def admin_panel_status(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    await callback.answer()
    await _render_panel_status(callback, session, settings)


@router.callback_query(F.data.startswith(AdminCB.PANEL_NODE_RECONNECT))
async def admin_panel_node_reconnect(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "").removeprefix(AdminCB.PANEL_NODE_RECONNECT)
    if not raw.isdigit():
        await callback.answer()
        return
    await callback.answer()
    line = READ_FAILED_LINE
    try:
        async with _panel(session, settings) as client:
            _ok, line = await reconnect_node_result(client, int(raw))
    except Exception:
        logger.warning("Panel node reconnect: no panel client")
    await _render_panel_status(callback, session, settings, prefix=line)


@router.callback_query(F.data == AdminCB.PANEL_CLEAR)
async def admin_panel_clear_confirm(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    """Preview only. Nothing is written until PANEL_CLEAR_OK."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_panel_write(callback, settings):
        return
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    current = await _format_panel_settings(session, settings)
    await _edit_or_send(
        message,
        texts.admin_panel_clear_confirm.format(current=current),
        confirm_keyboard(
            confirm_cb=AdminCB.PANEL_CLEAR_OK,
            confirm_label=texts.admin_confirm_yes,
            back_to=AdminCB.PANEL,
            back_label="تنظیمات پنل",
        ),
    )


@router.callback_query(F.data == AdminCB.PANEL_CLEAR_OK)
async def admin_panel_clear(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_panel_write(callback, settings):
        return
    admin_id = db_user.id if db_user is not None else None
    try:
        await clear_to_env(session, admin_user_id=admin_id)
        await session.commit()
    except Exception:
        logger.exception("panel settings clear failed")
        await session.rollback()
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    await state.clear()
    await callback.answer(texts.admin_panel_cleared)
    message = _tg_message(callback)
    if message is None:
        return
    text, markup = await _panel_screen(
        session,
        settings,
        callback.from_user.id if callback.from_user else None,
        texts.admin_panel_cleared,
    )
    await _edit_or_answer_admin(message, text, markup)


# ---- Live panel group list --------------------------------------------------


@router.callback_query(F.data == AdminCB.PANEL_GROUPS_LIST)
async def admin_panel_groups_list(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    groups, err = await _fetch_panel_groups(session, settings)
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    if err is not None:
        text = texts.admin_panel_groups_list_fail.format(error=escape(err))
    elif not groups:
        text = texts.admin_panel_groups_list_empty
    else:
        used = await _store_groups_by_panel_group(session, settings)
        lines = [texts.admin_panel_groups_list_title.format(total=len(groups))]
        lines += [_panel_group_line(g, used.get(g.id, [])) for g in groups]
        text = "\n".join(lines)
    await _edit_or_answer_admin(message, text, panel_groups_keyboard())


# --------------------------------------------------------------------------- #
# Customer desk
#
# Lookup could only take an order id, but a customer writing in gives a
# @username — or nothing. Manual wallet changes go through the normal ledger so
# a hand-made top-up leaves the same trail as an automatic one.
# --------------------------------------------------------------------------- #


def _retail_by_slug_from_view(view: Any) -> dict[str, int]:
    prices = list(getattr(view, "prices", None) or [])
    out: dict[str, int] = {}
    for plan in getattr(view, "packages", []) or []:
        cell = get_price(
            prices,
            slug=str(plan.slug),
            duration_days=duration_from_package_options(dict(plan.options or {})),
            tier=DEFAULT_TIER,
        )
        if cell is not None:
            out[str(plan.slug)] = int(cell)
    return out


def _is_ready_plan(package: Any) -> bool:
    return is_ready_plan(package)


_ORDER_STATUS_FA: dict[str, str] = {
    "PENDING_PAYMENT": "در انتظار پرداخت",
    "PAYMENT_REVIEW": "بررسی پرداخت",
    "PAYMENT_APPROVED": "تأییدشده",
    "PROVISIONING": "در حال ساخت",
    "ACTIVE": "فعال",
    "REJECTED": "ردشده",
    "PROVISION_FAILED": "ناموفق در پنل",
    "EXPIRED": "منقضی",
}


# One word each, for order buttons: «در انتظار پرداخت» made a 50-character
# label that a phone cuts. The order screen still shows the full status.
_ORDER_STATUS_SHORT_FA: dict[str, str] = {
    "PENDING_PAYMENT": "منتظر",
    "PAYMENT_REVIEW": "بررسی",
    "PAYMENT_APPROVED": "تأیید",
    "PROVISIONING": "ساخت",
    "ACTIVE": "فعال",
    "REJECTED": "رد",
    "PROVISION_FAILED": "خطا",
    "EXPIRED": "منقضی",
}


def _customer_name(user: User) -> str:
    return customer_name(user)


# Kept under its old name: the reseller tests import it.
_role_label = role_label


def _ledger_lines(facts: CardFacts) -> str:
    if not facts.ledger:
        return texts.admin_customer_ledger_empty
    lines = [texts.admin_customer_ledger_title]
    lines.extend(f"  {row.sign}{row.amount} • {row.label}" for row in facts.ledger)
    return "\n".join(lines)


def _card_text(card: CustomerCard) -> str:
    facts = card_facts(card, escape)
    return texts.admin_customer_card.format(
        name=facts.name,
        handle=facts.handle,
        telegram_id=facts.telegram_id,
        status=facts.status,
        role=facts.role,
        joined=facts.joined,
        last_seen=facts.last_seen,
        trial=facts.trial,
        wallet=facts.wallet,
        topped_up=facts.topped_up,
        orders_total=facts.orders_total,
        orders_paid=facts.orders_paid,
        spent=facts.spent,
        last_order=facts.last_order,
        referrals=facts.referrals,
        referrer=facts.referrer,
        referral_code=facts.referral_code,
        services_count=facts.services_count,
        ledger=_ledger_lines(facts),
    )


async def _show_customer(message: Message, session: AsyncSession, user_id: int) -> None:
    card = await get_customer_card(session, user_id)
    body = _card_text(card)
    markup = customer_card_keyboard(
        user_id,
        banned=card.is_banned,
        services=card.services,
        trial_taken=card.has_taken_trial,
        reseller=bool(card.user.is_reseller),
        wallet_toman=int(card.wallet_balance_toman or 0),
        orders_total=int(card.orders_total or 0),
        legacy_records_total=card.legacy_records_total,
        referrer_id=int(card.user.referred_by_id) if card.user.referred_by_id else None,
    )
    await end_read_transaction(session)
    await _edit_or_answer_admin(message, body, markup)


async def _edit_or_answer_admin(message: Message, body: str, markup: InlineKeyboardMarkup) -> None:
    await edit_or_send(message, body, markup)


_RESELLERS_LIST_CAP = 30


@router.callback_query(F.data == AdminCB.RESELLERS)
async def admin_resellers_list(
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
    if message is None:
        return
    result = await session.execute(select(User).where(User.is_reseller.is_(True)).order_by(User.id))
    all_resellers = list(result.scalars().all())
    if not all_resellers:
        await _edit_or_answer_admin(
            message, texts.admin_resellers_empty, admin_back_home_keyboard()
        )
        return
    shown = all_resellers[:_RESELLERS_LIST_CAP]
    omitted = len(all_resellers) - len(shown)
    body = texts.admin_resellers_title.format(count=to_persian_digits(len(all_resellers)))
    if omitted > 0:
        body = f"{body}\n\n{texts.admin_resellers_omitted.format(n=to_persian_digits(omitted))}"
    stats: dict[int, tuple[int, int, int]] = {}
    for shop in shown:
        quota = await get_reseller_trial_quota(session, shop.id)
        stats[shop.id] = (
            await reseller_month_sales(session, shop.id),
            quota.used,
            quota.quota,
        )
    body = f"{body}\n{texts.admin_resellers_month_hint}"
    await _edit_or_answer_admin(message, body, admin_resellers_keyboard(shown, stats))


async def _customer_id_from(callback: CallbackQuery, prefix: str) -> int | None:
    raw = (callback.data or "")[len(prefix) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_panel_invalid, show_alert=True)
        return None
    return int(raw)


_CUSTOMER_ORDERS_PAGE = 8


@router.callback_query(F.data.startswith(AdminCB.CUST_ORDERS))
async def admin_customer_orders(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """Their orders as buttons, newest first; a tap opens the order."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(AdminCB.CUST_ORDERS) :]
    uid_raw, _, off_raw = raw.partition(":")
    if not uid_raw.isdigit() or (off_raw and not off_raw.isdigit()):
        await callback.answer(texts.admin_panel_invalid, show_alert=True)
        return
    user_id, offset = int(uid_raw), int(off_raw or 0)
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    total = await count_customer_orders(session, user_id)
    if not total:
        await _edit_or_answer_admin(
            message, texts.admin_customer_orders_empty, back_to_customer_keyboard(user_id)
        )
        return
    offset = min(offset, max(0, total - 1) // _CUSTOMER_ORDERS_PAGE * _CUSTOMER_ORDERS_PAGE)
    orders = await list_customer_orders(
        session, user_id, limit=_CUSTOMER_ORDERS_PAGE, offset=offset
    )
    rows = []
    for order in orders:
        status = str(getattr(order.status, "value", order.status))
        rows.append(
            (order.id, customer_order_label(order, _ORDER_STATUS_SHORT_FA.get(status, status)))
        )
    body = texts.admin_customer_orders_title.format(
        total=to_persian_digits(total),
        page=to_persian_digits(offset // _CUSTOMER_ORDERS_PAGE + 1),
        pages=to_persian_digits((total - 1) // _CUSTOMER_ORDERS_PAGE + 1),
    )
    await _edit_or_answer_admin(
        message,
        body,
        customer_orders_keyboard(
            user_id, rows, offset=offset, limit=_CUSTOMER_ORDERS_PAGE, total=total
        ),
    )


def _uid_and_offset(callback: CallbackQuery, prefix: str) -> tuple[int, int] | None:
    """``{user id}[:{offset}]`` from a paged callback, or None if malformed."""
    uid_raw, _, off_raw = (callback.data or "")[len(prefix) :].partition(":")
    if not uid_raw.isdigit() or (off_raw and not off_raw.isdigit()):
        return None
    return int(uid_raw), int(off_raw or 0)


_CUSTOMER_SERVICES_PAGE = 8


async def _live_snapshots(
    session: AsyncSession, settings: Settings, account_ids: list[int]
) -> dict[int, ServiceSnapshot | None]:
    """One panel read per row, in one client. A failure is ⚪️, never an error."""
    found: dict[int, ServiceSnapshot | None] = dict.fromkeys(account_ids)
    try:
        async with _panel(session, settings) as client:
            for account_id in account_ids:
                try:
                    found[account_id] = await get_snapshot(session, client, account_id=account_id)
                except Exception:
                    logger.warning("service list: live read failed", exc_info=True)
    except Exception:
        logger.warning("service list: no panel client", exc_info=True)
    return found


@router.callback_query(F.data.startswith(AdminCB.CUST_SERVICES))
async def admin_customer_services(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    parsed = _uid_and_offset(callback, AdminCB.CUST_SERVICES)
    if parsed is None:
        await callback.answer(texts.admin_panel_invalid, show_alert=True)
        return
    user_id, offset = parsed
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    card = await get_customer_card(session, user_id)
    if not card.services:
        await _edit_or_answer_admin(
            message, texts.admin_customer_services_empty, back_to_customer_keyboard(user_id)
        )
        return
    total = len(card.services)
    offset = min(offset, (total - 1) // _CUSTOMER_SERVICES_PAGE * _CUSTOMER_SERVICES_PAGE)
    page = card.services[offset : offset + _CUSTOMER_SERVICES_PAGE]
    snapshots = await _live_snapshots(session, settings, [svc.account_id for svc in page])
    await _edit_or_answer_admin(
        message,
        texts.admin_customer_services_title.format(n=to_persian_digits(total)),
        customer_services_keyboard(
            user_id,
            page,
            snapshots=snapshots,
            offset=offset,
            limit=_CUSTOMER_SERVICES_PAGE,
            total=total,
        ),
    )


_CUSTOMER_REFS_PAGE = 8


@router.callback_query(F.data.startswith(AdminCB.CUST_REFS))
async def admin_customer_referrals(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    parsed = _uid_and_offset(callback, AdminCB.CUST_REFS)
    if parsed is None:
        await callback.answer(texts.admin_panel_invalid, show_alert=True)
        return
    user_id, offset = parsed
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    total = await count_customer_referrals(session, user_id)
    if not total:
        await _edit_or_answer_admin(
            message, texts.admin_customer_refs_empty, back_to_customer_keyboard(user_id)
        )
        return
    offset = min(offset, (total - 1) // _CUSTOMER_REFS_PAGE * _CUSTOMER_REFS_PAGE)
    people = await list_customer_referrals(
        session, user_id, limit=_CUSTOMER_REFS_PAGE, offset=offset
    )
    await _edit_or_answer_admin(
        message,
        texts.admin_customer_refs_title.format(
            total=to_persian_digits(total),
            page=to_persian_digits(offset // _CUSTOMER_REFS_PAGE + 1),
            pages=to_persian_digits((total - 1) // _CUSTOMER_REFS_PAGE + 1),
        ),
        customer_referrals_keyboard(
            user_id, people, offset=offset, limit=_CUSTOMER_REFS_PAGE, total=total
        ),
    )


# ---- ban / unban from the customer card ------------------------------------


@router.callback_query(F.data.startswith(AdminCB.CUST_BAN_OK))
async def admin_customer_ban_apply(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """Apply the choice, then land back on the card — not on a dead-end message.

    Modes: ``b``/``s`` ban (bot only / bot and services), ``ub``/``us`` unban.
    The direction rides in the callback so a double tap cannot flip it.
    """
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    uid_raw, _, mode = (callback.data or "")[len(AdminCB.CUST_BAN_OK) :].partition(":")
    if not uid_raw.isdigit() or mode not in {"b", "s", "ub", "us"}:
        await callback.answer(texts.admin_panel_invalid, show_alert=True)
        return
    user_id = int(uid_raw)
    ban = mode in {"b", "s"}
    try:
        await set_user_banned(session, user_id, ban, admin_telegram_ids=settings.admin_telegram_ids)
        await session.commit()
    except AdminError as exc:
        await session.rollback()
        await callback.answer(
            (
                texts.admin_cannot_ban_admin
                if str(exc) == "cannot_ban_admin"
                else texts.admin_user_not_found
            ),
            show_alert=True,
        )
        return
    note = texts.admin_customer_ban_done if ban else texts.admin_customer_unban_done
    if mode in {"s", "us"}:
        try:
            async with _panel(session, settings) as client:
                done, failed = await set_user_services_enabled(
                    session, client, user_id=user_id, enabled=not ban
                )
        except Exception:
            logger.warning("ban with services: no panel client", exc_info=True)
            accounts = await get_customer_card(session, user_id)
            done, failed = 0, len(accounts.services)
        note += "\n" + texts.admin_customer_services_result.format(
            done=to_persian_digits(done), verb="قطع" if ban else "وصل"
        )
        if failed:
            note += texts.admin_customer_services_failed.format(failed=to_persian_digits(failed))
    await callback.answer(note, show_alert=bool(mode in {"s", "us"}))
    message = _tg_message(callback)
    if message is not None:
        await _show_customer(message, session, user_id)


@router.callback_query(F.data.startswith(AdminCB.CUST_BAN))
async def admin_customer_ban_confirm(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    user_id = await _customer_id_from(callback, AdminCB.CUST_BAN)
    if user_id is None:
        return
    card = await get_customer_card(session, user_id)
    unban = card.is_banned
    if not unban and (
        card.user.is_admin or card.user.telegram_id in set(settings.admin_telegram_ids)
    ):
        await callback.answer(texts.admin_cannot_ban_admin, show_alert=True)
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    ask = texts.admin_customer_unban_ask if unban else texts.admin_customer_ban_ask
    await _edit_or_answer_admin(
        message,
        ask.format(name=escape(_customer_name(card.user)), n=to_persian_digits(len(card.services))),
        customer_ban_confirm_keyboard(user_id, unban=unban, services_count=len(card.services)),
    )


@router.callback_query(F.data.startswith(AdminCB.CUST_TRIAL_RESET))
async def admin_customer_trial_reset(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """Give someone their free trial back.

    Only clears the claim flag — the trial order and any live panel account
    stay, because they are what actually happened.
    """
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    user_id = await _customer_id_from(callback, AdminCB.CUST_TRIAL_RESET)
    if user_id is None:
        return
    try:
        await reset_trial(session, user_id)
        await session.commit()
    except ManualAdjustError as exc:
        await session.rollback()
        await callback.answer(str(exc)[:190], show_alert=True)
        return
    await callback.answer(texts.admin_customer_trial_reset_done)
    message = _tg_message(callback)
    if message is not None:
        await _show_customer(message, session, user_id)


@router.callback_query(F.data.startswith(AdminCB.CUST_DM))
async def admin_customer_dm_start(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """Answer a customer from their own card, instead of hunting for their chat."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    user_id = await _customer_id_from(callback, AdminCB.CUST_DM)
    if user_id is None:
        return
    card = await get_customer_card(session, user_id)
    await state.set_state(AdminStates.waiting_customer_dm)
    await state.update_data(dm_user_id=user_id)
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _edit_or_answer_admin(
        message,
        texts.admin_customer_dm_prompt.format(name=escape(_customer_name(card.user))),
        admin_cancel_input_keyboard(),
    )


@router.message(AdminStates.waiting_customer_dm)
async def admin_customer_dm_send(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    db_user: User | None,
) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None, settings, db_user):
        await state.clear()
        return
    data = await state.get_data()
    user_id = data.get("dm_user_id")
    body = (message.text or "").strip()
    if not user_id or not body:
        await message.answer(texts.admin_panel_invalid, reply_markup=admin_cancel_input_keyboard())
        return
    card = await get_customer_card(session, int(user_id))
    await state.clear()
    try:
        await bot.send_message(card.telegram_id, body)
    except Exception:
        logger.exception("Admin DM to user %s failed", user_id)
        await message.answer(
            texts.admin_customer_dm_failed, reply_markup=back_to_customer_keyboard(int(user_id))
        )
        return
    await message.answer(
        texts.admin_customer_dm_sent, reply_markup=back_to_customer_keyboard(int(user_id))
    )


@router.callback_query(F.data.startswith(AdminCB.CUST_RESELLER))
async def admin_toggle_reseller(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """Grant or revoke the reseller role by hand.

    There is no request form: the owner knows every shop he works with
    personally, and the previous bot's «درخواست نمایندگی» table sat empty for
    its whole life. Revoking leaves the agreed prices in place, so a shop that
    comes back does not have to be re-priced from scratch. Revoking asks first
    (``admin_revoke_reseller`` does it); granting stays one tap.
    """
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    user_id = await _customer_id_from(callback, AdminCB.CUST_RESELLER)
    if user_id is None:
        return
    user = await session.get(User, user_id)
    if user is None:
        await callback.answer(texts.admin_customer_not_found, show_alert=True)
        return
    message = _tg_message(callback)
    if user.is_reseller:
        # Removing the role takes effect at once, so it asks first.
        await callback.answer()
        if message is not None:
            await _edit_or_answer_admin(
                message,
                texts.admin_reseller_revoke_confirm.format(name=escape(_customer_name(user))),
                reseller_revoke_confirm_keyboard(user_id),
            )
        return
    user.is_reseller = True
    await session.commit()
    await callback.answer(texts.admin_reseller_granted)
    if message is not None:
        await _show_customer(message, session, user_id)


@router.callback_query(F.data.startswith(AdminCB.CUST_RESELLER_OK))
async def admin_revoke_reseller(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """The confirmed removal. A tap on someone who is no longer a reseller writes nothing."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    user_id = await _customer_id_from(callback, AdminCB.CUST_RESELLER_OK)
    if user_id is None:
        return
    user = await session.get(User, user_id)
    if user is None:
        await callback.answer(texts.admin_customer_not_found, show_alert=True)
        return
    message = _tg_message(callback)
    if not user.is_reseller:
        await callback.answer(texts.admin_reseller_already_not, show_alert=True)
    else:
        user.is_reseller = False
        await session.commit()
        await callback.answer(texts.admin_reseller_revoked)
    if message is not None:
        await _show_customer(message, session, user_id)


# ---- 🎁 create a service for a customer --------------------------------------

_GIFT_ERRORS: dict[str, str] = {
    "open_order": texts.admin_gift_err_open_order,
    "package_gone": texts.admin_gift_err_group,
    "group_off_sale": texts.admin_gift_err_group,
    "no_panel_group": texts.admin_gift_err_group,
    "incomplete_plan": texts.admin_gift_err_group,
    "no_price": texts.admin_gift_err_price,
    "reseller_wallet": texts.admin_gift_err_reseller_wallet,
    "wallet_short": texts.admin_gift_err_wallet_short,
    "key_conflict": texts.admin_gift_err_key,
    "order_not_resumable": texts.admin_gift_err_key,
    "bad_key": texts.admin_gift_err_key,
}


async def _gift_ids(callback: CallbackQuery, prefix: str) -> tuple[int, int] | None:
    """``{user id}:{second id}`` from a gift callback, or None (already answered)."""
    first, _, second = (callback.data or "")[len(prefix) :].partition(":")
    if not first.isdigit() or not second.isdigit():
        await callback.answer(texts.admin_panel_invalid, show_alert=True)
        return None
    return int(first), int(second)


@router.callback_query(F.data.startswith(AdminCB.CUST_GIFT_GROUP))
async def admin_gift_group(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    ids = await _gift_ids(callback, AdminCB.CUST_GIFT_GROUP)
    if ids is None:
        return
    user_id, cat_id = ids
    view = await published_catalog_view(session)
    cat = view.category(cat_id)
    message = _tg_message(callback)
    if cat is None or message is None or cat.id not in {c.id for c in sellable_groups(view)}:
        await callback.answer(texts.admin_gift_err_group, show_alert=True)
        return
    card = await get_customer_card(session, user_id)
    await callback.answer()
    plans = [
        (int(pkg.id), plan_button_label(pkg, price)) for pkg, price in sellable_plans(view, cat)
    ]
    await _edit_or_answer_admin(
        message,
        texts.admin_gift_pick_plan.format(
            name=escape(_customer_name(card.user)), group=escape(group_label(view, cat))
        ),
        gift_plans_keyboard(user_id, plans),
    )


@router.callback_query(F.data.startswith(AdminCB.CUST_GIFT_PLAN))
async def admin_gift_plan(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    ids = await _gift_ids(callback, AdminCB.CUST_GIFT_PLAN)
    if ids is None:
        return
    user_id, pkg_id = ids
    view = await published_catalog_view(session)
    pkg = view.package(pkg_id)
    message = _tg_message(callback)
    if pkg is None or message is None:
        await callback.answer(texts.admin_gift_err_group, show_alert=True)
        return
    try:
        offer = await resolve_offer(session, settings, package_slug=str(pkg.slug), view=view)
    except AdminServiceError as exc:
        await callback.answer(
            _GIFT_ERRORS.get(exc.code, texts.admin_gift_err_generic), show_alert=True
        )
        return
    await state.set_state(AdminStates.waiting_gift_service_name)
    await state.update_data(gift_user_id=user_id, gift_slug=offer.package_slug)
    await callback.answer()
    await _edit_or_answer_admin(
        message,
        texts.admin_gift_ask_name.format(plan=escape(offer.package_title)),
        back_to_customer_keyboard(user_id),
    )


@router.message(AdminStates.waiting_gift_service_name)
async def admin_gift_name(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None, settings, db_user):
        await state.clear()
        return
    data = await state.get_data()
    user_id = data.get("gift_user_id")
    slug = data.get("gift_slug")
    raw = (message.text or "").strip()
    if not user_id or not slug:
        await state.clear()
        await message.answer(texts.admin_gift_expired, reply_markup=admin_back_home_keyboard())
        return
    try:
        name = sanitize_service_name(raw)
    except ValueError:
        await message.answer(
            texts.admin_gift_name_bad, reply_markup=back_to_customer_keyboard(int(user_id))
        )
        return
    try:
        offer = await resolve_offer(session, settings, package_slug=str(slug))
    except AdminServiceError as exc:
        await message.answer(
            _GIFT_ERRORS.get(exc.code, texts.admin_gift_err_generic),
            reply_markup=back_to_customer_keyboard(int(user_id)),
        )
        return
    card = await get_customer_card(session, int(user_id))
    balance = int(card.wallet_balance_toman or 0)
    is_reseller = bool(card.user.is_reseller)
    wallet_ok = wallet_option_available(
        balance_toman=balance, retail_toman=offer.retail_toman, is_reseller=is_reseller
    )
    await state.set_state(None)  # keep the data; a stray text is not a name any more
    await state.update_data(gift_name=name)
    await message.answer(
        texts.admin_gift_confirm.format(
            name=escape(_customer_name(card.user)),
            group=escape(offer.category_title),
            plan=escape(offer.package_title),
            service=escape(name),
            cleaned=(
                texts.admin_gift_name_cleaned.format(raw=escape(raw[:32])) if name != raw else ""
            ),
            price=_fmt_toman(offer.retail_toman),
            wallet=_fmt_toman(balance),
            reseller_note=texts.admin_gift_reseller_note if is_reseller else "",
        ),
        reply_markup=gift_confirm_keyboard(
            int(user_id),
            request_key=new_request_key(),
            wallet_label=(
                texts.admin_gift_wallet_button.format(price=_fmt_toman(offer.retail_toman))
                if wallet_ok
                else None
            ),
        ),
    )


@router.callback_query(F.data.startswith(AdminCB.CUST_GIFT_GO))
async def admin_gift_go(  # noqa: PLR0911 - one exit per refusal keeps each message explicit
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    db_user: User | None,
) -> None:
    """Create the service. The key in the callback makes a second tap the same request."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(AdminCB.CUST_GIFT_GO) :]
    uid_raw, _, rest = raw.partition(":")
    mode, _, key = rest.partition(":")
    if not uid_raw.isdigit() or mode not in {"g", "w"} or not key:
        await callback.answer(texts.admin_panel_invalid, show_alert=True)
        return
    user_id = int(uid_raw)
    message = _tg_message(callback)
    if message is None:
        await callback.answer()
        return
    data = await state.get_data()
    slug, name = data.get("gift_slug"), data.get("gift_name")
    if not slug or not name or int(data.get("gift_user_id") or 0) != user_id:
        # The screen outlived its state — usually a second tap after success.
        done = await find_order_by_key(session, key)
        await callback.answer(
            texts.admin_gift_done if done is not None else texts.admin_gift_expired,
            show_alert=True,
        )
        return
    await callback.answer()
    try:
        async with _panel(session, settings) as client:
            account = await create_service_for_customer(
                session,
                client,
                settings,
                user_id=user_id,
                package_slug=str(slug),
                service_name=str(name),
                charge_wallet=mode == "w",
                request_key=key,
            )
    except AdminServiceError as exc:
        if exc.code == "provision_failed_refunded":
            await state.clear()
            await message.answer(
                texts.admin_gift_provision_failed_refunded.format(
                    amount=_fmt_toman(exc.refunded_toman)
                ),
                reply_markup=back_to_customer_keyboard(user_id),
            )
            return
        if exc.code == "provision_failed":
            # The state stays: the retry button is this same callback.
            await message.answer(
                texts.admin_gift_provision_failed,
                reply_markup=gift_retry_keyboard(user_id, callback.data or ""),
            )
            return
        await message.answer(
            _GIFT_ERRORS.get(exc.code, texts.admin_gift_err_generic),
            reply_markup=back_to_customer_keyboard(user_id),
        )
        return
    except Exception:
        logger.exception("Admin-created service failed for user %s", user_id)
        await session.rollback()
        await message.answer(
            texts.admin_gift_err_generic, reply_markup=back_to_customer_keyboard(user_id)
        )
        return
    await state.clear()
    card = await get_customer_card(session, user_id)
    delivered = False
    completed_order = await find_order_by_key(session, key)
    try:
        assert completed_order is not None, "created_order_missing"
        delivered = await deliver_provisioned_service_card(
            session,
            settings,
            user_id=user_id,
            account_id=account.id,
            order_id=completed_order.id,
            bot=bot,
            chat_id=card.telegram_id,
            intro=texts.notify_admin_service_created,
        )
    except Exception:
        logger.exception("Could not deliver the admin-created service to user %s", user_id)
    await message.answer(
        texts.admin_gift_done if delivered else texts.admin_gift_done_undelivered,
        reply_markup=back_to_customer_keyboard(user_id),
    )
    await _show_customer(message, session, user_id)


@router.callback_query(F.data.startswith(AdminCB.CUST_GIFT))
async def admin_gift_start(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """Pick a store group that is really on sale."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    user_id = await _customer_id_from(callback, AdminCB.CUST_GIFT)
    if user_id is None:
        return
    card = await get_customer_card(session, user_id)
    if await get_open_order(session, user_id) is not None:
        await callback.answer(texts.admin_gift_err_open_order, show_alert=True)
        return
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    view = await published_catalog_view(session)
    groups = sellable_groups(view)
    if not groups:
        await _edit_or_answer_admin(
            message, texts.admin_gift_no_groups, back_to_customer_keyboard(user_id)
        )
        return
    await _edit_or_answer_admin(
        message,
        texts.admin_gift_pick_group.format(name=escape(_customer_name(card.user))),
        gift_groups_keyboard(user_id, [(int(c.id), group_label(view, c)) for c in groups]),
    )


async def _ready_plans_by_group(session: AsyncSession) -> list[tuple[str, list[Any]]]:
    """Ready plans, grouped under their product group.

    A flat list is unreadable once two groups sell the same sizes at different
    prices — «۵۰ گیگ / ۳۰ روز» twice, with no way to tell ویژه from اقتصادی.
    """
    view = await get_catalog_view(session)
    groups: list[tuple[str, list[Any]]] = []
    for cat in view.categories:
        plans = [p for p in view.packages_of(cat.id) if _is_ready_plan(p)]
        if plans:
            groups.append((str(cat.title_fa), plans))
    return groups


async def _reseller_status_block(session: AsyncSession, user_id: int, card: Any) -> str:
    """The shop at a glance, above its three price sections."""
    return await status_block(session, user_id, wallet_toman=int(card.wallet_balance_toman or 0))


async def _show_reseller_panel(message: Message, session: AsyncSession, user_id: int) -> None:
    groups = await _ready_plans_by_group(session)
    plan_count = sum(len(plans) for _, plans in groups)
    from sqlalchemy import func, select

    from database.models import ResellerPlanPrice

    priced_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ResellerPlanPrice)
                .where(ResellerPlanPrice.user_id == user_id)
            )
        ).scalar_one()
        or 0
    )
    card = await get_customer_card(session, user_id)
    body = texts.admin_reseller_panel.format(name=escape(_customer_name(card.user)))
    body = f"{body}\n\n{await _reseller_status_block(session, user_id, card)}"
    if not plan_count:
        body = f"{body}\n\n{texts.admin_reseller_no_plans}"
    await _edit_or_answer_admin(
        message,
        body,
        reseller_panel_keyboard(
            user_id,
            priced_count=priced_count,
            plan_count=plan_count,
        ),
    )


async def _reseller_screen_plans(
    session: AsyncSession, data: dict[str, Any]
) -> tuple[bool, list[tuple[Any, int]]]:
    """Whether the matrix screen is a flat list, and its rows as ``(plan, retail)``.

    A percent cut and its undo are scoped to exactly these plans (see
    ``services.reseller_admin.screen_cells``).
    """
    cat_id = data.get("rs_category_id")
    if cat_id is None:
        return False, []
    return screen_cells(
        await get_catalog_view(session),
        category_id=int(cat_id),
        duration=data.get("rs_sel_duration_days"),
        tier=str(data.get("rs_sel_tier") or DEFAULT_TIER),
    )


async def _reseller_matrix_context(
    session: AsyncSession,
    state: FSMContext,
    user_id: int,
) -> dict[str, Any]:
    """Resolve category + duration + tier for the reseller matrix screen."""
    view = await get_catalog_view(session)
    cats = [(int(c.id), str(c.title_fa), c) for c in view.categories]
    cats = [
        (cid, title, c)
        for cid, title, c in cats
        if any(_is_ready_plan(p) for p in view.packages_of(cid))
    ]
    data = await state.get_data()
    cat_id = data.get("rs_category_id")
    cat_obj = None
    if cat_id is not None:
        cat_obj = next((c for cid, _t, c in cats if int(cid) == int(cat_id)), None)
    if cat_obj is None and cats:
        cat_obj = cats[0][2]
        cat_id = int(cats[0][0])
    is_flat = flat_plan_list_enabled(cat_obj) if cat_obj is not None else False
    durs = category_durations(cat_obj) if cat_obj is not None else [30]
    tiers = category_tiers(cat_obj) if cat_obj is not None else [DEFAULT_TIER]
    sel_duration = data.get("rs_sel_duration_days")
    if "rs_sel_duration_days" not in data:
        sel_duration = durs[0] if durs else 30
    sel_tier = str(data.get("rs_sel_tier") or (tiers[0] if tiers else DEFAULT_TIER))
    await state.update_data(
        rs_user_id=user_id,
        rs_category_id=cat_id,
        rs_sel_duration_days=sel_duration,
        rs_sel_tier=sel_tier,
        rs_flat=is_flat,
    )
    _flat, rows = await _reseller_screen_plans(session, await state.get_data())
    visible = [pkg for pkg, _retail in rows]
    retail = {str(pkg.slug): price for pkg, price in rows}
    agreed: dict[str, int] = {}
    if is_flat:
        for pkg in visible:
            plan_dur = duration_from_package_options(dict(pkg.options or {}))
            agreed_price = await get_plan_price(
                session, user_id, str(pkg.slug), duration_days=plan_dur, tier=DEFAULT_TIER
            )
            if agreed_price is not None:
                agreed[str(pkg.slug)] = int(agreed_price)
        kb_durs: list[int | None] = []
        kb_tiers = [DEFAULT_TIER]
    else:
        agreed = await list_plan_prices(session, user_id, duration_days=sel_duration, tier=sel_tier)
        kb_durs = list[int | None](durs)
        kb_tiers = tiers
    can_undo = bool(
        await undo_ops(
            session, user_id, flat=is_flat, rows=rows, duration=sel_duration, tier=sel_tier
        )
    )
    card = await get_customer_card(session, user_id)
    title = texts.admin_reseller_matrix_title.format(name=escape(_customer_name(card.user)))
    body = title
    if is_flat:
        body = f"{body}\n\n{texts.admin_reseller_flat_hint}"
    if not visible:
        body = f"{body}\n\n{texts.admin_reseller_no_plans_tab}"
    markup = reseller_matrix_keyboard(
        user_id=user_id,
        packages=visible,
        retail_by_slug=retail,
        agreed_by_slug=agreed,
        durations=kb_durs,
        tiers=kb_tiers,
        selected_duration=sel_duration,
        selected_tier=sel_tier if not is_flat else DEFAULT_TIER,
        can_undo=can_undo,
        categories=[(cid, title) for cid, title, _c in cats],
        selected_category_id=int(cat_id) if cat_id is not None else None,
        flat=is_flat,
    )
    return {
        "body": body,
        "markup": markup,
        "view": view,
        "visible": visible,
        "retail": retail,
        "agreed": agreed,
        "sel_duration": sel_duration,
        "sel_tier": sel_tier,
    }


async def _show_reseller_matrix(
    message: Message, session: AsyncSession, state: FSMContext, user_id: int
) -> None:
    ctx = await _reseller_matrix_context(session, state, user_id)
    await _edit_or_answer_admin(message, ctx["body"], ctx["markup"])


@router.callback_query(F.data == AdminCB.NOOP)
async def admin_noop(callback: CallbackQuery) -> None:
    """A group header is a label, not a button — answer so it does not spin."""
    await callback.answer()


@router.callback_query(F.data.startswith(AdminCB.RS_SEC))
async def admin_reseller_section(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """Three unrelated decisions, three screens: per-gigabyte, trials, plans."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(AdminCB.RS_SEC) :]
    user_raw, _, section = raw.partition(":")
    if not user_raw.isdigit():
        await callback.answer(texts.admin_panel_invalid, show_alert=True)
        return
    user_id = int(user_raw)
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return

    if section in {"custom", "trial"}:
        rate = await get_rate(session, user_id)
        body = (
            texts.admin_reseller_custom_intro
            if section == "custom"
            else texts.admin_reseller_trial_intro
        )
        markup = (
            reseller_custom_rate_keyboard(user_id, rate=rate)
            if section == "custom"
            else reseller_trial_settings_keyboard(user_id, rate=rate)
        )
        await _edit_or_answer_admin(message, body, markup)
        return

    if section == "plans":
        groups = await _ready_plans_by_group(session)
        if not groups:
            await _edit_or_answer_admin(
                message, texts.admin_reseller_no_plans, back_to_customer_keyboard(user_id)
            )
            return
        await state.update_data(rs_user_id=user_id)
        await _show_reseller_matrix(message, session, state, user_id)
        return

    await _edit_or_answer_admin(
        message, texts.admin_panel_invalid, back_to_customer_keyboard(user_id)
    )


@router.callback_query(F.data.startswith(AdminCB.RS_PANEL))
async def admin_reseller_panel(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    user_id = await _customer_id_from(callback, AdminCB.RS_PANEL)
    if user_id is None:
        return
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await _show_reseller_panel(message, session, user_id)


_RATE_FIELD_PROMPTS = {
    "gb": (RATE_FIELD_ATTRS["gb"], "admin_reseller_prompt_gb"),
    "day": (RATE_FIELD_ATTRS["day"], "admin_reseller_prompt_day"),
    "quota": (RATE_FIELD_ATTRS["quota"], "admin_reseller_prompt_quota"),
    "tprice": (RATE_FIELD_ATTRS["tprice"], "admin_reseller_prompt_tprice"),
}


@router.callback_query(F.data.startswith(AdminCB.RS_RATE))
async def admin_reseller_rate_ask(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(AdminCB.RS_RATE) :]
    user_raw, _, field = raw.partition(":")
    if not user_raw.isdigit() or field not in _RATE_FIELD_PROMPTS:
        await callback.answer(texts.admin_panel_invalid, show_alert=True)
        return
    await state.set_state(AdminStates.waiting_reseller_number)
    await state.update_data(rs_user_id=int(user_raw), rs_field=field)
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    _, prompt_key = _RATE_FIELD_PROMPTS[field]
    await _edit_or_answer_admin(message, getattr(texts, prompt_key), admin_cancel_input_keyboard())


@router.callback_query(F.data.startswith(AdminCB.RS_CAT))
async def admin_reseller_matrix_cat(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(AdminCB.RS_CAT) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_panel_invalid, show_alert=True)
        return
    data = await state.get_data()
    user_id = data.get("rs_user_id")
    if not user_id:
        await callback.answer(texts.admin_panel_invalid, show_alert=True)
        return
    await state.update_data(rs_category_id=int(raw))
    # Reset duration/tier defaults for the new category on next render
    await state.update_data(rs_sel_duration_days=None)
    data2 = await state.get_data()
    # clear the sentinel so context picks first duration
    await state.set_data({k: v for k, v in data2.items() if k != "rs_sel_duration_days"})
    await state.update_data(rs_category_id=int(raw), rs_user_id=int(user_id))
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await _show_reseller_matrix(message, session, state, int(user_id))


@router.callback_query(F.data.startswith(AdminCB.RS_DUR))
async def admin_reseller_matrix_dur(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    token = (callback.data or "")[len(AdminCB.RS_DUR) :]
    selected = parse_duration_cb(token)
    data = await state.get_data()
    user_id = data.get("rs_user_id")
    if not user_id:
        await callback.answer(texts.admin_panel_invalid, show_alert=True)
        return
    if "rs_sel_duration_days" in data and data.get("rs_sel_duration_days") == selected:
        await callback.answer()
        return
    await state.update_data(rs_sel_duration_days=selected)
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await _show_reseller_matrix(message, session, state, int(user_id))


@router.callback_query(F.data.startswith(AdminCB.RS_TIER))
async def admin_reseller_matrix_tier(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(AdminCB.RS_TIER) :]
    data = await state.get_data()
    user_id = data.get("rs_user_id")
    if not user_id or raw not in {"economy", "premium"}:
        await callback.answer(texts.admin_panel_invalid, show_alert=True)
        return
    if data.get("rs_sel_tier") == raw:
        await callback.answer()
        return
    await state.update_data(rs_sel_tier=raw)
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await _show_reseller_matrix(message, session, state, int(user_id))


@router.callback_query(F.data.startswith(AdminCB.RS_CELL))
async def admin_reseller_matrix_cell(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    slug = (callback.data or "")[len(AdminCB.RS_CELL) :]
    data = await state.get_data()
    user_id = data.get("rs_user_id")
    sel_duration = data.get("rs_sel_duration_days")
    sel_tier = str(data.get("rs_sel_tier") or DEFAULT_TIER)
    if not user_id or not slug:
        await callback.answer(texts.admin_panel_invalid, show_alert=True)
        return
    view = await get_catalog_view(session)
    plan = next((p for p in view.packages if p.slug == slug), None)
    if plan is None:
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    cat = view.category(int(plan.category_id))
    if flat_plan_list_enabled(cat):
        sel_duration = duration_from_package_options(dict(plan.options or {}))
        sel_tier = DEFAULT_TIER
    retail = get_price(
        list(view.prices or []),
        slug=str(plan.slug),
        duration_days=sel_duration,
        tier=sel_tier,
    )
    if retail is None and flat_plan_list_enabled(cat):
        fixed = getattr(plan, "fixed_price_toman", None)
        if fixed is not None:
            try:
                retail = int(fixed)
            except (TypeError, ValueError):
                retail = None
    await state.set_state(AdminStates.waiting_reseller_number)
    await state.update_data(
        rs_user_id=int(user_id),
        rs_field="plan",
        rs_slug=slug,
        rs_sel_duration_days=sel_duration,
        rs_sel_tier=sel_tier,
    )
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _edit_or_answer_admin(
        message,
        texts.admin_reseller_prompt_plan.format(
            title=escape(plan.title_fa),
            retail=_fmt_toman(int(retail or 0)),
        ),
        admin_cancel_input_keyboard(),
    )


@router.callback_query(F.data == AdminCB.RS_PCT)
async def admin_reseller_pct_ask(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    data = await state.get_data()
    if not data.get("rs_user_id"):
        await callback.answer(texts.admin_panel_invalid, show_alert=True)
        return
    await state.set_state(AdminStates.waiting_reseller_number)
    await state.update_data(rs_field="pct")
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        prompt = (
            texts.admin_reseller_pct_prompt_flat
            if data.get("rs_flat")
            else texts.admin_reseller_pct_prompt
        )
        await _edit_or_answer_admin(message, prompt, admin_cancel_input_keyboard())


@router.callback_query(F.data == AdminCB.RS_PCT_OK)
async def admin_reseller_pct_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    data = await state.get_data()
    user_id = data.get("rs_user_id")
    changes_raw = data.get("rs_pct_changes")
    pct = data.get("rs_pct_value")
    sel_duration = data.get("rs_sel_duration_days")
    sel_tier = str(data.get("rs_sel_tier") or DEFAULT_TIER)
    if not user_id or not changes_raw or pct is None:
        await callback.answer(texts.admin_reseller_pct_expired, show_alert=True)
        return
    flat = bool(data.get("rs_pct_flat"))
    cuts: list[Cut] = []
    for item in changes_raw:
        if isinstance(item, dict):
            cuts.append(
                Cut(
                    item.get("duration"),
                    str(item["slug"]),
                    int(item["before"]),
                    int(item["after"]),
                )
            )
        else:
            slug, before, after = item
            cuts.append(Cut(sel_duration, str(slug), int(before), int(after)))
    count = await apply_cuts(
        session, int(user_id), flat=flat, tier=sel_tier, pct=int(pct), cuts=cuts
    )
    await session.commit()
    await state.update_data(rs_pct_changes=None, rs_pct_value=None, rs_field=None, rs_pct_flat=None)
    await callback.answer(texts.admin_reseller_pct_done.format(count=to_persian_digits(count)))
    message = _tg_message(callback)
    if message is not None:
        await _show_reseller_matrix(message, session, state, int(user_id))


@router.callback_query(F.data == AdminCB.RS_PCT_NO)
async def admin_reseller_pct_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    data = await state.get_data()
    user_id = data.get("rs_user_id")
    await state.update_data(rs_pct_changes=None, rs_pct_value=None, rs_field=None, rs_pct_flat=None)
    await callback.answer()
    message = _tg_message(callback)
    if message is not None and user_id:
        await _show_reseller_matrix(message, session, state, int(user_id))


@router.callback_query(F.data == AdminCB.RS_UNDO)
async def admin_reseller_undo(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    data = await state.get_data()
    user_id = data.get("rs_user_id")
    sel_duration = data.get("rs_sel_duration_days")
    sel_tier = str(data.get("rs_sel_tier") or DEFAULT_TIER)
    if not user_id:
        await callback.answer(texts.admin_panel_invalid, show_alert=True)
        return
    flat, rows = await _reseller_screen_plans(session, data)
    restored = await undo_screen_cut(
        session, int(user_id), flat=flat, rows=rows, duration=sel_duration, tier=sel_tier
    )
    if restored is None:
        await callback.answer(texts.admin_reseller_nothing_to_undo, show_alert=True)
        return
    await session.commit()
    await callback.answer(texts.admin_reseller_undo_done.format(count=to_persian_digits(restored)))
    message = _tg_message(callback)
    if message is not None:
        await _show_reseller_matrix(message, session, state, int(user_id))


@router.callback_query(F.data.startswith(AdminCB.RS_PLAN_CLEAR))
async def admin_reseller_plan_clear(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(AdminCB.RS_PLAN_CLEAR) :]
    user_raw, _, slug = raw.partition(":")
    data = await state.get_data()
    sel_duration = data.get("rs_sel_duration_days")
    sel_tier = str(data.get("rs_sel_tier") or DEFAULT_TIER)
    if not user_raw.isdigit() or not slug:
        await callback.answer(texts.admin_panel_invalid, show_alert=True)
        return
    view = await get_catalog_view(session)
    plan = next((p for p in view.packages if p.slug == slug), None)
    if plan is not None:
        cat = view.category(int(plan.category_id))
        if flat_plan_list_enabled(cat):
            sel_duration = duration_from_package_options(dict(plan.options or {}))
            sel_tier = DEFAULT_TIER
    await clear_plan_price(session, int(user_raw), slug, duration_days=sel_duration, tier=sel_tier)
    await session.commit()
    await callback.answer(texts.admin_reseller_price_cleared)
    message = _tg_message(callback)
    if message is not None:
        await state.update_data(rs_user_id=int(user_raw))
        await _show_reseller_matrix(message, session, state, int(user_raw))


@router.callback_query(F.data.startswith(AdminCB.RS_PLAN))
async def admin_reseller_plan_ask(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """Legacy RS_PLAN callbacks — redirect into matrix cell flow when possible."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if (callback.data or "").startswith(AdminCB.RS_PLAN_CLEAR):
        return
    raw = (callback.data or "")[len(AdminCB.RS_PLAN) :]
    user_raw, _, slug = raw.partition(":")
    if not user_raw.isdigit() or not slug:
        await callback.answer(texts.admin_panel_invalid, show_alert=True)
        return
    view = await get_catalog_view(session)
    plan = next((p for p in view.packages if p.slug == slug), None)
    duration = duration_from_package_options(dict(plan.options or {})) if plan is not None else 30
    data = await state.get_data()
    await state.update_data(
        rs_user_id=int(user_raw),
        rs_sel_duration_days=data.get("rs_sel_duration_days", duration),
        rs_sel_tier=str(data.get("rs_sel_tier") or DEFAULT_TIER),
    )
    callback.data = f"{AdminCB.RS_CELL}{slug}"
    await admin_reseller_matrix_cell(callback, state, session, settings, db_user)


@router.message(AdminStates.waiting_reseller_number)
async def admin_reseller_number_save(  # noqa: PLR0911
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None, settings, db_user):
        await state.clear()
        return
    data = await state.get_data()
    user_id = data.get("rs_user_id")
    field = data.get("rs_field")
    if not user_id or not field:
        await state.clear()
        await message.answer(texts.admin_panel_invalid, reply_markup=admin_back_home_keyboard())
        return

    raw = (message.text or "").strip()

    if field == "pct":
        pct = parse_percent_input(raw)
        if pct is None:
            await message.answer(
                texts.admin_reseller_pct_invalid,
                reply_markup=admin_cancel_input_keyboard(),
            )
            return
        sel_duration = data.get("rs_sel_duration_days")
        sel_tier = str(data.get("rs_sel_tier") or DEFAULT_TIER)
        view = await get_catalog_view(session)
        flat, _rows = await _reseller_screen_plans(session, data)
        cuts = await preview_cut(
            session,
            int(user_id),
            view,
            category_id=data.get("rs_category_id"),
            duration=sel_duration,
            tier=sel_tier,
            pct=pct,
        )
        if not cuts:
            await message.answer(
                texts.admin_reseller_pct_nothing,
                reply_markup=admin_cancel_input_keyboard(),
            )
            return
        # The two shapes the confirm tap reads back: a flat list spans several durations.
        stored: list[Any] = (
            [
                {"duration": c.duration_days, "slug": c.slug, "before": c.before, "after": c.after}
                for c in cuts
            ]
            if flat
            else [(c.slug, c.before, c.after) for c in cuts]
        )
        await state.update_data(
            rs_pct_value=pct,
            rs_pct_changes=stored,
            rs_pct_flat=flat,
            rs_field=None,
        )
        await state.set_state(None)
        await message.answer(
            cut_preview_text(cuts, view), reply_markup=reseller_pct_preview_keyboard()
        )
        return

    # Zero is meaningful for a rate («این جزء رو حساب نکن») but not for a plan
    # price, where it would mean giving the service away.
    value = parse_reseller_input(str(field), raw)
    if value is None:
        await message.answer(
            texts.admin_store_number_invalid.format(example="1200"),
            reply_markup=admin_cancel_input_keyboard(),
        )
        return

    try:
        if field == "plan":
            slug = str(data.get("rs_slug") or "")
            sel_duration = data.get("rs_sel_duration_days")
            sel_tier = str(data.get("rs_sel_tier") or DEFAULT_TIER)
            await set_plan_price(
                session,
                int(user_id),
                slug,
                value,
                duration_days=sel_duration,
                tier=sel_tier,
            )
        else:
            attr, _ = _RATE_FIELD_PROMPTS[str(field)]
            await set_rate(session, int(user_id), **{attr: value})
        await session.commit()
    except ResellerError as exc:
        await session.rollback()
        await message.answer(str(exc), reply_markup=admin_cancel_input_keyboard())
        return
    # Keep matrix selection; clear only the input field marker
    await state.update_data(rs_field=None, rs_slug=None)
    await state.set_state(None)
    if field == "plan":
        await _show_reseller_matrix(message, session, state, int(user_id))
        return
    rate = await get_rate(session, int(user_id))
    if field in {"gb", "day"}:
        await message.answer(
            texts.admin_reseller_custom_intro,
            reply_markup=reseller_custom_rate_keyboard(int(user_id), rate=rate),
        )
        return
    await message.answer(
        texts.admin_reseller_trial_intro,
        reply_markup=reseller_trial_settings_keyboard(int(user_id), rate=rate),
    )


# Kept under its old name: the report-group setup parses a chat id with it too.
_parse_signed_toman = parse_signed_toman


@router.callback_query(F.data.startswith(AdminCB.LATE_IGNORE))
async def admin_late_receipt_ignore(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
) -> None:
    """«❌ نادیده»: edits this admin's own message, touches nothing else."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await callback.answer(texts.admin_late_receipt_ignored)
    message = _tg_message(callback)
    if message is not None:
        with contextlib.suppress(Exception):
            await message.edit_reply_markup(reply_markup=admin_back_home_keyboard())


# --------------------------------------------------------------------------- #
# Live service operations
#
# The PasarGuard client has had modify_user since phase 1 but nothing called
# it, so giving a customer a few days after an outage meant opening the panel.
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def _panel(session: AsyncSession, settings: Settings) -> AsyncIterator[PasarGuardClient]:
    """Borrow a panel client and always close it.

    ``get_pasarguard_client`` is a coroutine returning a client the caller must
    ``aclose()`` — wrapping it once keeps every operation from leaking one.
    """
    client = await get_pasarguard_client(session, settings)
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()


def _fmt_bytes(value: int | None) -> str:
    if value is None:
        return texts.admin_service_data_unlimited
    gb = value / (1024**3)
    return f"{to_persian_digits(f'{gb:.1f}'.rstrip('0').rstrip('.'))} گیگ"


def _fmt_expire(snapshot: ServiceSnapshot) -> str:
    """Jalali date, and how long is left: «۱۴۰۵/۰۷/۱۳ (۱۲ روز مانده)»."""
    moment = snapshot.expire_at
    if moment is None:
        return texts.admin_service_expire_none
    stamp = format_jalali(moment, persian_digits=True).split(" ")[0]
    if moment < datetime.now(UTC):
        return texts.admin_service_expired.format(when=stamp)
    return f"{stamp} ({time_left_fa(moment)} مانده)"


def _extras_lines(snapshot: ServiceSnapshot, extras: SupportExtras | None) -> str:
    """Support lines under the card. A line whose read failed is left out, never «—»."""
    lines: list[str] = []
    if snapshot.online_at is not None:
        lines.append(
            texts.admin_service_line_last_online.format(
                when=format_relative_age(snapshot.online_at, persian_digits=True)
            )
        )
    if extras is not None and extras.device_count is not None:
        limit = int(snapshot.hwid_limit or 0)
        count = to_persian_digits(extras.device_count)
        value = (
            f"{count} از {to_persian_digits(limit)}"
            if limit > 0
            else f"{count} ({texts.admin_service_devices_unlimited})"
        )
        lines.append(texts.admin_service_line_devices.format(value=value))
    if extras is not None and extras.last_update_app:
        lines.append(
            texts.admin_service_line_last_update.format(
                app=escape(extras.last_update_app),
                when=format_relative_age(extras.last_update_at, persian_digits=True),
            )
        )
    return "".join(f"\n{line}" for line in lines)


def _service_text(
    snapshot: ServiceSnapshot,
    *,
    name: str,
    unused: UnusedValue | None = None,
    extras: SupportExtras | None = None,
) -> str:
    if snapshot.is_unlimited_data:
        data = texts.admin_service_data_unlimited
    else:
        data = texts.admin_service_data_metered.format(
            used=_fmt_bytes(snapshot.used_traffic_bytes),
            limit=_fmt_bytes(snapshot.data_limit_bytes),
            remaining=_fmt_bytes(snapshot.remaining_bytes),
        )
    disabled = (snapshot.status or "").lower() == "disabled"
    return texts.admin_service_card.format(
        name=escape(name),
        panel_username=escape(snapshot.panel_username),
        status=(
            texts.admin_service_status_disabled if disabled else texts.admin_service_status_active
        ),
        expire=_fmt_expire(snapshot),
        data=data,
        extras=_extras_lines(snapshot, extras),
    ) + _unused_line(unused)


def _unused_line(unused: UnusedValue | None) -> str:
    """The number to credit if this service is compensated, with its formula."""
    if unused is None:
        return ""
    return "\n\n" + texts.admin_service_unused_value.format(
        amount=_fmt_toman(unused.amount_toman),
        paid=_fmt_toman(unused.paid_toman),
        period=to_persian_digits(unused.period_days),
        days=to_persian_digits(unused.days_left),
    )


async def _service_context(session: AsyncSession, account_id: int) -> tuple[PasarGuardAccount, str]:
    account = await session.get(PasarGuardAccount, account_id)
    if account is None:
        raise ServiceOpError("سرویس یافت نشد.")
    name = str(account.display_name or account.service_name or account.panel_username)
    return account, name


async def _render_service(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    account_id: int,
    *,
    extra_rows: list[list[InlineKeyboardButton]] | None = None,
) -> None:
    account, name = await _service_context(session, account_id)
    async with _panel(session, settings) as client:
        snapshot = await get_snapshot(session, client, account_id=account_id)
    disabled = (snapshot.status or "").lower() == "disabled"
    unused = await unused_value_for_account(session, account, expire_unix=snapshot.expire_unix)
    try:
        async with _panel(session, settings) as client:
            extras: SupportExtras | None = await get_support_extras(client, snapshot)
    except Exception:  # noqa: BLE001 - decoration only; the card must still open
        extras = None
    exempt_line = "\n\n" + texts.admin_life_exempt_line if account.delete_exempt else ""
    await _edit_or_answer_admin(
        message,
        _service_text(snapshot, name=name, unused=unused, extras=extras) + exempt_line,
        service_ops_keyboard(
            account_id,
            int(account.user_id),
            disabled=disabled,
            has_expiry=snapshot.expire_at is not None,
            extra_rows=extra_rows,
        ),
    )


@router.callback_query(F.data.startswith(AdminCB.SVC_TOGGLE_OK))
async def admin_service_toggle_apply(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    bot: Bot,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(AdminCB.SVC_TOGGLE_OK) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    try:
        account, name = await _service_context(session, int(raw))
        async with _panel(session, settings) as client:
            current = await get_snapshot(session, client, account_id=int(raw))
            enable = (current.status or "").lower() == "disabled"
            await set_enabled(session, client, account_id=int(raw), enabled=enable)
    except ServiceOpError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(texts.admin_service_enabled if enable else texts.admin_service_disabled)
    _ = (account, name, bot)
    message = _tg_message(callback)
    if message is not None:
        await _render_service(message, session, settings, int(raw))


@router.callback_query(F.data.startswith(AdminCB.SVC_TOGGLE))
async def admin_service_toggle_confirm(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(AdminCB.SVC_TOGGLE) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    try:
        _account, name = await _service_context(session, int(raw))
        async with _panel(session, settings) as client:
            snapshot = await get_snapshot(session, client, account_id=int(raw))
    except ServiceOpError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    disabled = (snapshot.status or "").lower() == "disabled"
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    body = (
        texts.admin_service_toggle_confirm_on.format(name=escape(name))
        if disabled
        else texts.admin_service_toggle_confirm_off.format(name=escape(name))
    )
    await _edit_or_answer_admin(
        message, body, service_toggle_confirm_keyboard(int(raw), disable=not disabled)
    )


@router.callback_query(F.data.startswith(AdminCB.SVC_EXTEND))
async def admin_service_extend_start(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(AdminCB.SVC_EXTEND) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    try:
        _account, name = await _service_context(session, int(raw))
        async with _panel(session, settings) as client:
            snapshot = await get_snapshot(session, client, account_id=int(raw))
    except ServiceOpError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await state.clear()
    await state.update_data(
        service_account_id=int(raw),
        service_operation_key=uuid.uuid4().hex,
    )
    await state.set_state(AdminStates.waiting_service_days)
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _edit_or_answer_admin(
        message,
        texts.admin_service_prompt_extend.format(name=escape(name), expire=_fmt_expire(snapshot)),
        admin_cancel_input_keyboard(),
    )


@router.callback_query(F.data.startswith(AdminCB.SVC_DATA))
async def admin_service_data_start(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(AdminCB.SVC_DATA) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    try:
        _account, name = await _service_context(session, int(raw))
        async with _panel(session, settings) as client:
            snapshot = await get_snapshot(session, client, account_id=int(raw))
    except ServiceOpError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    if snapshot.is_unlimited_data:
        await callback.answer("این سرویس نامحدود است؛ افزودن حجم معنا ندارد.", show_alert=True)
        return
    await state.clear()
    await state.update_data(
        service_account_id=int(raw),
        service_operation_key=uuid.uuid4().hex,
    )
    await state.set_state(AdminStates.waiting_service_gb)
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _edit_or_answer_admin(
        message,
        texts.admin_service_prompt_data.format(
            name=escape(name), data=_fmt_bytes(snapshot.data_limit_bytes)
        ),
        admin_cancel_input_keyboard(),
    )


# --------------------------------------------------------------------------- #
# Support tools on the service card: devices, link, QR
# --------------------------------------------------------------------------- #


def _device_row(index: int, device: Any) -> str:
    parts = [
        str(device.device_model or "").strip() or "دستگاه ناشناس",
        " ".join(p for p in (device.device_os, device.os_version) if p).strip(),
    ]
    if device.created_at is not None:
        parts.append(format_jalali(device.created_at, persian_digits=True).split(" ")[0])
    return f"{to_persian_digits(index)}. " + " • ".join(escape(p) for p in parts if p)


async def _render_devices(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    account_id: int,
    *,
    toast: str = "",
) -> None:
    _account_row, name = await _service_context(session, account_id)
    async with _panel(session, settings) as client:
        devices = await list_devices(session, client, account_id=account_id)
        snapshot = await get_snapshot(session, client, account_id=account_id)
    limit = int(snapshot.hwid_limit or 0)
    body = texts.admin_service_devices_title.format(
        name=escape(name),
        count=to_persian_digits(len(devices)),
        limit=to_persian_digits(limit) if limit > 0 else texts.admin_service_devices_unlimited,
        rows=(
            "\n".join(_device_row(i, d) for i, d in enumerate(devices, 1))
            if devices
            else texts.admin_service_devices_empty
        ),
    )
    if toast:
        body = f"{toast}\n\n{body}"
    await _edit_or_answer_admin(
        message, body, service_devices_keyboard(account_id, count=len(devices))
    )


def _svc_id(callback: CallbackQuery, prefix: str) -> int | None:
    raw = (callback.data or "")[len(prefix) :]
    return int(raw) if raw.isdigit() else None


@router.callback_query(F.data.startswith(AdminCB.SVC_DEV_RESET_OK))
async def admin_service_devices_reset_apply(
    callback: CallbackQuery, session: AsyncSession, settings: Settings, db_user: User | None
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    account_id = _svc_id(callback, AdminCB.SVC_DEV_RESET_OK)
    if account_id is None:
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    try:
        async with _panel(session, settings) as client:
            await reset_devices(session, client, account_id=account_id)
    except ServiceOpError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await _render_devices(
            message, session, settings, account_id, toast=texts.admin_service_devices_reset_done
        )


@router.callback_query(F.data.startswith(AdminCB.SVC_DEV_RESET))
async def admin_service_devices_reset_confirm(
    callback: CallbackQuery, session: AsyncSession, settings: Settings, db_user: User | None
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    account_id = _svc_id(callback, AdminCB.SVC_DEV_RESET)
    if account_id is None:
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    try:
        _account_row, name = await _service_context(session, account_id)
    except ServiceOpError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await _edit_or_answer_admin(
            message,
            texts.admin_service_devices_reset_confirm.format(name=escape(name)),
            service_devices_reset_confirm_keyboard(account_id),
        )


@router.callback_query(F.data.startswith(AdminCB.SVC_DEVICES))
async def admin_service_devices(
    callback: CallbackQuery, session: AsyncSession, settings: Settings, db_user: User | None
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    account_id = _svc_id(callback, AdminCB.SVC_DEVICES)
    if account_id is None:
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    message = _tg_message(callback)
    try:
        if message is None:
            await callback.answer()
            return
        await _render_devices(message, session, settings, account_id)
    except ServiceOpError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()


async def _render_link_screen(
    message: Message, session: AsyncSession, account_id: int, *, toast: str = ""
) -> None:
    account, name = await _service_context(session, account_id)
    url = str(account.subscription_url or "").strip()
    body = (
        texts.admin_service_link_screen.format(name=escape(name), url=escape(url))
        if url
        else texts.admin_service_link_none
    )
    if toast:
        body = f"{toast}\n\n{body}"
    await _edit_or_answer_admin(
        message, body, service_link_keyboard(account_id, with_link=bool(url))
    )


@router.callback_query(F.data.startswith(AdminCB.SVC_LINK_NEW_OK))
async def admin_service_link_revoke_apply(
    callback: CallbackQuery, session: AsyncSession, settings: Settings, db_user: User | None
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    account_id = _svc_id(callback, AdminCB.SVC_LINK_NEW_OK)
    if account_id is None:
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    async with _panel(session, settings) as client:
        new_url = await revoke_link(session, client, account_id=account_id)
    if not new_url:
        await callback.answer(texts.admin_service_link_revoke_failed, show_alert=True)
        return
    await session.commit()
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await _render_link_screen(
            message, session, account_id, toast=texts.admin_service_link_revoked
        )


@router.callback_query(F.data.startswith(AdminCB.SVC_LINK_NEW))
async def admin_service_link_revoke_confirm(
    callback: CallbackQuery, session: AsyncSession, settings: Settings, db_user: User | None
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    account_id = _svc_id(callback, AdminCB.SVC_LINK_NEW)
    if account_id is None:
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    try:
        _account_row, name = await _service_context(session, account_id)
    except ServiceOpError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await _edit_or_answer_admin(
            message,
            texts.admin_service_link_revoke_confirm.format(name=escape(name)),
            service_link_revoke_confirm_keyboard(account_id),
        )


@router.callback_query(F.data.startswith(AdminCB.SVC_LINK_SEND))
async def admin_service_link_send(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    bot: Bot,
) -> None:
    """The customer's own card goes to the customer's chat, never the admin's."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    account_id = _svc_id(callback, AdminCB.SVC_LINK_SEND)
    account = await session.get(PasarGuardAccount, account_id) if account_id else None
    owner = await session.get(User, int(account.user_id)) if account is not None else None
    if account is None or owner is None:
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    try:
        sent = await deliver_provisioned_service_card(
            session,
            settings,
            user_id=int(owner.id),
            account_id=int(account.id),
            intro=texts.notify_service_link_resent,
            bot=bot,
            chat_id=int(owner.telegram_id),
        )
    except Exception:  # noqa: BLE001 - a blocked customer is an answer, not a crash
        logger.exception("resend link failed for account %s", account_id)
        sent = False
    await callback.answer(
        texts.admin_service_link_sent if sent else texts.admin_service_link_send_failed,
        show_alert=not sent,
    )


@router.callback_query(F.data.startswith(AdminCB.SVC_LINK_QR))
async def admin_service_link_qr(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    bot: Bot,
) -> None:
    """The QR goes to the admin with the link as caption and a way back."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    account_id = _svc_id(callback, AdminCB.SVC_LINK_QR)
    account = await session.get(PasarGuardAccount, account_id) if account_id else None
    url = str(account.subscription_url or "").strip() if account is not None else ""
    if account is None or not url or callback.from_user is None:
        await callback.answer(texts.admin_service_qr_failed, show_alert=True)
        return
    try:
        png = await service_qr_png(session, settings, url=url, bot=bot)
        await bot.send_photo(
            chat_id=callback.from_user.id,
            photo=BufferedInputFile(png, filename="service-qr.png"),
            caption=f"<code>{escape(url)}</code>",
            reply_markup=service_link_back_keyboard(int(account.id)),
        )
    except Exception:  # noqa: BLE001
        logger.exception("admin QR failed for account %s", account_id)
        await callback.answer(texts.admin_service_qr_failed, show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith(AdminCB.SVC_LINK))
async def admin_service_link(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    account_id = _svc_id(callback, AdminCB.SVC_LINK)
    if account_id is None:
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    message = _tg_message(callback)
    try:
        if message is not None:
            await _render_link_screen(message, session, account_id)
    except ServiceOpError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith(AdminCB.SVC))
async def admin_service_card(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(AdminCB.SVC) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    try:
        await _render_service(message, session, settings, int(raw))
    except ServiceOpError as exc:
        await _edit_or_answer_admin(message, str(exc), admin_back_home_keyboard())


async def _apply_service_number(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    *,
    kind: str,
) -> None:
    """«تمدید» (applies) and «افزودن حجم» (asks first): one number typed by the admin."""
    data = await state.get_data()
    account_id = data.get("service_account_id")
    raw_key = data.get("service_operation_key")
    operation_key = str(raw_key) if raw_key else None
    if not account_id:
        await state.clear()
        await message.answer(texts.admin_action_error, reply_markup=admin_back_home_keyboard())
        return
    amount = _parse_signed_toman(message.text or "")
    if amount is None or amount <= 0:
        await message.answer(
            texts.admin_store_number_invalid.format(example="7" if kind == "days" else "20"),
            reply_markup=admin_cancel_input_keyboard(),
        )
        return
    if kind == "gb":
        await _ask_to_add_data(message, state, session, settings, int(account_id), amount)
        return
    await _run_service_op(
        message,
        state,
        session,
        settings,
        bot,
        kind=kind,
        account_id=int(account_id),
        amount=amount,
        operation_key=operation_key,
    )


async def _run_service_op(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    *,
    kind: str,
    account_id: int,
    amount: int,
    operation_key: str | None,
) -> bool:
    """Apply «تمدید» or «افزودن حجم» through the one service function. False if refused."""
    try:
        account, name = await _service_context(session, int(account_id))
        async with _panel(session, settings) as client:
            if kind == "days":
                snapshot = await extend_days(
                    session,
                    client,
                    account_id=int(account_id),
                    days=amount,
                    operation_key=operation_key,
                )
            else:
                snapshot = await add_data(
                    session,
                    client,
                    account_id=int(account_id),
                    gigabytes=amount,
                    operation_key=operation_key,
                )
    except ServiceOpError as exc:
        await message.answer(str(exc), reply_markup=admin_cancel_input_keyboard())
        return False
    await state.clear()

    owner = await session.get(User, int(account.user_id))
    if owner is not None and not snapshot.replayed:
        notice = (
            texts.notify_service_extended.format(name=escape(name), expire=_fmt_expire(snapshot))
            if kind == "days"
            else texts.notify_service_data.format(
                name=escape(name), amount=f"{to_persian_digits(amount)} گیگ"
            )
        )
        with contextlib.suppress(Exception):
            await bot.send_message(chat_id=owner.telegram_id, text=notice)

    done = (
        texts.admin_service_extended.format(expire=_fmt_expire(snapshot))
        if kind == "days"
        else texts.admin_service_data_added.format(limit=_fmt_bytes(snapshot.data_limit_bytes))
    )
    await message.answer(
        f"{done}\n\n{_service_text(snapshot, name=name)}",
        reply_markup=service_ops_keyboard(
            int(account_id),
            int(account.user_id),
            disabled=(snapshot.status or "").lower() == "disabled",
            has_expiry=snapshot.expire_at is not None,
        ),
    )
    return True


async def _ask_to_add_data(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    account_id: int,
    gigabytes: int,
) -> None:
    """The typed gigabytes become a question: nothing is written until the confirm tap."""
    if gigabytes > MAX_MANUAL_GB:
        await message.answer(
            texts.admin_store_number_invalid.format(example="20"),
            reply_markup=admin_cancel_input_keyboard(),
        )
        return
    try:
        _account, name = await _service_context(session, account_id)
        async with _panel(session, settings) as client:
            snapshot = await get_snapshot(session, client, account_id=account_id)
    except ServiceOpError as exc:
        await message.answer(str(exc), reply_markup=admin_cancel_input_keyboard())
        return
    if snapshot.is_unlimited_data:
        await message.answer(
            "این سرویس نامحدود است؛ افزودن حجم معنا ندارد.",
            reply_markup=admin_cancel_input_keyboard(),
        )
        return
    # The amount waits in the FSM state; the confirm tap takes it out before writing.
    await state.update_data(pending_gb=gigabytes)
    after = int(snapshot.data_limit_bytes or 0) + gigabytes * 1024**3
    await message.answer(
        texts.admin_service_data_confirm.format(
            name=escape(name),
            amount=to_persian_digits(gigabytes),
            before=_fmt_bytes(snapshot.data_limit_bytes),
            after=_fmt_bytes(after),
        ),
        reply_markup=service_data_confirm_keyboard(account_id),
    )


@router.callback_query(F.data.startswith(AdminCB.SVC_DATA_OK))
async def admin_service_data_confirmed(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    bot: Bot,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(AdminCB.SVC_DATA_OK) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    data = await state.get_data()
    pending = data.get("pending_gb")
    message = _tg_message(callback)
    if not pending or message is None or data.get("service_account_id") != int(raw):
        await callback.answer(texts.admin_service_data_nothing_pending, show_alert=True)
        return
    # Take it out before the panel call: the next tap on this button finds nothing.
    await state.update_data(pending_gb=None)
    await callback.answer()
    raw_key = data.get("service_operation_key")
    done = await _run_service_op(
        message,
        state,
        session,
        settings,
        bot,
        kind="gb",
        account_id=int(raw),
        amount=int(pending),
        operation_key=str(raw_key) if raw_key else None,
    )
    if not done:
        await state.update_data(pending_gb=int(pending))


@router.message(AdminStates.waiting_service_days)
async def admin_service_extend_apply(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    bot: Bot,
) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None, settings, db_user):
        await state.clear()
        await message.answer(texts.admin_action_denied, reply_markup=admin_back_home_keyboard())
        return
    await _apply_service_number(message, state, session, settings, bot, kind="days")


@router.message(AdminStates.waiting_service_gb)
async def admin_service_data_apply(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    bot: Bot,
) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None, settings, db_user):
        await state.clear()
        await message.answer(texts.admin_action_denied, reply_markup=admin_back_home_keyboard())
        return
    await _apply_service_number(message, state, session, settings, bot, kind="gb")


# --------------------------------------------------------------------------- #
# Report group
#
# Setup is evidence-based: a route counts as ready only after a real message
# reached it. A wrong thread id, a bot without permission and a group with
# forum mode off all look identical in the database.
# --------------------------------------------------------------------------- #


def _setup_report(result: SetupResult) -> str:
    """Say exactly which topics worked, so a partial setup cannot read as success."""
    lines: list[str] = []
    verified = set(result.verified)
    for key in result.created:
        if key in verified:
            lines.append(texts.admin_reports_line_created.format(title=TOPIC_TITLES_FA[key]))
    for key in result.reused:
        if key in verified:
            lines.append(texts.admin_reports_line_reused.format(title=TOPIC_TITLES_FA[key]))
    for key, reason in result.failures:
        lines.append(
            texts.admin_reports_line_failed.format(
                title=TOPIC_TITLES_FA.get(key, key), reason=escape(reason)
            )
        )
    title = escape(result.readiness.chat_title or str(result.readiness.chat_id))
    template = texts.admin_reports_setup_done if result.ok else texts.admin_reports_setup_partial
    text = template.format(title=title, lines="\n".join(lines) or "—")
    if result.requeued:
        text += "\n\n" + texts.admin_reports_requeued.format(
            count=to_persian_digits(result.requeued)
        )
    return text


def _reports_text(readiness: GroupReadiness) -> str:
    if not readiness.attached:
        status = texts.admin_reports_not_attached
    else:
        status = texts.admin_reports_attached.format(
            title=escape(readiness.chat_title or str(readiness.chat_id)),
            state=(
                texts.admin_reports_state_on if readiness.enabled else texts.admin_reports_state_off
            ),
            ready=to_persian_digits(readiness.ready_count),
            total=to_persian_digits(len(readiness.topics)),
        )
    return texts.admin_reports_title.format(status=status)


async def _render_reports(message: Message, session: AsyncSession) -> None:
    readiness = await get_readiness(session)
    await session.commit()
    await _edit_or_answer_admin(message, _reports_text(readiness), report_group_keyboard(readiness))


@router.callback_query(F.data == AdminCB.REPORTS)
async def admin_reports_home(
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
        await _render_reports(message, session)


@router.callback_query(F.data == AdminCB.REP_ATTACH)
async def admin_reports_attach_start(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_report_chat_id)
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await _edit_or_answer_admin(
            message, texts.admin_reports_attach_help, admin_cancel_input_keyboard()
        )


@router.message(AdminStates.waiting_report_chat_id)
async def admin_reports_attach_apply(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    bot: Bot,
) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None, settings, db_user):
        await state.clear()
        await message.answer(texts.admin_action_denied, reply_markup=admin_back_home_keyboard())
        return
    chat_id = _parse_signed_toman(message.text or "")
    if chat_id is None:
        await message.answer(
            texts.admin_store_number_invalid.format(example="-1001234567890"),
            reply_markup=admin_cancel_input_keyboard(),
        )
        return
    try:
        # One message does the whole job: attach, create every topic, prove
        # each one, switch reporting on. Copying five thread ids out of
        # «Copy Link» by hand was five chances to paste the wrong one.
        result = await run_full_setup(session, bot, chat_id=int(chat_id))
        await session.commit()
    except ReportGroupError as exc:
        await session.rollback()
        await message.answer(str(exc), reply_markup=admin_cancel_input_keyboard())
        return
    await state.clear()
    await message.answer(
        _setup_report(result),
        reply_markup=report_group_keyboard(result.readiness),
    )


@router.callback_query(F.data.startswith(AdminCB.REP_TOPIC))
async def admin_reports_topic_start(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    key = (callback.data or "")[len(AdminCB.REP_TOPIC) :]
    if key not in TOPIC_KEYS:
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    await state.clear()
    await state.update_data(report_topic_key=key)
    await state.set_state(AdminStates.waiting_report_topic_id)
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await _edit_or_answer_admin(
            message,
            texts.admin_reports_topic_prompt.format(title=TOPIC_TITLES_FA[key]),
            admin_cancel_input_keyboard(),
        )


@router.message(AdminStates.waiting_report_topic_id)
async def admin_reports_topic_apply(
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
    key = str(data.get("report_topic_key") or "")
    thread_id = _parse_signed_toman(message.text or "")
    if key not in TOPIC_KEYS or thread_id is None or thread_id <= 0:
        await message.answer(
            texts.admin_store_number_invalid.format(example="12"),
            reply_markup=admin_cancel_input_keyboard(),
        )
        return
    try:
        readiness = await set_topic(session, topic_key=key, thread_id=int(thread_id))
        await session.commit()
    except ReportGroupError as exc:
        await session.rollback()
        await message.answer(str(exc), reply_markup=admin_back_home_keyboard())
        await state.clear()
        return
    await state.clear()
    await message.answer(
        texts.admin_reports_topic_set.format(title=TOPIC_TITLES_FA[key]),
        reply_markup=report_group_keyboard(readiness),
    )


@router.callback_query(F.data.startswith(AdminCB.REP_VERIFY))
async def admin_reports_verify(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    bot: Bot,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    key = (callback.data or "")[len(AdminCB.REP_VERIFY) :]
    if key not in TOPIC_KEYS:
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    try:
        readiness = await verify_topic(session, bot, topic_key=key)
        await session.commit()
    except ReportGroupError as exc:
        await session.commit()  # a failed test still records the loss of proof
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(texts.admin_reports_verified.format(title=TOPIC_TITLES_FA[key]))
    message = _tg_message(callback)
    if message is not None:
        await _edit_or_answer_admin(
            message, _reports_text(readiness), report_group_keyboard(readiness)
        )


@router.callback_query(F.data == AdminCB.REP_TOGGLE)
async def admin_reports_toggle(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    current = await get_readiness(session)
    try:
        readiness = await set_reporting_enabled(session, enabled=not current.enabled)
        await session.commit()
    except ReportGroupError as exc:
        await session.rollback()
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(
        texts.admin_reports_enabled if readiness.enabled else texts.admin_reports_disabled
    )
    message = _tg_message(callback)
    if message is not None:
        await _edit_or_answer_admin(
            message, _reports_text(readiness), report_group_keyboard(readiness)
        )


# --------------------------------------------------------------------------- #
# Broadcast
#
# The audience count is on every screen because «همهٔ مشتری‌ها» means nothing
# until you know whether that is nine people or nine thousand.
# --------------------------------------------------------------------------- #

_CAST_STATE_FA = {
    BroadcastState.sending: "admin_cast_state_sending",
    BroadcastState.done: "admin_cast_state_done",
    BroadcastState.cancelled: "admin_cast_state_cancelled",
}


async def _signed(body: str, message: Message, settings: Settings) -> str:
    """Add BRAND.md's closing block before the owner ever sees the preview.

    Signed *here*, at compose time, rather than in `_deliver`: «همان‌طور که
    می‌نویسی برای مشتری ارسال می‌شود» is a promise the prompt makes, so the text
    stored on the row, the text in the preview and the text the customer gets
    have to be one string. A broadcast that failed to resolve the bot's own
    username still goes out — unsigned is a blemish, not a reason to block the
    owner's outage announcement.
    """
    bot = message.bot
    if bot is None:
        return body
    try:
        username = await resolve_bot_username(bot, settings)
    except Exception:
        logger.exception("could not resolve bot username for the broadcast signature")
        return body
    return with_signature(body, username)


def _preview(body: str, *, limit: int = 600) -> str:
    clipped = body if len(body) <= limit else body[: limit - 1] + "…"
    return escape(clipped)


async def _running_broadcast_id(session: AsyncSession) -> int | None:
    row = await session.scalar(
        select(Broadcast).where(Broadcast.state == BroadcastState.sending).limit(1)
    )
    return int(row.id) if row else None


async def _render_cast_home(message: Message, session: AsyncSession) -> None:
    running = await _running_broadcast_id(session)
    if running is None:
        status = texts.admin_cast_idle
    else:
        progress = await get_progress(session, running)
        status = texts.admin_cast_running.format(
            id=to_persian_digits(running),
            sent=to_persian_digits(progress.sent),
            total=to_persian_digits(progress.total),
        )
    await _edit_or_answer_admin(
        message,
        texts.admin_cast_home.format(status=status),
        broadcast_home_keyboard(running),
    )


def _progress_text(progress: BroadcastProgress) -> str:
    state_key = _CAST_STATE_FA.get(progress.state, "admin_cast_state_sending")
    body = texts.admin_cast_progress.format(
        id=to_persian_digits(progress.broadcast_id),
        state=getattr(texts, state_key),
        sent=to_persian_digits(progress.sent),
        blocked=to_persian_digits(progress.blocked),
        failed=to_persian_digits(progress.failed),
        pending=to_persian_digits(progress.pending),
        total=to_persian_digits(progress.total),
    )
    if progress.blocked:
        body = f"{body}\n\n{texts.admin_cast_blocked_note}"
    return body


@router.callback_query(F.data == AdminCB.CAST)
async def admin_cast_home(
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
        await _render_cast_home(message, session)


@router.callback_query(F.data == AdminCB.CAST_NEW)
async def admin_cast_new(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_broadcast_body)
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await _edit_or_answer_admin(
            message,
            texts.admin_cast_prompt_body.format(limit=to_persian_digits(MAX_BODY_CHARS)),
            admin_cancel_input_keyboard(),
        )


@router.message(AdminStates.waiting_broadcast_body)
async def admin_cast_body(
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
    body = (message.text or "").strip()
    if not body or len(body) > MAX_BODY_CHARS:
        await message.answer(
            texts.admin_cast_prompt_body.format(limit=to_persian_digits(MAX_BODY_CHARS)),
            reply_markup=admin_cancel_input_keyboard(),
        )
        return
    body = await _signed(body, message, settings)
    await state.update_data(broadcast_body=body)
    counts = {
        audience.value: await count_audience(session, audience) for audience in BroadcastAudience
    }
    await message.answer(
        texts.admin_cast_pick_audience.format(preview=_preview(body)),
        reply_markup=broadcast_audience_keyboard(counts),
    )


# --------------------------------------------------------------------------- #
# Outage announcement — rides the broadcast pipeline, adds no delivery code
# --------------------------------------------------------------------------- #


async def _ann_to_audience(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    body: str,
    *,
    settings: Settings,
) -> None:
    """Hand a composed announcement to the existing broadcast confirm flow.

    From here an announcement is an ordinary broadcast: same audience picker,
    same two-tap confirm, same resumable worker. Nothing about delivery is
    duplicated for it.
    """
    await state.set_state(None)
    body = await _signed(body, message, settings)
    await state.update_data(broadcast_body=body)
    counts = {
        audience.value: await count_audience(session, audience) for audience in BroadcastAudience
    }
    await message.answer(
        texts.admin_ann_preview.format(preview=body),
        reply_markup=broadcast_audience_keyboard(counts),
    )


@router.callback_query(F.data == AdminCB.ANN)
async def admin_ann_home(
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
    if message is None:
        return
    running = await _running_broadcast_id(session)
    status = (
        texts.admin_cast_running.format(id=to_persian_digits(running))
        if running is not None
        else texts.admin_cast_idle
    )
    await _edit_or_answer_admin(
        message,
        texts.admin_ann_home.format(status=status),
        announcement_home_keyboard(),
    )


@router.callback_query(F.data == AdminCB.ANN_OUTAGE)
async def admin_ann_outage_start(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_outage_server)
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await _edit_or_answer_admin(
            message, texts.admin_ann_ask_server, admin_cancel_input_keyboard()
        )


@router.message(AdminStates.waiting_outage_server)
async def admin_ann_outage_server(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None, settings, db_user):
        await state.clear()
        await message.answer(texts.admin_action_denied, reply_markup=admin_back_home_keyboard())
        return
    try:
        server = clean_field(message.text or "", label="نام سرور")
    except AnnouncementError as exc:
        await message.answer(
            texts.admin_ann_invalid.format(error=str(exc)),
            reply_markup=admin_cancel_input_keyboard(),
        )
        return
    await state.update_data(ann_server=server)
    await state.set_state(AdminStates.waiting_outage_since)
    await message.answer(texts.admin_ann_ask_since, reply_markup=announcement_since_keyboard())


@router.callback_query(F.data == AdminCB.ANN_NOW)
async def admin_ann_outage_now(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.update_data(ann_since=now_hhmm(settings.display_timezone))
    await state.set_state(AdminStates.waiting_outage_alternative)
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await _edit_or_answer_admin(
            message, texts.admin_ann_ask_alternative, announcement_alternative_keyboard()
        )


@router.message(AdminStates.waiting_outage_since)
async def admin_ann_outage_since(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None, settings, db_user):
        await state.clear()
        await message.answer(texts.admin_action_denied, reply_markup=admin_back_home_keyboard())
        return
    try:
        since = clean_field(message.text or "", label="ساعت")
    except AnnouncementError as exc:
        await message.answer(
            texts.admin_ann_invalid.format(error=str(exc)),
            reply_markup=announcement_since_keyboard(),
        )
        return
    await state.update_data(ann_since=since)
    await state.set_state(AdminStates.waiting_outage_alternative)
    await message.answer(
        texts.admin_ann_ask_alternative, reply_markup=announcement_alternative_keyboard()
    )


async def _finish_outage(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    alternative: str,
    *,
    settings: Settings,
) -> None:
    data = await state.get_data()
    try:
        body = compose_outage(
            server=str(data.get("ann_server") or ""),
            since=str(data.get("ann_since") or ""),
            alternative=alternative,
        )
    except AnnouncementError as exc:
        await message.answer(
            texts.admin_ann_invalid.format(error=str(exc)),
            reply_markup=announcement_home_keyboard(),
        )
        return
    await _ann_to_audience(message, state, session, body, settings=settings)


@router.callback_query(F.data == AdminCB.ANN_SKIP)
async def admin_ann_outage_skip(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await _finish_outage(message, state, session, "-", settings=settings)


@router.message(AdminStates.waiting_outage_alternative)
async def admin_ann_outage_alternative(
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
    raw = (message.text or "").strip()
    await _finish_outage(message, state, session, "-" if is_skip(raw) else raw, settings=settings)


@router.callback_query(F.data == AdminCB.ANN_RESTORE)
async def admin_ann_restore_start(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_restore_server)
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await _edit_or_answer_admin(
            message, texts.admin_ann_ask_restore_server, admin_cancel_input_keyboard()
        )


@router.message(AdminStates.waiting_restore_server)
async def admin_ann_restore_server(
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
    try:
        body = compose_restore(server=message.text or "")
    except AnnouncementError as exc:
        await message.answer(
            texts.admin_ann_invalid.format(error=str(exc)),
            reply_markup=admin_cancel_input_keyboard(),
        )
        return
    await _ann_to_audience(message, state, session, body, settings=settings)


@router.callback_query(F.data.startswith(AdminCB.CAST_AUD))
async def admin_cast_pick_audience(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(AdminCB.CAST_AUD) :]
    try:
        audience = BroadcastAudience(raw)
    except ValueError:
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    data = await state.get_data()
    body = str(data.get("broadcast_body") or "")
    if not body:
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    count = await count_audience(session, audience)
    await state.update_data(broadcast_audience=audience.value)
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _edit_or_answer_admin(
        message,
        texts.admin_cast_confirm.format(
            count=to_persian_digits(count),
            audience=AUDIENCE_TITLES_FA[audience],
            preview=_preview(body),
        ),
        broadcast_confirm_keyboard(),
    )


@router.callback_query(F.data == AdminCB.CAST_TEST)
async def admin_cast_test_send(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    db_user: User | None,
) -> None:
    """The exact composed body to the tapping admin. No targets, no campaign."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    body = str((await state.get_data()).get("broadcast_body") or "")
    if not body or callback.from_user is None:
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    try:
        await bot.send_message(chat_id=callback.from_user.id, text=body)
    except Exception:
        logger.exception("broadcast test send failed")
        await callback.answer(texts.admin_cast_test_failed, show_alert=True)
        return
    await callback.answer(texts.admin_cast_test_sent, show_alert=True)


@router.callback_query(F.data == AdminCB.CAST_SEND)
async def admin_cast_send(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    data = await state.get_data()
    body = str(data.get("broadcast_body") or "")
    raw_audience = str(data.get("broadcast_audience") or "")
    if not body or not raw_audience:
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    try:
        broadcast = await create_broadcast(
            session,
            body=body,
            audience=BroadcastAudience(raw_audience),
            admin_telegram_id=callback.from_user.id if callback.from_user else None,
        )
        await session.commit()
    except BroadcastError as exc:
        await session.rollback()
        await callback.answer(str(exc), show_alert=True)
        return
    await state.clear()
    progress = await get_progress(session, int(broadcast.id))
    await callback.answer(texts.admin_cast_started.format(count=to_persian_digits(progress.total)))
    message = _tg_message(callback)
    if message is not None:
        await _edit_or_answer_admin(
            message,
            _progress_text(progress),
            broadcast_progress_keyboard(int(broadcast.id), running=True),
        )


@router.callback_query(F.data.startswith(AdminCB.CAST_CANCEL))
async def admin_cast_cancel(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(AdminCB.CAST_CANCEL) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    try:
        progress = await cancel_broadcast(session, int(raw))
        await session.commit()
    except BroadcastError as exc:
        await session.rollback()
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(texts.admin_cast_cancelled, show_alert=True)
    message = _tg_message(callback)
    if message is not None:
        await _edit_or_answer_admin(
            message,
            _progress_text(progress),
            broadcast_progress_keyboard(int(raw), running=False),
        )


@router.callback_query(F.data.startswith(AdminCB.CAST_STATUS))
async def admin_cast_status(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(AdminCB.CAST_STATUS) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    try:
        progress = await get_progress(session, int(raw))
    except BroadcastError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await _edit_or_answer_admin(
            message,
            _progress_text(progress),
            broadcast_progress_keyboard(int(raw), running=progress.state is BroadcastState.sending),
        )


# --------------------------------------------------------------------------- #
# Service QR background
#
# The service QR goes on the brand template by default. The owner can send a
# photo instead; it is square-cropped and the code sits in the same central
# box. Every screen shows a preview rendered by the same code the customer's
# card uses, so what the owner approves is what the customer gets.
# --------------------------------------------------------------------------- #

_QR_PREVIEW_URL = "https://example.com/sub/preview-only-not-a-real-link"


async def _render_qr_background(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    *,
    heading: str | None = None,
) -> None:
    from bot.delivery import service_qr_png
    from services.qr_brand import load_qr_background

    custom = await load_qr_background(session) is not None
    body = texts.admin_qr_bg_title.format(
        current=(texts.admin_qr_bg_current_custom if custom else texts.admin_qr_bg_current_template)
    )
    if heading:
        body = f"{heading}\n\n{body}"
    png = await service_qr_png(session, settings, url=_QR_PREVIEW_URL, bot=message.bot)
    await message.answer_photo(
        BufferedInputFile(png, filename="qr-preview.png"),
        caption=body,
        reply_markup=qr_background_keyboard(custom=custom),
    )


@router.callback_query(F.data == AdminCB.QR_BG)
async def admin_qr_background(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_qr_background)
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await _render_qr_background(message, session, settings)


@router.message(AdminStates.waiting_qr_background, F.photo)
async def admin_qr_background_upload(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    bot: Bot,
) -> None:
    from services.qr_brand import QrBackgroundError, save_qr_background

    if not _is_admin(message.from_user.id if message.from_user else None, settings, db_user):
        await state.clear()
        await message.answer(texts.admin_action_denied, reply_markup=admin_back_home_keyboard())
        return
    photo = (message.photo or [])[-1]
    buffer = await bot.download(photo.file_id)
    raw = buffer.read() if buffer is not None else b""
    try:
        await save_qr_background(session, raw)
        await session.commit()
    except QrBackgroundError as exc:
        await session.rollback()
        reason = getattr(texts, f"admin_qr_bg_reason_{exc}", str(exc))
        await message.answer(
            texts.admin_qr_bg_bad.format(reason=reason), reply_markup=admin_cancel_input_keyboard()
        )
        return
    await _render_qr_background(message, session, settings, heading=texts.admin_qr_bg_saved)


@router.message(AdminStates.waiting_qr_background)
async def admin_qr_background_not_a_photo(
    message: Message, settings: Settings, db_user: User | None
) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None, settings, db_user):
        return
    await message.answer(
        texts.admin_qr_bg_bad.format(reason=texts.admin_qr_bg_reason_not_an_image),
        reply_markup=admin_cancel_input_keyboard(),
    )


@router.callback_query(F.data == AdminCB.QR_BG_RESET)
async def admin_qr_background_reset_ask(
    callback: CallbackQuery, settings: Settings, db_user: User | None
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await message.answer(
            texts.admin_qr_bg_reset_ask, reply_markup=qr_background_reset_keyboard()
        )


@router.callback_query(F.data == AdminCB.QR_BG_RESET_OK)
async def admin_qr_background_reset(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    from services.qr_brand import clear_qr_background

    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await clear_qr_background(session)
    await session.commit()
    await state.set_state(AdminStates.waiting_qr_background)
    await callback.answer(texts.admin_qr_bg_reset_done)
    message = _tg_message(callback)
    if message is not None:
        await _render_qr_background(
            message, session, settings, heading=texts.admin_qr_bg_reset_done
        )

"""User account screen — named service list, detail, refresh, link."""

from __future__ import annotations

import logging
from datetime import datetime
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api_client.factory import get_pasarguard_client
from bot.config import Settings
from bot.delivery import service_qr_png
from bot.keyboards.account import (
    AccountCB,
    account_empty_keyboard,
    account_orders_keyboard,
    howto_platform_keyboard,
    parse_howto_callback,
    parse_revoke_callback,
    reseller_search_cancel_keyboard,
    reseller_services_keyboard,
    service_detail_keyboard,
    service_revoke_confirm_keyboard,
    services_list_keyboard,
)
from bot.keyboards.main_menu import MenuCallback, build_main_menu_keyboard
from bot.states import ResellerStates
from bot.texts import texts
from database.models import PasarGuardAccount, User
from services.account_orders import is_trial_account
from services.accounts import (
    AccountOverview,
    get_account_overview,
    list_named_services,
    list_services_page,
    live_account_clause,
    status_map_for_services,
)
from services.customer_label import CustomerLabel
from services.service_card import format_overview_text, truncate_caption
from services.service_ops import revoke_link
from services.subscription_qr import render_subscription_qr_png
from services.trial import user_trial_eligible
from services.ux_format import (
    as_rtl_lines,
    is_expiring_soon,
    to_persian_digits,
)
from services.ux_format import fa_toman as _fmt_toman

logger = logging.getLogger(__name__)
router = Router(name="account")


def _tg_message(callback: CallbackQuery) -> Message | None:
    message = callback.message
    if isinstance(message, Message):
        return message
    return None


def _status_fa(status: str | None) -> str:
    if not status:
        return texts.account_unknown
    return texts.account_status_labels.get(status, status)


def _warn_days(settings: Settings) -> int:
    return int(settings.service_expire_warn_days)


# Re-export for existing unit tests / call sites.
_truncate_caption = truncate_caption


async def _send_subscription_qr(message: Message, url: str, *, png: bytes | None = None) -> None:
    """Legacy helper: send QR photo with URL once in caption (no howto)."""
    cleaned = (url or "").strip()
    if not cleaned:
        return
    caption = _truncate_caption(f"{texts.account_qr_caption}\n<code>{escape(cleaned)}</code>")
    try:
        png = png if png is not None else render_subscription_qr_png(cleaned)
    except Exception:
        logger.exception("Failed to render subscription QR")
        await message.answer(f"{texts.account_qr_error}\n<code>{escape(cleaned)}</code>")
        return
    try:
        await message.answer_photo(
            BufferedInputFile(png, filename="subscription-qr.png"),
            caption=caption,
        )
    except Exception:
        logger.exception("Failed to send subscription QR photo")
        await message.answer(f"{texts.account_link_title}\n<code>{escape(cleaned)}</code>")


async def _deliver_subscription_qr(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    *,
    account_id: int | None = None,
) -> None:
    if db_user is None:
        await callback.answer(texts.account_need_user, show_alert=True)
        return
    try:
        overview = await _load_overview(session, settings, db_user.id, account_id=account_id)
    except Exception:
        logger.exception("Failed to load subscription QR for user %s", getattr(db_user, "id", None))
        await callback.answer(texts.account_error, show_alert=True)
        return
    if not overview.subscription_url:
        await callback.answer(texts.account_no_link, show_alert=True)
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        url = overview.subscription_url
        png = await service_qr_png(session, settings, url=url, bot=callback.bot)
        await _send_subscription_qr(message, url, png=png)


def _parse_trailing_id(data: str | None, prefix: str) -> int | None:
    if not data or not data.startswith(prefix):
        return None
    rest = data[len(prefix) :]
    if not rest.isdigit():
        return None
    return int(rest)


def format_orders_text(overview: AccountOverview) -> str:
    if not overview.recent_orders:
        return texts.account_orders_empty
    lines = [texts.account_orders_title, ""]
    for item in overview.recent_orders:
        lines.append(
            texts.account_order_line.format(
                order_id=to_persian_digits(item.order_id),
                status=_status_fa(item.status),
                price=_fmt_toman(item.price_toman),
            )
        )
    return "\n".join(lines)


async def _load_overview(
    session: AsyncSession,
    settings: Settings,
    user_id: int,
    *,
    account_id: int | None = None,
) -> AccountOverview:
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


async def _reply(
    target: CallbackQuery | Message,
    text: str,
    markup: InlineKeyboardMarkup | None,
    *,
    toast: str | None = None,
    answer: bool = True,
) -> None:
    """Answer either an inline tap or a bottom-keyboard tap.

    A reply-keyboard button arrives as the customer's own message, which cannot
    be edited — so that path always sends a new one.
    """
    if isinstance(target, Message):
        await target.answer(text, reply_markup=markup)
        return
    if answer:
        await target.answer(toast)
    message = _tg_message(target)
    if message is None:
        return
    try:
        await message.edit_text(text, reply_markup=markup)
    except Exception:
        await message.answer(text, reply_markup=markup)


async def _status_map_for_services(
    session: AsyncSession,
    settings: Settings,
    services: list,
) -> dict[int, tuple[str | None, datetime | None]]:
    """Best-effort status/expire per account (panel when possible, else order expire)."""
    if not services:
        return {}
    client = await get_pasarguard_client(session, settings)
    try:
        live = await status_map_for_services(session, client, services)
    finally:
        await client.aclose()
    return {account_id: (item.status, item.expire) for account_id, item in live.items()}


async def _show_reseller_services(
    target: CallbackQuery | Message,
    session: AsyncSession,
    settings: Settings,
    db_user: User,
    *,
    offset: int = 0,
    query: str | None = None,
    toast: str | None = None,
    answer: bool = True,
) -> None:
    """A shop's customer list — paged and searchable.

    Separate from the customer screen because the two answer different
    questions. A customer asks «سرویس خودم چطوره؟» and has two. A shop asks
    «سرویس آقای رضایی کِی تموم می‌شه؟» and has thirty, none of them their own.
    """
    try:
        page = await list_services_page(session, db_user.id, offset=offset, query=query)
    except Exception:
        logger.exception("Failed to page services for reseller %s", db_user.id)
        await _reply(target, texts.account_error, None, toast=toast, answer=answer)
        return

    if not page.rows and page.total == 0:
        body = (
            texts.reseller_services_none_found.format(query=escape(query))
            if query
            else texts.account_services_empty
        )
        await _reply(
            target,
            body,
            reseller_services_keyboard(page),
            toast=toast,
            answer=answer,
        )
        return

    try:
        status_by_id = await _status_map_for_services(session, settings, page.rows)
    except Exception:
        logger.exception("status map failed reseller=%s", db_user.id)
        status_by_id = {}

    header = (
        texts.reseller_services_found.format(
            query=escape(query), total=to_persian_digits(page.total)
        )
        if query
        else texts.reseller_services_title.format(
            total=to_persian_digits(page.total),
            first=to_persian_digits(page.offset + 1),
            last=to_persian_digits(page.offset + len(page.rows)),
        )
    )
    await _reply(
        target,
        header,
        reseller_services_keyboard(
            page, status_by_id=status_by_id, warn_days=settings.service_expire_warn_days
        ),
        toast=toast,
        answer=answer,
    )


async def _show_services_list(
    target: CallbackQuery | Message,
    session: AsyncSession,
    settings: Settings,
    db_user: User,
    *,
    toast: str | None = None,
    answer: bool = True,
) -> None:
    if db_user.is_reseller:
        await _show_reseller_services(
            target, session, settings, db_user, toast=toast, answer=answer
        )
        return
    try:
        services = await list_named_services(session, db_user.id)
    except Exception:
        logger.exception("Failed to list named services for user %s", db_user.id)
        if isinstance(target, Message):
            await target.answer(texts.account_error)
        elif answer:
            await target.answer(texts.account_error, show_alert=True)
        return

    if not services:
        eligible = False
        if settings.trial_enabled:
            eligible = await user_trial_eligible(session, settings, db_user)
        await _reply(
            target,
            texts.account_services_empty,
            account_empty_keyboard(trial_eligible=eligible),
            toast=toast,
            answer=answer,
        )
        return

    try:
        status_by_id = await _status_map_for_services(session, settings, services)
    except Exception:
        logger.exception("status map failed user=%s", db_user.id)
        status_by_id = {}

    await _reply(
        target,
        texts.account_services_title,
        services_list_keyboard(
            services,
            status_by_id=status_by_id,
            warn_days=_warn_days(settings),
        ),
        toast=toast,
        answer=answer,
    )


async def _present_service_detail_message(
    callback: CallbackQuery,
    overview: AccountOverview,
    *,
    warn_days: int,
    timezone: str | None = None,
    toast: str | None = None,
    can_rename: bool = False,
    is_trial: bool = False,
    qr_png: bytes | None = None,
) -> None:
    """Show service detail as ONE message: QR photo + caption when URL exists."""
    if overview.account_id is None:
        await callback.answer(texts.account_error, show_alert=True)
        return

    caption = _truncate_caption(
        format_overview_text(
            overview,
            warn_days=warn_days,
            timezone=timezone,
            # Same flag as the rename button: this screen belongs to a shop.
            show_customer=can_rename,
        )
    )
    show_renew = is_expiring_soon(overview.expire, warn_days=warn_days)
    markup = service_detail_keyboard(
        overview.account_id,
        has_link=bool(overview.subscription_url),
        show_renew=show_renew,
        can_rename=can_rename,
        is_trial=is_trial,
    )
    url = (overview.subscription_url or "").strip()
    message = _tg_message(callback)

    if not url:
        await _reply(callback, caption, markup, toast=toast)
        return

    png: bytes | None
    try:
        png = qr_png if qr_png is not None else render_subscription_qr_png(url)
    except Exception:
        logger.exception("Failed to render subscription QR for account %s", overview.account_id)
        png = None

    if png is None:
        await _reply(callback, caption, markup, toast=toast)
        return

    await callback.answer(toast)
    if message is None:
        return

    media = InputMediaPhoto(
        media=BufferedInputFile(png, filename="subscription-qr.png"),
        caption=caption,
    )

    # Refresh path: same photo message → edit in place.
    if message.photo:
        try:
            await message.edit_media(media=media, reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            if "not modified" in str(exc).lower():
                # Nothing changed since the last look. The card on screen is
                # already correct, so resending it would just duplicate it —
                # pressing «به‌روزرسانی» twice used to fill the chat with
                # identical cards and log a traceback each time.
                return
            logger.warning(
                "edit_media rejected for account %s (%s); sending new photo",
                overview.account_id,
                exc,
            )
        except Exception:
            logger.exception(
                "edit_media failed for account %s; sending new photo", overview.account_id
            )

    try:
        await message.answer_photo(
            BufferedInputFile(png, filename="subscription-qr.png"),
            caption=caption,
            reply_markup=markup,
        )
    except Exception:
        logger.exception("answer_photo failed for account %s; text fallback", overview.account_id)
        await message.answer(caption, reply_markup=markup)


async def _show_service_detail(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User,
    account_id: int,
    *,
    toast: str | None = None,
) -> None:
    try:
        overview = await _load_overview(session, settings, db_user.id, account_id=account_id)
    except Exception:
        logger.exception(
            "Failed to load account overview for user %s account %s",
            db_user.id,
            account_id,
        )
        await callback.answer(texts.account_error, show_alert=True)
        return

    if not overview.has_account or overview.account_id is None:
        await callback.answer(texts.account_error, show_alert=True)
        await _show_services_list(callback, session, settings, db_user, answer=False)
        return

    url = (overview.subscription_url or "").strip()
    account = await session.get(PasarGuardAccount, int(overview.account_id))
    await _present_service_detail_message(
        callback,
        overview,
        warn_days=_warn_days(settings),
        timezone=settings.display_timezone,
        toast=toast,
        can_rename=bool(getattr(db_user, "is_reseller", False)),
        is_trial=account is not None and await is_trial_account(session, account),
        qr_png=(
            await service_qr_png(session, settings, url=url, bot=callback.bot) if url else None
        ),
    )


async def show_services_list_for_message(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    db_user: User,
) -> None:
    """Public entry to «سرویس‌های من», for the bottom keyboard."""
    await _show_services_list(message, session, settings, db_user, answer=False)


@router.callback_query(F.data == MenuCallback.ACCOUNT)
async def menu_account(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if db_user is None:
        await callback.answer(texts.account_need_user, show_alert=True)
        return
    await _show_services_list(callback, session, settings, db_user)


@router.callback_query(F.data == AccountCB.LIST)
async def account_list(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if db_user is None:
        await callback.answer(texts.account_need_user, show_alert=True)
        return
    await _show_services_list(callback, session, settings, db_user)


@router.callback_query(F.data.startswith(AccountCB.SVC_REFRESH_PREFIX))
async def account_svc_refresh(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if db_user is None:
        await callback.answer(texts.account_need_user, show_alert=True)
        return
    account_id = _parse_trailing_id(callback.data, AccountCB.SVC_REFRESH_PREFIX)
    if account_id is None:
        await callback.answer(texts.account_error, show_alert=True)
        return
    await _show_service_detail(
        callback,
        session,
        settings,
        db_user,
        account_id,
        toast=texts.account_refreshed,
    )


@router.callback_query(F.data.startswith(AccountCB.SVC_LINK_PREFIX))
async def account_svc_link(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    account_id = _parse_trailing_id(callback.data, AccountCB.SVC_LINK_PREFIX)
    if account_id is None:
        await callback.answer(texts.account_error, show_alert=True)
        return
    await _deliver_subscription_qr(callback, session, settings, db_user, account_id=account_id)


@router.callback_query(F.data.startswith(AccountCB.SVC_QR_PREFIX))
async def account_svc_qr(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    account_id = _parse_trailing_id(callback.data, AccountCB.SVC_QR_PREFIX)
    if account_id is None:
        await callback.answer(texts.account_error, show_alert=True)
        return
    await _deliver_subscription_qr(callback, session, settings, db_user, account_id=account_id)


_HOWTO_PLATFORM_TEXTS = {
    "android": "account_howto_android",
    "ios": "account_howto_ios",
    "windows": "account_howto_windows",
    "macos": "account_howto_macos",
}


@router.callback_query(F.data.startswith(AccountCB.SVC_HOWTO_PREFIX))
async def account_svc_howto(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """Platform picker first; then short Persian guide for the chosen OS."""
    if db_user is None:
        await callback.answer(texts.account_need_user, show_alert=True)
        return
    parsed = parse_howto_callback(callback.data)
    if parsed is None:
        await callback.answer(texts.account_error, show_alert=True)
        return
    account_id, platform = parsed
    try:
        overview = await _load_overview(session, settings, db_user.id, account_id=account_id)
    except Exception:
        logger.exception("Failed to load howto context for account %s", account_id)
        await callback.answer(texts.account_error, show_alert=True)
        return
    if not overview.has_account:
        await callback.answer(texts.account_error, show_alert=True)
        return

    message = _tg_message(callback)
    if platform is None:
        await callback.answer()
        if message is not None:
            await message.answer(
                as_rtl_lines(texts.account_howto_pick_platform),
                reply_markup=howto_platform_keyboard(account_id),
            )
        return

    guide = getattr(texts, _HOWTO_PLATFORM_TEXTS[platform])
    await callback.answer()
    if message is not None:
        await message.answer(as_rtl_lines(guide), reply_markup=howto_platform_keyboard(account_id))


async def _revoke_subscription_link(
    session: AsyncSession,
    settings: Settings,
    *,
    user_id: int,
    account_id: int,
) -> str | None:
    """Revoke panel sub link, persist absolutized URL; return new URL or None on failure."""
    owned = await session.scalar(
        select(PasarGuardAccount.id).where(
            PasarGuardAccount.id == account_id,
            PasarGuardAccount.user_id == user_id,
            live_account_clause(),
        )
    )
    if owned is None:
        return None
    client = await get_pasarguard_client(session, settings)
    try:
        return await revoke_link(session, client, account_id=account_id)
    finally:
        await client.aclose()


@router.callback_query(F.data.startswith(AccountCB.SVC_REVOKE_PREFIX))
async def account_svc_revoke(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """Confirm (danger) then revoke subscription link for the customer's own service."""
    if db_user is None:
        await callback.answer(texts.account_need_user, show_alert=True)
        return
    parsed = parse_revoke_callback(callback.data)
    if parsed is None:
        await callback.answer(texts.account_error, show_alert=True)
        return
    account_id, is_confirm = parsed

    if not is_confirm:
        await callback.answer()
        message = _tg_message(callback)
        if message is not None:
            await message.answer(
                texts.account_revoke_warn,
                reply_markup=service_revoke_confirm_keyboard(account_id),
            )
        return

    try:
        overview = await _load_overview(session, settings, db_user.id, account_id=account_id)
    except Exception:
        logger.exception("Failed to load account %s before revoke", account_id)
        await callback.answer(texts.account_revoke_fail, show_alert=True)
        return

    if not overview.has_account or not overview.panel_username:
        await callback.answer(texts.account_revoke_fail, show_alert=True)
        return

    new_url = await _revoke_subscription_link(
        session,
        settings,
        user_id=db_user.id,
        account_id=account_id,
    )
    if not new_url:
        await callback.answer(texts.account_revoke_fail, show_alert=True)
        return

    await _show_service_detail(
        callback,
        session,
        settings,
        db_user,
        account_id,
        toast=texts.account_revoke_success,
    )


@router.callback_query(F.data.startswith(AccountCB.SVC_RENEW_PREFIX))
async def account_svc_renew(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """Jump into purchase with renew intent for this account_id."""
    if db_user is None:
        await callback.answer(texts.account_need_user, show_alert=True)
        return
    account_id = _parse_trailing_id(callback.data, AccountCB.SVC_RENEW_PREFIX)
    if account_id is None:
        await callback.answer(texts.account_error, show_alert=True)
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    from bot.handlers.purchase import start_renew_purchase

    await start_renew_purchase(
        message,
        state,
        session,
        settings,
        db_user,
        account_id=account_id,
    )


@router.callback_query(F.data.startswith(AccountCB.SVC_PREFIX))
async def account_svc_select(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if db_user is None:
        await callback.answer(texts.account_need_user, show_alert=True)
        return
    account_id = _parse_trailing_id(callback.data, AccountCB.SVC_PREFIX)
    if account_id is None:
        await callback.answer(texts.account_error, show_alert=True)
        return
    await _show_service_detail(callback, session, settings, db_user, account_id)


@router.callback_query(F.data == AccountCB.REFRESH)
async def account_refresh(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """Legacy refresh → return to named service list."""
    if db_user is None:
        await callback.answer(texts.account_need_user, show_alert=True)
        return
    await _show_services_list(callback, session, settings, db_user, toast=texts.account_refreshed)


@router.callback_query(F.data == AccountCB.ORDERS)
async def account_orders(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if db_user is None:
        await callback.answer(texts.account_need_user, show_alert=True)
        return
    try:
        overview = await _load_overview(session, settings, db_user.id)
    except Exception:
        logger.exception("Failed to load orders for user %s", db_user.id)
        await callback.answer(texts.account_error, show_alert=True)
        return
    await _reply(callback, format_orders_text(overview), account_orders_keyboard())


@router.callback_query(F.data == AccountCB.LINK)
async def account_show_link(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """Legacy link callback without account id."""
    await _deliver_subscription_qr(callback, session, settings, db_user)


@router.callback_query(F.data == AccountCB.QR)
async def account_show_qr(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """Legacy QR callback without account id."""
    await _deliver_subscription_qr(callback, session, settings, db_user)


@router.callback_query(F.data == AccountCB.BACK)
async def account_back(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    is_admin = bool(db_user and db_user.is_admin)
    user_id = db_user.id if db_user else None
    markup = await build_main_menu_keyboard(
        session,
        user_id=user_id,
        is_admin=is_admin,
        is_reseller=bool(getattr(db_user, "is_reseller", False)),
    )
    await _reply(callback, texts.main_menu, markup)


@router.callback_query(F.data.startswith(AccountCB.RS_PAGE))
async def reseller_services_page(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if db_user is None or not db_user.is_reseller:
        await callback.answer()
        return
    raw = (callback.data or "")[len(AccountCB.RS_PAGE) :]
    if not raw.isdigit():
        await callback.answer()
        return
    await state.clear()
    # ``_reply`` answers the tap; answering here as well made Telegram refuse
    # the second answer and the page never changed.
    await _show_reseller_services(callback, session, settings, db_user, offset=int(raw))


@router.callback_query(F.data == AccountCB.RS_CLEAR)
async def reseller_services_clear_search(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if db_user is None or not db_user.is_reseller:
        await callback.answer()
        return
    await state.clear()
    await _show_reseller_services(callback, session, settings, db_user)


@router.callback_query(F.data == AccountCB.RS_SEARCH)
async def reseller_services_search_start(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: User | None,
) -> None:
    if db_user is None or not db_user.is_reseller:
        await callback.answer()
        return
    await state.set_state(ResellerStates.waiting_service_query)
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await message.answer(
            texts.reseller_services_search_prompt,
            reply_markup=reseller_search_cancel_keyboard(),
        )


@router.message(ResellerStates.waiting_service_query)
async def reseller_services_search_run(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if db_user is None or not db_user.is_reseller:
        await state.clear()
        return
    query = (message.text or "").strip()
    if not query:
        await message.answer(
            texts.reseller_services_search_prompt,
            reply_markup=reseller_search_cancel_keyboard(),
        )
        return
    await state.clear()
    await _show_reseller_services(message, session, settings, db_user, query=query)


@router.callback_query(F.data.startswith(AccountCB.RS_RENAME))
async def reseller_rename_start(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    """Relabel a service the shop already holds.

    The reason this exists: services created before the bot asked — trials from
    an earlier version, or anything bought before the shop became a reseller —
    carry no customer name, and there was no way to add one afterwards.
    """
    if db_user is None or not db_user.is_reseller:
        await callback.answer()
        return
    raw = (callback.data or "")[len(AccountCB.RS_RENAME) :]
    if not raw.isdigit():
        await callback.answer()
        return
    account = await session.get(PasarGuardAccount, int(raw))
    if account is None or int(account.user_id) != int(db_user.id):
        # Not theirs, or gone. Same answer either way — never confirm that
        # someone else's service exists.
        await callback.answer(texts.account_error, show_alert=True)
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    from bot.handlers.customer_label import FLOW_RENAME, ask_customer_name

    current = (account.display_name or account.service_name or "—").strip()
    await ask_customer_name(
        message,
        state,
        flow=FLOW_RENAME,
        note=texts.reseller_rename_prompt.format(current=escape(current)),
        rename_account_id=int(raw),
    )


async def continue_rename_with_customer_label(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    *,
    customer: CustomerLabel,
) -> None:
    """Save the relabel once `bot.handlers.customer_label` has name and phone.

    Only `display_name` moves. The panel username was minted when the service
    was created and renaming it here would not rename it there — the two would
    simply stop agreeing.
    """
    label = str(customer)
    data = await state.get_data()
    account_id = data.get("rename_account_id")
    await state.clear()
    if db_user is None or not db_user.is_reseller or not account_id:
        return
    account = await session.get(PasarGuardAccount, int(account_id))
    if account is None or int(account.user_id) != int(db_user.id):
        await message.answer(texts.account_error)
        return
    account.display_name = label
    await session.commit()
    await message.answer(texts.reseller_rename_done.format(label=escape(label)))
    await _show_reseller_services(message, session, settings, db_user)

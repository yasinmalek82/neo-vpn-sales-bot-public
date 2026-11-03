"""STORE_ADMIN Telegram UX — مدیریت فروشگاه."""

from __future__ import annotations

import logging
from html import escape
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from api_client.factory import get_pasarguard_client
from bot.config import Settings
from bot.handlers.admin import _deny_if_not_admin, _is_admin, _tg_message
from bot.handlers.common import edit_or_send
from bot.keyboards.admin import AdminCB
from bot.keyboards.purchase import parse_duration_cb
from bot.keyboards.store_admin import (
    StoreCB,
    parse_pkg_cb,
    plan_button_label,
    store_back_hub_keyboard,
    store_cancel_input_keyboard,
    store_cat_group_keyboard,
    store_cat_panel_keyboard,
    store_cat_sale_keyboard,
    store_cat_trial_keyboard,
    store_category_dashboard_keyboard,
    store_category_delete_confirm_keyboard,
    store_category_durations_keyboard,
    store_category_kind_pick_keyboard,
    store_changes_keyboard,
    store_confirm_keyboard,
    store_discount_keyboard,
    store_duration_add_keyboard,
    store_duration_delete_confirm_keyboard,
    store_group_move_done_keyboard,
    store_group_move_keyboard,
    store_hub_keyboard,
    store_new_pkg_confirm_keyboard,
    store_new_pkg_duration_keyboard,
    store_new_pkg_size_keyboard,
    store_new_pkg_users_keyboard,
    store_package_delete_confirm_keyboard,
    store_package_detail_keyboard,
    store_panel_group_picker_keyboard,
    store_pay_cancel_keyboard,
    store_pay_card_confirm_keyboard,
    store_pay_clear_confirm_keyboard,
    store_pay_keyboard,
    store_plan_order_keyboard,
    store_price_jump_keyboard,
    store_promo_first_keyboard,
    store_promo_keyboard,
    store_promo_skip_keyboard,
    store_restore_confirm_keyboard,
)
from bot.states import AdminStates
from bot.texts import texts
from database.models import User
from database.models.store import StoreCategory, StorePackage, StorePackageKind
from services.group_sync import (
    accounts_of_category,
    move_accounts_to_groups,
    target_groups_for_category,
)
from services.panel_group_list import fetch_live_groups as _fetch_live_groups
from services.payment_settings import (
    FIELDS as PAYMENT_FIELDS,
)
from services.payment_settings import (
    PaymentInfo,
    PaymentSettingsError,
    format_card_number,
    get_payment_info,
    save_payment_field,
)
from services.payment_settings import clear_to_env as clear_payment_to_env
from services.payment_settings import validate_field as validate_payment_field
from services.promo_admin import (
    delete_promo_code,
    parse_code_and_percent,
    parse_promo_number,
    promo_lines,
    save_promo_code,
    toggle_promo_code,
)
from services.store_admin import (
    MAX_PANEL_GROUPS,
    CatalogView,
    StoreAdminError,
    StoreOwnerRequired,
    assert_store_owner,
    commit_and_activate,
    count_prices_for_duration,
    duration_label_fa,
    format_preview_fa,
    get_catalog_view,
    get_draft_category,
    get_draft_package,
    parse_panel_group_ids,
    preview_catalog,
    seed_store_if_empty,
)
from services.store_catalog import (
    category_kind,
    flat_plan_dim_label,
    hwid_limit_label_fa,
    is_volume_like,
    package_hwid_limit,
    plan_order_mode,
)
from services.store_changes import (
    apply_undo,
    change_log_text,
    undo_confirm_text,
    undone_text,
)
from services.store_config import is_store_owner
from services.store_dashboard import (
    group_facts,
    group_marks,
    hub_body,
    panel_group_label,
    parent_of,
    plan_order_label,
    retail_cell,
)
from services.store_edit import PriceJumpNeedsConfirm, set_package_price
from services.store_group_settings import (
    add_duration,
    parse_trial_input,
    remove_duration,
    rename_category,
    sale_hint,
    save_category_trial,
    save_hwid_limit,
    save_panel_groups,
    save_premium_groups,
    save_sort_order,
    services_left_on_old_groups,
    toggle_category_flag,
    toggle_flat_plan_list,
    toggle_tiers,
)
from services.store_plan_edit import (
    create_group,
    create_package,
    create_subgroup,
    derive_plan_title,
    dim_keys,
    group_delete_confirm,
    move_package,
    no_tab_warning,
    package_delete_confirm,
    parse_positive,
    read_group_title,
    read_hwid,
    read_plan_number,
    read_plan_title,
    read_price,
    remove_group,
    remove_package,
    save_package_hwid,
    set_package_dims,
    set_package_title,
    toggle_package_flag,
    toggle_plan_order,
)
from services.store_plan_view import ready_dims_complete
from services.store_strategy import (
    MAX_PROMO_DAYS,
    MAX_PROMO_LIMIT,
    get_sales_rules_view,
    list_promo_codes,
    list_store_changes,
    update_draft_sales_rules,
)
from services.ux_format import (
    format_toman,
    parse_positive_int,
    to_persian_digits,
)

logger = logging.getLogger(__name__)
router = Router(name="store_admin")


_GROUP_MOVE_FAILED_CAP = 20


def _owner(settings: Settings, tg_id: int | None) -> bool:
    return is_store_owner(settings, tg_id)


async def _deny_if_not_owner_write(callback: CallbackQuery, settings: Settings) -> bool:
    tg_id = callback.from_user.id if callback.from_user else None
    if _owner(settings, tg_id):
        return False
    await callback.answer(texts.admin_store_owner_only, show_alert=True)
    return True


async def _edit_or_answer(
    message: Message, body: str, reply_markup: InlineKeyboardMarkup | None
) -> None:
    await edit_or_send(message, body, reply_markup)


async def _badge(session: AsyncSession) -> str:
    """The one line every editing screen carries.

    It used to say «پیش‌نویس vN» or «نسخهٔ فعال vN», because those were two
    states the owner had to keep straight. There is only one now: what is on
    screen is what customers see, so the version number is noise and the draft
    half of the sentence describes something that no longer exists.
    """
    view = await get_catalog_view(session)
    if view.version is None:
        return texts.admin_store_badge_empty
    return texts.admin_store_badge_live


def _with_badge(badge: str, body: str) -> str:
    return f"{badge}\n\n{body}"


def _hub_summary(view: CatalogView, *, is_owner: bool) -> tuple[str, list[tuple[int, str]]]:
    """The hub's one status line and its top-level group buttons."""
    if not view.exists or not view.categories:
        return texts.admin_store_no_catalog, []
    body = hub_body(view, is_owner=is_owner)
    tops = sorted(
        (c for c in view.categories if c.parent_id is None), key=lambda c: (c.sort_order, c.id)
    )
    groups: list[tuple[int, str]] = [
        (int(cat.id), f"{group_marks(view, cat)}{cat.title_fa}") for cat in tops
    ]
    return body, groups


def _plan_buttons(view: CatalogView, cat: StoreCategory) -> list[tuple[int, str]]:
    """``(package id, label)`` for every plan of the group, shelf order."""
    prices = list(view.prices)
    return [
        (int(pkg.id), plan_button_label(pkg, retail_cell(prices, pkg)))
        for pkg in view.packages_of(cat.id)
    ]


def _dashboard_text(view: CatalogView, cat: StoreCategory) -> str:
    """Title, a compact status block, then a ⚠️ line for anything wrong."""
    facts = group_facts(view, cat, quote=escape)
    lines = [texts.admin_store_dash_title.format(title=escape(facts.title)), *facts.lines]
    if facts.warnings:
        lines.append("")
        lines.extend(facts.warnings)
    return "\n".join(lines)


def _pkg_retail_price(pkg: StorePackage, prices: list | None = None) -> int | None:
    """Economy-cell price from the matrix. Callers must pass ``prices``.

    Never touch ``pkg.store_version`` (lazy-load → MissingGreenlet) and never
    read ``fixed_price_toman`` (T-004 AST gate; only services/store_admin may).
    """
    if prices is None:
        return None
    return retail_cell(prices, pkg)


def _package_detail_text(pkg: StorePackage, prices: list | None = None) -> str:
    """Summary first (dims, price, device limit, states); the buttons are below."""
    kind_val = pkg.kind.value if hasattr(pkg.kind, "value") else str(pkg.kind)
    kind_emoji = "🎁" if kind_val == "ready" else "🧩"
    retail = _pkg_retail_price(pkg, prices)
    price = _fmt_price(retail) if retail is not None else "قیمت‌گذاری پویا"
    opts = pkg.options or {}
    lines = [f"{kind_emoji} {escape(pkg.title_fa)}"]
    if kind_val == "ready":
        dims = flat_plan_dim_label(pkg)
        lines.append(f"{dims} | {escape(str(price))}" if dims else escape(str(price)))
        if opts.get("volume_gb") is not None or opts.get("data_limit_gb") is not None:
            lines.append(f"کاربر همزمان: {hwid_limit_label_fa(package_hwid_limit(pkg))}")
    else:
        lines.append(escape(str(price)))
        label_map = {
            "volume_options_gb": "حجم‌ها (GB)",
            "duration_options_days": "مدت‌ها (روز)",
            "user_options": "تعداد کاربر",
            "duration_options_months": "مدت‌ها (ماه)",
        }
        for key, fa in label_map.items():
            if opts.get(key):
                lines.append(escape(f"{fa}: {opts[key]}"))
    lines.append(
        texts.admin_store_pkg_states.format(
            enabled="روشن" if pkg.enabled else "خاموش",
            new_sale="روشن" if pkg.allow_new_sale else "خاموش",
            renew="روشن" if pkg.allow_renew else "خاموش",
        )
    )
    lines.append(f"تخفیف اضافه: {'ممنوع' if pkg.forbid_extra_discount else 'مجاز'}")
    if kind_val == "ready" and not ready_dims_complete(pkg):
        # Such a plan silently diverts the customer into the dynamic builder and
        # its fixed price is ignored, so say so where the owner will see it.
        lines.append("")
        lines.append(texts.admin_store_pkg_incomplete)
    return "\n".join(lines)


@router.callback_query(F.data == AdminCB.STORE)
async def store_hub(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _render_hub(message, session, settings, callback.from_user)


async def _render_hub(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    from_user: Any | None,
) -> None:
    """The shop home screen. Shared so «انصراف» lands somewhere real."""
    tg_id = from_user.id if from_user else None
    owner = _owner(settings, tg_id)
    view = await get_catalog_view(session)
    body, groups = _hub_summary(view, is_owner=owner)
    kb = store_hub_keyboard(is_owner=owner, has_categories=bool(view.categories), groups=groups)
    await _edit_or_answer(message, body, kb)


# Screens the hub replaced. A button minted before the redesign still sits on
# the owner's screen, so each lands on the hub instead of spinning.
@router.callback_query(F.data == StoreCB.ADV)
@router.callback_query(F.data == StoreCB.STRAT)
@router.callback_query(F.data == StoreCB.CATS)
@router.callback_query(F.data == StoreCB.READY)
@router.callback_query(F.data == StoreCB.CUSTOM)
@router.callback_query(F.data == StoreCB.PKGS_PICK)
async def store_retired_to_hub(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    await store_hub(callback, settings, db_user, session, state)


@router.callback_query(F.data.startswith(StoreCB.CAT))
async def store_cat_detail(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    # Avoid colliding with longer prefixes handled elsewhere — CAT is adm:st:cat:
    data = callback.data or ""
    if not data.startswith(StoreCB.CAT) or data.startswith(
        (
            StoreCB.CAT_EN,
            StoreCB.CAT_NS,
            StoreCB.CAT_RN,
            StoreCB.CAT_TITLE,
            StoreCB.CAT_SORT,
            StoreCB.CAT_GROUPS,
            StoreCB.CAT_DEL,
            StoreCB.CAT_DEL_OK,
            StoreCB.CAT_DURS,
            StoreCB.CAT_DUR_ADD,
            StoreCB.CAT_DUR_NONE,
            StoreCB.CAT_DUR_DEL,
            StoreCB.CAT_DUR_DEL_OK,
            StoreCB.CAT_TIERS,
            StoreCB.CAT_TIER_GROUPS,
            StoreCB.CAT_TL,
            StoreCB.CAT_FLAT,
            StoreCB.CAT_CUSTOM,
            StoreCB.CAT_KIND,
            StoreCB.CAT_ADD,
            StoreCB.CAT_ADD_KIND,
            StoreCB.CAT_SUB_ADD,
            StoreCB.CAT_SET,
        )
    ):
        return
    raw = data[len(StoreCB.CAT) :]
    if not raw.isdigit():
        await callback.answer()
        return
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    tg_id = callback.from_user.id if callback.from_user else None
    owner = _owner(settings, tg_id)
    # Read-only: opening a group must not create a draft, which is what nine
    # browse screens used to do (burning a version number per visit).
    view = await get_catalog_view(session)
    cat = view.category(int(raw))
    if cat is None:
        await _edit_or_answer(message, "گروه محصول یافت نشد.", store_back_hub_keyboard())
        return
    await _render_category(message, view, cat, owner=owner)


async def _render_category(
    message: Message,
    view: CatalogView,
    cat: StoreCategory,
    *,
    owner: bool,
    notice: str | None = None,
) -> None:
    """The group dashboard. ``notice`` rides on the same message as the screen,
    so a successful edit does not leave a bare confirmation with no buttons."""
    badge = texts.admin_store_badge_live if view.version else texts.admin_store_badge_empty
    body = _dashboard_text(view, cat)
    if notice:
        body = f"✅ {notice}\n\n{body}"
    await _edit_or_answer(
        message,
        _with_badge(badge, body),
        store_category_dashboard_keyboard(
            cat,
            is_owner=owner,
            children=view.children_of(cat.id),
            plan_buttons=_plan_buttons(view, cat),
        ),
    )


SECTION_SALE = "sale"
SECTION_TRIAL = "trial"
SECTION_PANEL = "panel"
SECTION_GROUP = "group"
SECTION_ORDER = "order"


async def _render_section(
    message: Message,
    view: CatalogView,
    cat: StoreCategory,
    section: str,
    *,
    notice: str | None = None,
) -> None:
    """One of the four short screens under the dashboard (or the plan order).

    Every edit inside a section comes back to *that* section, so changing
    three settings of the same kind is three taps, not three trips through the
    dashboard.
    """
    badge = texts.admin_store_badge_live if view.version else texts.admin_store_badge_empty
    title = escape(cat.title_fa)
    if section == SECTION_SALE:
        body = texts.admin_store_sec_sale.format(title=title)
        hint = sale_hint(cat)
        if hint:
            body = f"{body}\n\n{hint}"
        kb = store_cat_sale_keyboard(cat)
    elif section == SECTION_TRIAL:
        body = texts.admin_store_sec_trial.format(title=title)
        kb = store_cat_trial_keyboard(cat)
    elif section == SECTION_PANEL:
        body = texts.admin_store_sec_panel.format(
            title=title, groups=escape(panel_group_label(cat, parent_of(view, cat)))
        )
        kb = store_cat_panel_keyboard(cat)
    elif section == SECTION_ORDER:
        body = texts.admin_store_sec_order.format(title=title)
        manual = plan_order_mode(cat) == "manual"
        lines = [f"{i}. {label}" for i, (_pid, label) in enumerate(_plan_buttons(view, cat), 1)]
        if lines:
            body = f"{body}\n\n" + "\n".join(lines)
        kb = store_plan_order_keyboard(
            cat,
            _plan_buttons(view, cat),
            mode_label=plan_order_label(cat),
            manual=manual,
        )
    else:
        body = texts.admin_store_sec_group.format(title=title)
        kb = store_cat_group_keyboard(
            cat,
            has_plans=bool(view.packages_of(cat.id)),
            has_children=bool(view.children_of(cat.id)),
        )
    if notice:
        body = f"✅ {notice}\n\n{body}"
    await _edit_or_answer(message, _with_badge(badge, body), kb)


def _section_handler(prefix: str, section: str) -> Any:
    async def handler(
        callback: CallbackQuery,
        settings: Settings,
        db_user: User | None,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        if await _deny_if_not_admin(callback, settings, db_user):
            return
        if await _deny_if_not_owner_write(callback, settings):
            return
        raw = (callback.data or "")[len(prefix) :]
        if not raw.isdigit():
            await callback.answer()
            return
        await state.clear()
        await callback.answer()
        message = _tg_message(callback)
        if message is None:
            return
        view = await get_catalog_view(session)
        cat = view.category(int(raw))
        if cat is None:
            await _edit_or_answer(message, "گروه محصول یافت نشد.", store_back_hub_keyboard())
            return
        await _render_section(message, view, cat, section)

    handler.__name__ = f"store_section_{section}"
    return handler


for _prefix, _section in (
    (StoreCB.SEC_SALE, SECTION_SALE),
    (StoreCB.SEC_TRIAL, SECTION_TRIAL),
    (StoreCB.SEC_PANEL, SECTION_PANEL),
    (StoreCB.SEC_GROUP, SECTION_GROUP),
    (StoreCB.SEC_ORDER, SECTION_ORDER),
):
    router.callback_query(F.data.startswith(_prefix))(_section_handler(_prefix, _section))


@router.callback_query(F.data.startswith(StoreCB.CAT_SET))
@router.callback_query(F.data.startswith(StoreCB.PKGS))
async def store_retired_to_group(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """«تنظیمات بیشتر» and the plan list are the dashboard now."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    data = callback.data or ""
    prefix = StoreCB.CAT_SET if data.startswith(StoreCB.CAT_SET) else StoreCB.PKGS
    raw = data[len(prefix) :]
    if not raw.isdigit():
        await callback.answer()
        return
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    tg_id = callback.from_user.id if callback.from_user else None
    view = await get_catalog_view(session)
    cat = view.category(int(raw))
    if cat is None:
        await _edit_or_answer(message, "گروه محصول یافت نشد.", store_back_hub_keyboard())
        return
    await _render_category(message, view, cat, owner=_owner(settings, tg_id))


async def _toggle_cat_flag(  # noqa: PLR0911
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    *,
    prefix: str,
    field: str,
    section: str = SECTION_SALE,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(prefix) :]
    if not raw.isdigit():
        await callback.answer()
        return
    message = _tg_message(callback)
    if message is None:
        return
    try:
        await toggle_category_flag(session, int(raw), field)
    except StoreAdminError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(texts.admin_store_saved)
    view = await get_catalog_view(session)
    live = view.category(int(raw))
    if live is None:
        await _edit_or_answer(message, "گروه محصول یافت نشد.", store_back_hub_keyboard())
        return
    await _render_section(message, view, live, section, notice=texts.admin_store_saved)


@router.callback_query(F.data.startswith(StoreCB.CAT_EN))
async def store_cat_toggle_en(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    await _toggle_cat_flag(
        callback, settings, db_user, session, prefix=StoreCB.CAT_EN, field="enabled"
    )


@router.callback_query(F.data.startswith(StoreCB.CAT_NS))
async def store_cat_toggle_ns(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    await _toggle_cat_flag(
        callback,
        settings,
        db_user,
        session,
        prefix=StoreCB.CAT_NS,
        field="allow_new_sale",
    )


@router.callback_query(F.data.startswith(StoreCB.CAT_RN))
async def store_cat_toggle_rn(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    await _toggle_cat_flag(
        callback, settings, db_user, session, prefix=StoreCB.CAT_RN, field="allow_renew"
    )


@router.callback_query(F.data.startswith(StoreCB.CAT_CUSTOM))
async def store_cat_toggle_custom(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    await _toggle_cat_flag(
        callback,
        settings,
        db_user,
        session,
        prefix=StoreCB.CAT_CUSTOM,
        field="custom_purchase_mode",
    )


@router.callback_query(F.data.startswith(StoreCB.CAT_TL))
async def store_cat_toggle_tl(callback: CallbackQuery) -> None:
    """Retired: purchase never reads ``time_limit_filter_enabled`` (T-006)."""
    await callback.answer(texts.admin_store_tl_filter_retired, show_alert=True)


@router.callback_query(F.data.startswith(StoreCB.CAT_FLAT))
async def store_cat_toggle_flat(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.CAT_FLAT) :]
    if not raw.isdigit():
        await callback.answer()
        return
    message = _tg_message(callback)
    if message is None:
        return
    try:
        await toggle_flat_plan_list(session, int(raw))
    except StoreAdminError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(texts.admin_store_saved)
    view = await get_catalog_view(session)
    live = view.category(int(raw))
    if live is None:
        await _edit_or_answer(message, "گروه محصول یافت نشد.", store_back_hub_keyboard())
        return
    await _render_section(message, view, live, SECTION_SALE, notice=texts.admin_store_saved)


@router.callback_query(F.data.startswith(StoreCB.CAT_HWID))
async def store_cat_hwid_start(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """Edit ``volume_hwid_limit`` for custom volume purchases in this group."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.CAT_HWID) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    view = await get_catalog_view(session)
    cat = view.category(int(raw))
    if cat is None or not is_volume_like(cat):
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await state.update_data(hwid_cat_id=int(raw), hwid_pkg_id=None)
    await state.set_state(AdminStates.store_waiting_new_pkg_users)
    await _edit_or_answer(message, texts.admin_store_pick_users, store_new_pkg_users_keyboard())


@router.callback_query(F.data.startswith(StoreCB.CAT_KIND))
async def store_cat_toggle_kind(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    await _toggle_cat_flag(
        callback,
        settings,
        db_user,
        session,
        prefix=StoreCB.CAT_KIND,
        field="kind",
        section=SECTION_GROUP,
    )


@router.callback_query(F.data == StoreCB.CAT_ADD)
async def store_cat_add_start(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await state.set_state(AdminStates.store_waiting_cat_new_title)
    await _edit_or_answer(message, texts.admin_store_prompt_cat_new, store_cancel_input_keyboard())


@router.message(AdminStates.store_waiting_cat_new_title)
async def store_cat_add_title(
    message: Message,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    tg_id = message.from_user.id if message.from_user else None
    if not _is_admin(tg_id, settings, db_user):
        await message.answer(texts.admin_action_denied, reply_markup=store_back_hub_keyboard())
        return
    if not _owner(settings, tg_id):
        await message.answer(texts.admin_store_owner_only, reply_markup=store_back_hub_keyboard())
        return
    title = (message.text or "").strip()
    if not title:
        await message.answer(
            texts.admin_store_invalid_input,
            reply_markup=store_cancel_input_keyboard(),
        )
        return
    await state.update_data(new_cat_title=title)
    await state.set_state(AdminStates.store_waiting_cat_kind)
    await message.answer(
        texts.admin_store_prompt_cat_kind,
        reply_markup=store_category_kind_pick_keyboard(),
    )


@router.callback_query(F.data.startswith(StoreCB.CAT_ADD_KIND))
async def store_cat_add_kind(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    kind = (callback.data or "")[len(StoreCB.CAT_ADD_KIND) :]
    data = await state.get_data()
    title = data.get("new_cat_title")
    message = _tg_message(callback)
    if message is None:
        return
    # Only the picker armed by the title prompt may create a group. Without this
    # guard, tapping a kind button left over on an older message created a
    # duplicate group from a title the operator had already abandoned.
    in_picker = await state.get_state() == AdminStates.store_waiting_cat_kind.state
    if not title or not in_picker:
        await state.update_data(new_cat_title=None)
        await callback.answer(texts.stale_button, show_alert=True)
        return
    try:
        cat = await create_group(session, str(title), kind)
        await state.update_data(new_cat_title=None)
    except StoreAdminError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(texts.admin_store_cat_created.format(title=cat.title_fa))
    view = await get_catalog_view(session)
    live = view.category(int(cat.id))
    notice = texts.admin_store_cat_created.format(title=escape(cat.title_fa))
    if live is None:
        await _edit_or_answer(message, notice, store_back_hub_keyboard())
        return
    await _render_category(message, view, live, owner=True, notice=notice)


@router.callback_query(F.data.startswith(StoreCB.CAT_TITLE))
async def store_cat_edit_title(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.CAT_TITLE) :]
    if not raw.isdigit():
        await callback.answer()
        return
    await state.set_state(AdminStates.store_waiting_cat_title)
    await state.update_data(store_cat_id=int(raw))
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _edit_or_answer(
        message, texts.admin_store_prompt_cat_title, store_cancel_input_keyboard()
    )


@router.callback_query(F.data.startswith(StoreCB.CAT_RTRIAL))
async def store_cat_toggle_reseller_trial(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    await _toggle_cat_flag(
        callback,
        settings,
        db_user,
        session,
        prefix=StoreCB.CAT_RTRIAL,
        field="reseller_trial",
        section=SECTION_TRIAL,
    )


@router.callback_query(F.data.startswith(StoreCB.CAT_TRIAL))
async def store_cat_trial_start(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    """Ask for this group's trial as «GB days»; 0 switches it off."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.CAT_TRIAL) :]
    if not raw.isdigit():
        await callback.answer()
        return
    await state.set_state(AdminStates.store_waiting_cat_trial)
    await state.update_data(store_cat_id=int(raw))
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _edit_or_answer(
        message, texts.admin_store_prompt_cat_trial, store_cancel_input_keyboard()
    )


_parse_trial_input = parse_trial_input  # the parser moved to services; the name stays for callers


@router.message(AdminStates.store_waiting_cat_trial)
async def store_cat_trial_input(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    tg_id = message.from_user.id if message.from_user else None
    if not _is_admin(tg_id, settings, db_user):
        return
    try:
        assert_store_owner(settings, tg_id)
    except StoreOwnerRequired:
        await message.answer(texts.admin_store_owner_only, reply_markup=store_back_hub_keyboard())
        await state.clear()
        return
    cat_id = (await state.get_data()).get("store_cat_id")
    if not cat_id:
        await state.clear()
        await message.answer(texts.admin_store_lost_context, reply_markup=store_back_hub_keyboard())
        return
    parsed = _parse_trial_input(message.text or "")
    if parsed is None:
        await message.answer(
            texts.admin_store_prompt_cat_trial, reply_markup=store_cancel_input_keyboard()
        )
        return
    gb, days = parsed
    try:
        await save_category_trial(session, int(cat_id), gb=gb, days=days)
    except StoreAdminError as exc:
        await session.rollback()
        await message.answer(
            str(exc) or texts.admin_store_invalid_input,
            reply_markup=store_cancel_input_keyboard(),
        )
        return
    await state.clear()
    view = await get_catalog_view(session)
    live = view.category(int(cat_id))
    if live is None:
        await message.answer(texts.admin_store_lost_context, reply_markup=store_back_hub_keyboard())
        return
    await _render_section(message, view, live, SECTION_TRIAL, notice=texts.admin_store_saved)


@router.callback_query(F.data.startswith(StoreCB.CAT_SORT))
async def store_cat_edit_sort(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.CAT_SORT) :]
    if not raw.isdigit():
        await callback.answer()
        return
    await state.set_state(AdminStates.store_waiting_cat_sort)
    await state.update_data(store_cat_id=int(raw))
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _edit_or_answer(message, texts.admin_store_prompt_cat_sort, store_cancel_input_keyboard())


PICK_MAIN = "main"
PICK_TIER = "tier"


def _picker_body(title: str, kind: str, selected: list[int], unreachable: bool) -> str:
    what = texts.admin_store_pick_what_tier if kind == PICK_TIER else texts.admin_store_pick_what
    hint = texts.admin_store_pick_hint_tier if kind == PICK_TIER else texts.admin_store_pick_hint
    chosen = "، ".join(f"#{gid}" for gid in selected) or texts.admin_store_pick_none
    body = texts.admin_store_pick_groups.format(
        what=what, title=escape(title), selected=chosen, hint=hint
    )
    if unreachable:
        body = f"{body}\n\n{texts.admin_store_pick_unreachable}"
    return body


async def _open_group_picker(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
    kind: str,
    raw: str,
) -> None:
    """Both panel-group settings open the same picker: the panel's groups as toggles.

    The owner used to type one id from memory. The list comes from the panel
    once, on open, and rides in FSM data so each toggle is not another panel
    call. If the panel cannot answer, typing ids still works.
    """
    await callback.answer()
    message = _tg_message(callback)
    if message is None or not raw.isdigit():
        return
    view = await get_catalog_view(session)
    cat = view.category(int(raw))
    if cat is None:
        await _edit_or_answer(message, texts.admin_store_lost_context, store_back_hub_keyboard())
        return
    if kind == PICK_TIER:
        current = (cat.sales_flags or {}).get("premium_panel_group_ids") or []
        await state.set_state(AdminStates.store_waiting_cat_tier_groups)
    else:
        current = cat.panel_group_ids or []
        await state.set_state(AdminStates.store_waiting_cat_groups)
    selected = [int(gid) for gid in current]
    groups, err = await _fetch_live_groups(session, settings)
    await state.update_data(
        store_cat_id=cat.id,
        store_pick_kind=kind,
        store_pick_ids=selected,
        store_pick_groups=[list(g) for g in groups],
        store_pick_title=cat.title_fa,
    )
    await _edit_or_answer(
        message,
        _picker_body(cat.title_fa, kind, selected, unreachable=bool(err) or not groups),
        store_panel_group_picker_keyboard(cat.id, groups, selected),
    )


@router.callback_query(F.data.startswith(StoreCB.CAT_GROUPS))
async def store_cat_edit_groups(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.CAT_GROUPS) :]
    await _open_group_picker(callback, session, settings, state, PICK_MAIN, raw)


@router.callback_query(F.data.startswith(StoreCB.GROUP_PICK))
async def store_group_pick_toggle(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.GROUP_PICK) :]
    cat_raw, _, gid_raw = raw.partition(":")
    data = await state.get_data()
    message = _tg_message(callback)
    if (
        message is None
        or not cat_raw.isdigit()
        or not gid_raw.isdigit()
        or data.get("store_cat_id") != int(cat_raw)
        or "store_pick_kind" not in data
    ):
        await callback.answer(texts.admin_store_pick_expired, show_alert=True)
        return
    selected = [int(g) for g in data.get("store_pick_ids") or []]
    gid = int(gid_raw)
    if gid in selected:
        selected.remove(gid)
    elif len(selected) >= MAX_PANEL_GROUPS:
        await callback.answer(
            texts.admin_store_pick_max.format(n=MAX_PANEL_GROUPS), show_alert=True
        )
        return
    else:
        selected.append(gid)
    await state.update_data(store_pick_ids=selected)
    await callback.answer()
    groups = [(int(g[0]), str(g[1]), bool(g[2])) for g in data.get("store_pick_groups") or []]
    await _edit_or_answer(
        message,
        _picker_body(
            str(data.get("store_pick_title") or ""),
            str(data["store_pick_kind"]),
            selected,
            unreachable=not groups,
        ),
        store_panel_group_picker_keyboard(int(cat_raw), groups, selected),
    )


@router.callback_query(F.data.startswith(StoreCB.GROUP_PICK_SAVE))
async def store_group_pick_save(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.GROUP_PICK_SAVE) :]
    data = await state.get_data()
    message = _tg_message(callback)
    if (
        message is None
        or not raw.isdigit()
        or data.get("store_cat_id") != int(raw)
        or "store_pick_kind" not in data
    ):
        await callback.answer(texts.admin_store_pick_expired, show_alert=True)
        return
    await callback.answer()
    ids = [int(g) for g in data.get("store_pick_ids") or []]
    await _save_picked_groups(
        message,
        state,
        session,
        settings,
        int(raw),
        str(data["store_pick_kind"]),
        ids,
        from_button=True,
    )


@router.message(AdminStates.store_waiting_cat_groups)
@router.message(AdminStates.store_waiting_cat_tier_groups)
async def store_group_pick_typed(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    """Typed ids — the fallback when the panel could not list its groups."""
    tg_id = message.from_user.id if message.from_user else None
    if not _is_admin(tg_id, settings, db_user):
        return
    try:
        assert_store_owner(settings, tg_id)
    except StoreOwnerRequired:
        await message.answer(texts.admin_store_owner_only, reply_markup=store_back_hub_keyboard())
        await state.clear()
        return
    data = await state.get_data()
    cat_id = data.get("store_cat_id")
    if not cat_id:
        await state.clear()
        await message.answer(texts.admin_store_lost_context, reply_markup=store_back_hub_keyboard())
        return
    cur = await state.get_state()
    kind = PICK_TIER if cur == AdminStates.store_waiting_cat_tier_groups.state else PICK_MAIN
    try:
        ids = parse_panel_group_ids(message.text or "")
    except StoreAdminError as exc:
        await message.answer(
            str(exc) or texts.admin_store_invalid_input,
            reply_markup=store_cancel_input_keyboard(),
        )
        return
    await _save_picked_groups(
        message, state, session, settings, int(cat_id), kind, ids, from_button=False
    )


async def _save_picked_groups(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    cat_id: int,
    kind: str,
    ids: list[int],
    *,
    from_button: bool,
) -> None:
    """Save either panel-group setting; the main one may ask to move services.

    From a button the picker message is edited; after a typed reply the bot
    cannot edit the owner's message, so it answers.
    """
    notice = texts.admin_store_saved
    groups_before: list[int] | None = None
    try:
        if kind == PICK_TIER:
            _cat, disabled = await save_premium_groups(session, cat_id, ids)
            if disabled:
                notice = f"{notice}\n{texts.admin_store_tier_disabled_no_groups}"
        else:
            groups_before = await save_panel_groups(session, settings, cat_id, ids)
    except StoreAdminError as exc:
        await message.answer(
            str(exc) or texts.admin_store_invalid_input,
            reply_markup=store_cancel_input_keyboard(),
        )
        return
    await state.clear()
    view = await get_catalog_view(session)
    live = view.category(cat_id)
    if live is None:
        await message.answer(texts.admin_store_lost_context, reply_markup=store_back_hub_keyboard())
        return
    if groups_before is not None:
        count = await services_left_on_old_groups(session, settings, live.id, groups_before)
        if count:
            body = f"✅ {texts.admin_store_saved}\n\n" + texts.admin_store_group_move_ask.format(
                count=count
            )
            markup = store_group_move_keyboard(live.id, groups_before or [])
            if from_button:
                await _edit_or_answer(message, body, markup)
            else:
                await message.answer(body, reply_markup=markup)
            return
    await _render_section(message, view, live, SECTION_PANEL, notice=notice)


@router.message(AdminStates.store_waiting_cat_title)
@router.message(AdminStates.store_waiting_cat_sort)
async def store_cat_fsm_input(  # noqa: PLR0911
    message: Message,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    tg_id = message.from_user.id if message.from_user else None
    if not _is_admin(tg_id, settings, db_user):
        return
    try:
        assert_store_owner(settings, tg_id)
    except StoreOwnerRequired:
        await message.answer(texts.admin_store_owner_only, reply_markup=store_back_hub_keyboard())
        await state.clear()
        return
    data = await state.get_data()
    cat_id = data.get("store_cat_id")
    if not cat_id:
        await state.clear()
        await message.answer(texts.admin_store_lost_context, reply_markup=store_back_hub_keyboard())
        return
    cur = await state.get_state()
    text = (message.text or "").strip()
    try:
        if cur == AdminStates.store_waiting_cat_title.state:
            await rename_category(session, int(cat_id), text)
        elif cur == AdminStates.store_waiting_cat_sort.state:
            await save_sort_order(session, int(cat_id), text)
    except StoreAdminError as exc:
        await message.answer(
            str(exc) or texts.admin_store_invalid_input,
            reply_markup=store_cancel_input_keyboard(),
        )
        return
    except ValueError:
        # int()/float() text is English; never show it to a Persian operator.
        await message.answer(
            texts.admin_store_invalid_input,
            reply_markup=store_cancel_input_keyboard(),
        )
        return
    await state.clear()
    view = await get_catalog_view(session)
    live = view.category(int(cat_id))
    if live is None:
        await message.answer(texts.admin_store_lost_context, reply_markup=store_back_hub_keyboard())
        return
    await _render_section(message, view, live, SECTION_GROUP, notice=texts.admin_store_saved)


@router.callback_query(F.data.startswith(StoreCB.CAT_GROUP_KEEP))
async def store_cat_group_keep(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(StoreCB.CAT_GROUP_KEEP) :]
    await callback.answer()
    message = _tg_message(callback)
    if message is None or not raw.isdigit():
        return
    await _edit_or_answer(
        message,
        texts.admin_store_group_move_kept,
        store_group_move_done_keyboard(int(raw)),
    )


@router.callback_query(F.data.startswith(StoreCB.CAT_GROUP_MOVE))
async def store_cat_group_move(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.CAT_GROUP_MOVE) :]
    await callback.answer()
    message = _tg_message(callback)
    cat_raw, _, old_raw = raw.partition(":")
    old_parts = [p for p in old_raw.split("-") if p]
    if message is None or not cat_raw.isdigit() or not all(p.isdigit() for p in old_parts):
        return
    cat_id = int(cat_raw)
    from_groups = [int(p) for p in old_parts]
    back = store_group_move_done_keyboard(cat_id)
    # Re-resolve at tap time: the button may be an hour old.
    groups = await target_groups_for_category(session, settings, cat_id)
    accounts = await accounts_of_category(session, cat_id)
    if not groups or not from_groups:
        await _edit_or_answer(message, texts.admin_store_group_move_no_target, back)
        return
    if not accounts:
        await _edit_or_answer(message, texts.admin_store_group_move_none, back)
        return
    try:
        client = await get_pasarguard_client(session, settings)
    except Exception:
        logger.exception("group move: cannot open panel client")
        await _edit_or_answer(message, texts.admin_store_group_move_panel_down, back)
        return
    try:
        result = await move_accounts_to_groups(client, accounts, groups, from_groups=from_groups)
    finally:
        await client.aclose()
    body = texts.admin_store_group_move_report.format(
        moved=result.moved,
        already=result.already,
        skipped=result.skipped,
        failed=len(result.failed),
    )
    if result.failed:
        shown = result.failed[:_GROUP_MOVE_FAILED_CAP]
        names = "\n".join(shown)
        if len(result.failed) > len(shown):
            names += f"\n… و {len(result.failed) - len(shown)} مورد دیگه"
        body += texts.admin_store_group_move_failed_list.format(names=names)
    await _edit_or_answer(message, body, back)


@router.callback_query(F.data.startswith(StoreCB.PKG_ORDER))
async def store_pkg_order_toggle(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.PKG_ORDER) :]
    if not raw.isdigit():
        await callback.answer()
        return
    cat_id = int(raw)
    try:
        await toggle_plan_order(session, cat_id)
        view = await get_catalog_view(session)
        live = view.category(cat_id)
    except StoreAdminError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(texts.admin_store_saved)
    message = _tg_message(callback)
    if message and live is not None:
        await _render_section(message, view, live, SECTION_ORDER, notice=texts.admin_store_saved)


@router.callback_query(F.data.startswith(StoreCB.PKG_UP))
async def store_pkg_move_up(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    await _store_pkg_move(callback, settings, db_user, session, direction="up")


@router.callback_query(F.data.startswith(StoreCB.PKG_DN))
async def store_pkg_move_down(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    await _store_pkg_move(callback, settings, db_user, session, direction="down")


async def _store_pkg_move(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    *,
    direction: str,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    prefix = StoreCB.PKG_UP if direction == "up" else StoreCB.PKG_DN
    raw = (callback.data or "")[len(prefix) :]
    if not raw.isdigit():
        await callback.answer()
        return
    try:
        # Moving the top plan up changes nothing; a freeze to manual already done
        # on that tap is persisted (the service decides, the bot only answers).
        moved = await move_package(session, int(raw), direction=direction)
        if moved.status == "at_edge" and not moved.flipped:
            await callback.answer(texts.admin_store_order_at_edge)
            return
        await callback.answer(
            texts.admin_store_order_first_manual if moved.flipped else texts.admin_store_saved
        )
        view = await get_catalog_view(session)
        live = view.category(moved.category_id)
    except StoreAdminError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    message = _tg_message(callback)
    if message and live is not None:
        await _render_section(message, view, live, SECTION_ORDER)


@router.callback_query(F.data.startswith(StoreCB.PKG))
async def store_pkg_detail(  # noqa: PLR0911
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    data = callback.data or ""
    # Skip toggle/edit prefixes that also start with adm:st:p…
    if data.startswith(
        (
            StoreCB.PKG_EN,
            StoreCB.PKG_NS,
            StoreCB.PKG_RN,
            StoreCB.PKG_FD,
            StoreCB.PKG_TITLE,
            StoreCB.PKG_PRICE,
            StoreCB.PKG_ORDER,
            StoreCB.PKG_UP,
            StoreCB.PKG_DN,
            StoreCB.PKGS,
            StoreCB.PKGS_PICK,
        )
    ):
        return
    if not data.startswith(StoreCB.PKG):
        return
    pkg_id, back_cb = parse_pkg_cb(data)
    if pkg_id is None:
        await callback.answer(texts.stale_button, show_alert=True)
        return
    raw = str(pkg_id)
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    tg_id = callback.from_user.id if callback.from_user else None
    owner = _owner(settings, tg_id)
    # Read-only: opening a plan used to call get_draft_package (and commit),
    # which invented a draft every time the owner merely looked.
    view = await get_catalog_view(session)
    pkg = view.package(int(raw))
    if pkg is None:
        await _edit_or_answer(message, "پلن یافت نشد.", store_back_hub_keyboard())
        return
    await _edit_or_answer(
        message,
        _package_detail_text(pkg, view.prices),
        store_package_detail_keyboard(pkg, is_owner=owner, back_cb=back_cb),
    )


async def _toggle_pkg(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    *,
    prefix: str,
    field: str,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(prefix) :]
    parts = raw.split(":")
    if not parts or not parts[0].isdigit():
        await callback.answer()
        return
    pkg_id = int(parts[0])
    origin = parts[1] if len(parts) > 1 else None
    back_cb = {"r": StoreCB.READY, "c": StoreCB.CUSTOM}.get(origin) if origin else None
    message = _tg_message(callback)
    if message is None:
        return
    try:
        await toggle_package_flag(session, pkg_id, field)
    except StoreAdminError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(texts.admin_store_saved)
    view = await get_catalog_view(session)
    live = view.package(pkg_id)
    if live is None:
        await _edit_or_answer(message, "پلن یافت نشد.", store_back_hub_keyboard())
        return
    await _edit_or_answer(
        message,
        _package_detail_text(live, view.prices),
        store_package_detail_keyboard(live, is_owner=True, back_cb=back_cb),
    )


@router.callback_query(F.data.startswith(StoreCB.PKG_EN))
async def store_pkg_en(
    c: CallbackQuery, settings: Settings, db_user: User | None, session: AsyncSession
) -> None:
    await _toggle_pkg(c, settings, db_user, session, prefix=StoreCB.PKG_EN, field="enabled")


@router.callback_query(F.data.startswith(StoreCB.PKG_NS))
async def store_pkg_ns(
    c: CallbackQuery, settings: Settings, db_user: User | None, session: AsyncSession
) -> None:
    await _toggle_pkg(c, settings, db_user, session, prefix=StoreCB.PKG_NS, field="allow_new_sale")


@router.callback_query(F.data.startswith(StoreCB.PKG_RN))
async def store_pkg_rn(
    c: CallbackQuery, settings: Settings, db_user: User | None, session: AsyncSession
) -> None:
    await _toggle_pkg(c, settings, db_user, session, prefix=StoreCB.PKG_RN, field="allow_renew")


@router.callback_query(F.data.startswith(StoreCB.PKG_FD))
async def store_pkg_fd(
    c: CallbackQuery, settings: Settings, db_user: User | None, session: AsyncSession
) -> None:
    await _toggle_pkg(
        c, settings, db_user, session, prefix=StoreCB.PKG_FD, field="forbid_extra_discount"
    )


@router.callback_query(F.data.startswith(StoreCB.PKG_TL))
async def store_pkg_tl(c: CallbackQuery) -> None:
    """Retired: ``has_time_limit`` is derived from the plan's duration.

    Kept so a stale keyboard from an older message explains itself instead of
    silently desyncing the flag from the plan's actual expiry.
    """
    await c.answer(texts.admin_store_tl_derived, show_alert=True)


@router.callback_query(F.data.startswith(StoreCB.PKG_TITLE))
async def store_pkg_edit_title(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.PKG_TITLE) :]
    if not raw.isdigit():
        await callback.answer()
        return
    await state.set_state(AdminStates.store_waiting_pkg_title)
    await state.update_data(store_pkg_id=int(raw))
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _edit_or_answer(
        message, texts.admin_store_prompt_pkg_title, store_cancel_input_keyboard()
    )


@router.callback_query(F.data.startswith(StoreCB.PKG_PRICE))
async def store_pkg_edit_price(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.PKG_PRICE) :]
    if not raw.isdigit():
        await callback.answer()
        return
    try:
        pkg = await get_draft_package(session, int(raw))
        if pkg.kind != StorePackageKind.ready:
            await callback.answer(texts.admin_store_price_ready_only, show_alert=True)
            return
    except StoreAdminError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await state.set_state(AdminStates.store_waiting_pkg_price)
    await state.update_data(store_pkg_id=int(raw))
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _edit_or_answer(
        message, texts.admin_store_prompt_pkg_price, store_cancel_input_keyboard()
    )


@router.message(AdminStates.store_waiting_pkg_title)
@router.message(AdminStates.store_waiting_pkg_price)
async def store_pkg_fsm_input(  # noqa: PLR0911
    message: Message,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    tg_id = message.from_user.id if message.from_user else None
    if not _is_admin(tg_id, settings, db_user):
        return
    try:
        assert_store_owner(settings, tg_id)
    except StoreOwnerRequired:
        await message.answer(texts.admin_store_owner_only, reply_markup=store_back_hub_keyboard())
        await state.clear()
        return
    data = await state.get_data()
    pkg_id = data.get("store_pkg_id")
    if not pkg_id:
        await state.clear()
        await message.answer(texts.admin_store_lost_context, reply_markup=store_back_hub_keyboard())
        return
    cur = await state.get_state()
    text = (message.text or "").strip()
    try:
        if cur == AdminStates.store_waiting_pkg_title.state:
            await set_package_title(session, int(pkg_id), text)
        elif cur == AdminStates.store_waiting_pkg_price.state:
            await set_package_price(session, int(pkg_id), text)
    except PriceJumpNeedsConfirm as ask:
        # 10x or a tenth of the current price: nothing is written yet. Wait for
        # «بله» in FSM state, not inside this handler.
        await session.rollback()
        await state.update_data(store_pkg_price_raw=text)
        await state.set_state(AdminStates.store_waiting_pkg_price_confirm)
        await message.answer(str(ask), reply_markup=store_price_jump_keyboard(int(pkg_id)))
        return
    except StoreAdminError as exc:
        await message.answer(
            str(exc) or texts.admin_store_invalid_input,
            reply_markup=store_cancel_input_keyboard(),
        )
        return
    except ValueError:
        # int()/float() text is English; never show it to a Persian operator.
        await message.answer(
            texts.admin_store_invalid_input,
            reply_markup=store_cancel_input_keyboard(),
        )
        return
    await state.clear()
    view = await get_catalog_view(session)
    pkg = view.package(int(pkg_id))
    if pkg is None:
        await message.answer(texts.admin_store_lost_context, reply_markup=store_back_hub_keyboard())
        return
    await message.answer(
        texts.admin_store_saved + "\n\n" + _package_detail_text(pkg, view.prices),
        reply_markup=store_package_detail_keyboard(pkg, is_owner=True),
    )


@router.callback_query(F.data == StoreCB.PKG_PRICE_OK)
async def store_pkg_price_confirm(  # noqa: PLR0911
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """«بله» on a price 10x or a tenth of the current one: the second request, saved once."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    if await state.get_state() != AdminStates.store_waiting_pkg_price_confirm.state:
        # A second tap, or a button left on an older message: the first one saved.
        await callback.answer(texts.stale_button, show_alert=True)
        return
    data = await state.get_data()
    pkg_id = data.get("store_pkg_id")
    raw = data.get("store_pkg_price_raw")
    if not pkg_id or raw is None:
        await state.clear()
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    try:
        await set_package_price(session, int(pkg_id), str(raw), confirmed=True)
    except StoreAdminError as exc:
        await session.rollback()
        await state.clear()
        await callback.answer(str(exc) or texts.admin_store_invalid_input, show_alert=True)
        return
    await state.clear()
    await callback.answer(texts.admin_store_saved)
    message = _tg_message(callback)
    if message is None:
        return
    view = await get_catalog_view(session)
    pkg = view.package(int(pkg_id))
    if pkg is None:
        await _edit_or_answer(message, texts.admin_store_lost_context, store_back_hub_keyboard())
        return
    await _edit_or_answer(
        message,
        texts.admin_store_saved + "\n\n" + _package_detail_text(pkg, view.prices),
        store_package_detail_keyboard(pkg, is_owner=True),
    )


@router.callback_query(F.data == StoreCB.PREVIEW)
async def store_preview(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    view = await get_catalog_view(session)
    if not view.exists:
        await _edit_or_answer(
            message,
            texts.admin_store_no_catalog,
            store_back_hub_keyboard(),
        )
        return
    try:
        preview = preview_catalog(view)
    except StoreAdminError as exc:
        await _edit_or_answer(message, str(exc), store_back_hub_keyboard())
        return
    body = format_preview_fa(preview)
    if len(body) > 3900:
        body = body[:3900] + "\n…"
    await _edit_or_answer(message, body, store_back_hub_keyboard())


@router.callback_query(F.data == StoreCB.SEED)
async def store_seed(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    message = _tg_message(callback)
    if message is None:
        return
    try:
        result = await seed_store_if_empty(session, settings.pricing_config_path)
        await session.commit()
    except Exception as exc:
        logger.exception("store seed failed")
        await callback.answer(str(exc)[:180], show_alert=True)
        return
    if result is None:
        await callback.answer(texts.admin_store_seed_skipped, show_alert=True)
        await _render_hub(message, session, settings, callback.from_user)
        return
    await callback.answer(texts.admin_store_seed_ok)
    body = texts.admin_store_seeded.format(version=result.version)
    await _edit_or_answer(message, body, store_back_hub_keyboard())


def _combine_fa(mode: str) -> str:
    if mode == "best_of_promo_vs_extra":
        return "بهترین تخفیف (پرومو در برابر معرف)"
    if mode == "sum":
        return "جمع تخفیف‌ها (قدیمی)"
    return mode


async def _show_discount(message: Message, session: AsyncSession, *, owner: bool) -> None:
    view = await get_catalog_view(session)
    if not view.exists or view.version is None:
        await _edit_or_answer(message, texts.admin_store_no_catalog, store_back_hub_keyboard())
        return
    rules = get_sales_rules_view(view.version.sales_rules)
    body = texts.admin_store_disc_title.format(
        combine_fa=_combine_fa(str(rules.get("discount_combine"))),
    )
    kb = store_discount_keyboard(
        is_owner=owner,
        discount_combine=str(rules.get("discount_combine") or "best_of_promo_vs_extra"),
    )
    await _edit_or_answer(message, body, kb)


@router.callback_query(F.data == StoreCB.DISC)
async def store_discount_hub(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    tg_id = callback.from_user.id if callback.from_user else None
    await _show_discount(message, session, owner=_owner(settings, tg_id))


@router.callback_query(F.data == StoreCB.DISC_BEST)
async def store_disc_best(
    callback: CallbackQuery, settings: Settings, db_user: User | None, session: AsyncSession
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    await update_draft_sales_rules(session, discount_combine="best_of_promo_vs_extra")
    await commit_and_activate(session, notes="قاعدهٔ ترکیب تخفیف عوض شد")
    await callback.answer(texts.admin_store_saved)
    message = _tg_message(callback)
    if message:
        await _show_discount(message, session, owner=True)


@router.callback_query(F.data == StoreCB.DISC_SUM)
async def store_disc_sum(
    callback: CallbackQuery, settings: Settings, db_user: User | None, session: AsyncSession
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    await update_draft_sales_rules(session, discount_combine="sum")
    await commit_and_activate(session, notes="قاعدهٔ ترکیب تخفیف عوض شد")
    await callback.answer(texts.admin_store_saved)
    message = _tg_message(callback)
    if message:
        await _show_discount(message, session, owner=True)


async def _promo_body(session: AsyncSession, promos: list[dict[str, Any]], *, hint: str) -> str:
    """The code list: the title, then each code with its rules and its live usage."""
    body = texts.admin_store_promo_title.format(count=len(promos), hint=hint)
    if not promos:
        return body
    blocks = [
        texts.admin_promo_row.format(
            mark="✅" if line.active else "❌", code=escape(line.code), percent=escape(line.percent)
        )
        + "\n"
        + line.rules_text
        + "\n"
        + line.usage
        for line in await promo_lines(session, promos)
    ]
    return body + "\n\n" + "\n\n".join(blocks)


@router.callback_query(F.data == StoreCB.PROMO)
async def store_promo_list(
    callback: CallbackQuery, settings: Settings, db_user: User | None, session: AsyncSession
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    tg_id = callback.from_user.id if callback.from_user else None
    owner = _owner(settings, tg_id)
    view = await get_catalog_view(session)
    if not view.exists or view.version is None:
        await _edit_or_answer(message, texts.admin_store_no_catalog, store_back_hub_keyboard())
        return
    promos = list_promo_codes(view.version.pricing_document)
    hint = texts.admin_store_promo_owner_hint if owner else texts.admin_store_promo_ops_hint
    body = await _promo_body(session, promos, hint=hint)
    await _edit_or_answer(message, body, store_promo_keyboard(promos, is_owner=owner))


@router.callback_query(F.data == StoreCB.PROMO_ADD)
async def store_promo_add(
    callback: CallbackQuery, settings: Settings, db_user: User | None, state: FSMContext
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    await state.set_state(AdminStates.store_waiting_promo)
    await callback.answer()
    message = _tg_message(callback)
    if message:
        await _edit_or_answer(
            message, texts.admin_store_prompt_promo, store_cancel_input_keyboard()
        )


@router.message(AdminStates.store_waiting_promo)
async def store_promo_fsm(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
) -> None:
    """Step 1: «کد درصد». Nothing is saved yet: the wizard asks the usage rules next and
    writes the code once, at the end, so a half-finished wizard leaves no unlimited code live."""
    tg_id = message.from_user.id if message.from_user else None
    if not _is_admin(tg_id, settings, db_user):
        return
    try:
        assert_store_owner(settings, tg_id)
    except StoreOwnerRequired:
        await message.answer(texts.admin_store_owner_only, reply_markup=store_back_hub_keyboard())
        await state.clear()
        return
    parts = (message.text or "").strip().split()
    try:
        code, pct_raw = (parts + ["", ""])[:2]
        cleaned, pct = parse_code_and_percent(code, pct_raw)
    except StoreAdminError as exc:
        await message.answer(
            str(exc) or texts.admin_store_invalid_input,
            reply_markup=store_cancel_input_keyboard(),
        )
        return
    await state.update_data(promo_code=cleaned, promo_percent=pct, promo_rules={})
    await state.set_state(AdminStates.store_promo_first)
    await message.answer(
        texts.admin_promo_ask_first_only, reply_markup=store_promo_first_keyboard()
    )


async def _promo_ask(
    target: Message, state: FSMContext, step: Any, text: str, *, edit: bool
) -> None:
    await state.set_state(step)
    if edit:
        await _edit_or_answer(target, text, store_promo_skip_keyboard())
    else:
        await target.answer(text, reply_markup=store_promo_skip_keyboard())


async def _promo_finish(
    target: Message,
    state: FSMContext,
    session: AsyncSession,
    *,
    edit: bool,
) -> None:
    """The last answer is in: write the code with its rules, publish, show the list."""
    data = await state.get_data()
    try:
        promos = await save_promo_code(
            session,
            code=str(data["promo_code"]),
            percent=float(data["promo_percent"]),
            answers=data.get("promo_rules") or {},
        )
    except (StoreAdminError, KeyError) as exc:
        await state.clear()
        text = str(exc) if isinstance(exc, StoreAdminError) else texts.admin_promo_wizard_lost
        await target.answer(text, reply_markup=store_back_hub_keyboard())
        return
    await state.clear()
    body = await _promo_body(session, promos, hint=texts.admin_store_promo_owner_hint)
    keyboard = store_promo_keyboard(promos, is_owner=True)
    if edit:
        await _edit_or_answer(target, texts.admin_store_saved + "\n\n" + body, keyboard)
    else:
        await target.answer(texts.admin_store_saved + "\n\n" + body, reply_markup=keyboard)


async def _promo_go_on(
    target: Message,
    state: FSMContext,
    session: AsyncSession,
    after: str,
    *,
    edit: bool,
) -> None:
    """Ask the question that follows ``after`` (first, user_limit, total_limit, days)."""
    if after == "first":
        await _promo_ask(
            target,
            state,
            AdminStates.store_promo_user_limit,
            texts.admin_promo_ask_user_limit,
            edit=edit,
        )
    elif after == "user_limit":
        await _promo_ask(
            target,
            state,
            AdminStates.store_promo_total_limit,
            texts.admin_promo_ask_total_limit,
            edit=edit,
        )
    elif after == "total_limit":
        await _promo_ask(
            target, state, AdminStates.store_promo_days, texts.admin_promo_ask_days, edit=edit
        )
    else:
        await _promo_finish(target, state, session, edit=edit)


async def _promo_remember(state: FSMContext, **answers: Any) -> None:
    data = await state.get_data()
    rules = dict(data.get("promo_rules") or {})
    rules.update(answers)
    await state.update_data(promo_rules=rules)


@router.callback_query(F.data.startswith(StoreCB.PROMO_Q))
async def store_promo_wizard_button(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    """«بله» / «نه» on the first question, «رد کردن» on any: the answer for the step on screen."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    action = (callback.data or "")[len(StoreCB.PROMO_Q) :]
    step = await state.get_state()
    message = _tg_message(callback)
    if message is None:
        await callback.answer()
        return
    if step == AdminStates.store_promo_first.state and action in {"y", "n", "s"}:
        await callback.answer()
        yes = action == "y"
        # A first purchase can only be used once, so «uses per customer» is not asked.
        await _promo_remember(state, first_purchase_only=yes, per_user_limit=1 if yes else 0)
        if yes:
            await _promo_ask(
                message,
                state,
                AdminStates.store_promo_total_limit,
                texts.admin_promo_ask_total_limit,
                edit=True,
            )
        else:
            await _promo_go_on(message, state, session, "first", edit=True)
        return
    steps = {
        AdminStates.store_promo_user_limit.state: "user_limit",
        AdminStates.store_promo_total_limit.state: "total_limit",
        AdminStates.store_promo_days.state: "days",
    }
    if action == "s" and step in steps:
        await callback.answer()
        await _promo_go_on(message, state, session, steps[step], edit=True)
        return
    await callback.answer(texts.admin_promo_wizard_lost, show_alert=True)


async def _promo_number(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
    *,
    maximum: int,
) -> int | None:
    """The typed number for the step on screen, or None after saying what is wrong."""
    tg_id = message.from_user.id if message.from_user else None
    if not _is_admin(tg_id, settings, db_user):
        return None
    try:
        assert_store_owner(settings, tg_id)
    except StoreOwnerRequired:
        await message.answer(texts.admin_store_owner_only, reply_markup=store_back_hub_keyboard())
        await state.clear()
        return None
    try:
        return parse_promo_number(message.text or "", maximum=maximum)
    except StoreAdminError as exc:
        await message.answer(str(exc), reply_markup=store_promo_skip_keyboard())
        return None


@router.message(AdminStates.store_promo_first)
async def store_promo_first_typed(
    message: Message, settings: Settings, db_user: User | None
) -> None:
    """The first question wants a button, not text: ask it again, with its buttons."""
    tg_id = message.from_user.id if message.from_user else None
    if not _is_admin(tg_id, settings, db_user):
        return
    await message.answer(
        texts.admin_promo_ask_first_only, reply_markup=store_promo_first_keyboard()
    )


@router.message(AdminStates.store_promo_user_limit)
async def store_promo_user_limit(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    value = await _promo_number(message, state, settings, db_user, maximum=MAX_PROMO_LIMIT)
    if value is None:
        return
    await _promo_remember(state, per_user_limit=value)
    await _promo_go_on(message, state, session, "user_limit", edit=False)


@router.message(AdminStates.store_promo_total_limit)
async def store_promo_total_limit(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    value = await _promo_number(message, state, settings, db_user, maximum=MAX_PROMO_LIMIT)
    if value is None:
        return
    await _promo_remember(state, total_limit=value)
    await _promo_go_on(message, state, session, "total_limit", edit=False)


@router.message(AdminStates.store_promo_days)
async def store_promo_days(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    value = await _promo_number(message, state, settings, db_user, maximum=MAX_PROMO_DAYS)
    if value is None:
        return
    await _promo_remember(state, days=value)
    await _promo_go_on(message, state, session, "days", edit=False)


@router.callback_query(F.data.startswith(StoreCB.PROMO_DEL))
async def store_promo_del_confirm(
    callback: CallbackQuery, settings: Settings, db_user: User | None
) -> None:
    """Preview only — 🗑 sits beside the harmless toggle in the same row."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    code = (callback.data or "")[len(StoreCB.PROMO_DEL) :]
    await callback.answer()
    message = _tg_message(callback)
    if message:
        await _edit_or_answer(
            message,
            texts.admin_store_promo_del_confirm.format(code=escape(code)),
            store_confirm_keyboard(
                confirm_cb=f"{StoreCB.PROMO_DEL_OK}{code}",
                back_to=StoreCB.PROMO,
                back_label="کدهای تخفیف",
            ),
        )


@router.callback_query(F.data.startswith(StoreCB.PROMO_DEL_OK))
async def store_promo_del(
    callback: CallbackQuery, settings: Settings, db_user: User | None, session: AsyncSession
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    code = (callback.data or "")[len(StoreCB.PROMO_DEL_OK) :]
    try:
        promos = await delete_promo_code(session, code)
    except StoreAdminError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(texts.admin_store_saved)
    message = _tg_message(callback)
    if message:
        body = await _promo_body(session, promos, hint=texts.admin_store_promo_owner_hint)
        await _edit_or_answer(message, body, store_promo_keyboard(promos, is_owner=True))


@router.callback_query(F.data.startswith(StoreCB.PROMO_TG))
async def store_promo_toggle(
    callback: CallbackQuery, settings: Settings, db_user: User | None, session: AsyncSession
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    code = (callback.data or "")[len(StoreCB.PROMO_TG) :]
    try:
        promos = await toggle_promo_code(session, code)
    except StoreAdminError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(texts.admin_store_saved)
    message = _tg_message(callback)
    if message:
        body = await _promo_body(session, promos, hint=texts.admin_store_promo_owner_hint)
        await _edit_or_answer(message, body, store_promo_keyboard(promos, is_owner=True))


def _pay_mark(info: PaymentInfo, field: str, shown: str) -> str:
    """The value, tagged «(از .env)» when the env supplies it and it is not empty."""
    body = escape(shown or "—")
    if field in info.from_env and shown:
        body += texts.admin_store_pay_from_env
    return body


async def _pay_screen_text(
    session: AsyncSession, settings: Settings, *, prefix: str = ""
) -> tuple[str, PaymentInfo]:
    info = await get_payment_info(session, settings)
    body = texts.admin_store_pay_title.format(
        card=_pay_mark(info, "card_number", format_card_number(info.card_number)),
        holder=_pay_mark(info, "card_holder_name", info.card_holder_name),
        bank=_pay_mark(info, "bank_name", info.bank_name),
        instructions=_pay_mark(info, "instructions", info.instructions),
        note=(
            texts.admin_store_pay_note_override
            if info.has_override
            else texts.admin_store_pay_note_env
        ),
    )
    return prefix + body, info


async def _show_pay_screen(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    tg_id: int | None,
    *,
    prefix: str = "",
) -> None:
    body, info = await _pay_screen_text(session, settings, prefix=prefix)
    kb = store_pay_keyboard(can_edit=_owner(settings, tg_id), has_override=info.has_override)
    await _edit_or_answer(message, body, kb)


@router.callback_query(F.data == StoreCB.PAY)
async def store_pay_display(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _show_pay_screen(message, session, settings, callback.from_user.id)


@router.callback_query(F.data.startswith(StoreCB.PAY_EDIT))
async def store_pay_edit(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    field = (callback.data or "").removeprefix(StoreCB.PAY_EDIT)
    if field not in PAYMENT_FIELDS:
        await callback.answer(texts.admin_store_invalid_input, show_alert=True)
        return
    await state.set_state(AdminStates.store_waiting_pay_field)
    await state.update_data(pay_field=field)
    await callback.answer()
    message = _tg_message(callback)
    if message is not None:
        await _edit_or_answer(
            message, texts.admin_store_pay_prompts[field], store_pay_cancel_keyboard()
        )


@router.message(AdminStates.store_waiting_pay_field)
async def store_pay_input(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    tg_id = message.from_user.id if message.from_user else None
    if not _is_admin(tg_id, settings, db_user):
        return
    if not _owner(settings, tg_id):
        await message.answer(texts.admin_store_owner_only, reply_markup=store_back_hub_keyboard())
        await state.clear()
        return
    field = (await state.get_data()).get("pay_field")
    if field not in PAYMENT_FIELDS:
        await state.clear()
        await message.answer(texts.admin_store_lost_context, reply_markup=store_back_hub_keyboard())
        return
    try:
        value = validate_payment_field(str(field), message.text or "")
    except PaymentSettingsError as exc:
        await message.answer(
            texts.admin_store_pay_errors.get(exc.code, texts.admin_store_invalid_input),
            reply_markup=store_pay_cancel_keyboard(),
        )
        return
    if field == "card_number":
        # Money-critical: show the number as it will be stored and wait for a tap.
        info = await get_payment_info(session, settings)
        await state.update_data(pay_card=value)
        await message.answer(
            texts.admin_store_pay_card_confirm.format(
                new=format_card_number(value),
                old=escape(format_card_number(info.card_number) or "—"),
            ),
            reply_markup=store_pay_card_confirm_keyboard(),
        )
        return
    admin_id = db_user.id if db_user is not None else None
    try:
        await save_payment_field(session, str(field), value, admin_user_id=admin_id)
        await session.commit()
    except Exception:
        logger.exception("payment field save failed field=%s", field)
        await session.rollback()
        await state.clear()
        await message.answer(texts.admin_action_error, reply_markup=store_back_hub_keyboard())
        return
    await state.clear()
    body, info = await _pay_screen_text(
        session, settings, prefix=texts.admin_store_pay_saved + "\n\n"
    )
    await message.answer(
        body, reply_markup=store_pay_keyboard(can_edit=True, has_override=info.has_override)
    )


@router.callback_query(F.data == StoreCB.PAY_CARD_OK)
async def store_pay_card_save(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    card = (await state.get_data()).get("pay_card")
    message = _tg_message(callback)
    if not card or message is None:
        await state.clear()
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    admin_id = db_user.id if db_user is not None else None
    try:
        await save_payment_field(session, "card_number", str(card), admin_user_id=admin_id)
        await session.commit()
    except Exception:
        logger.exception("payment card save failed")
        await session.rollback()
        await state.clear()
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    await state.clear()
    await callback.answer(texts.admin_store_saved)
    await _show_pay_screen(
        message,
        session,
        settings,
        callback.from_user.id,
        prefix=texts.admin_store_pay_saved + "\n\n",
    )


@router.callback_query(F.data == StoreCB.PAY_CLEAR)
async def store_pay_clear_confirm(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """Preview only. Nothing is written until PAY_CLEAR_OK."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    current, _info = await _pay_screen_text(session, settings)
    await _edit_or_answer(
        message,
        texts.admin_store_pay_clear_confirm.format(current=current),
        store_pay_clear_confirm_keyboard(),
    )


@router.callback_query(F.data == StoreCB.PAY_CLEAR_OK)
async def store_pay_clear(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    admin_id = db_user.id if db_user is not None else None
    try:
        await clear_payment_to_env(session, admin_user_id=admin_id)
        await session.commit()
    except Exception:
        logger.exception("payment settings clear failed")
        await session.rollback()
        await callback.answer(texts.admin_action_error, show_alert=True)
        return
    await state.clear()
    await callback.answer(texts.admin_store_pay_cleared)
    message = _tg_message(callback)
    if message is not None:
        await _show_pay_screen(
            message,
            session,
            settings,
            callback.from_user.id,
            prefix=texts.admin_store_pay_cleared + "\n\n",
        )


@router.callback_query(F.data.startswith("adm:st:st") & ~F.data.startswith(StoreCB.STRAT))
async def store_steps_retired(callback: CallbackQuery) -> None:
    """Retired: purchase never reads ``purchase_steps`` (same pattern as PKG_TL).

    Stale keyboards may still fire any of these; keep the names referenced so
    the admin-navigation structural test still sees a handler for each constant.
    """
    _ = (
        StoreCB.STEPS,
        StoreCB.STEP_UP,
        StoreCB.STEP_DN,
        StoreCB.STEP_TG,
        StoreCB.STEP_RM,
        StoreCB.STEP_RM_OK,
        StoreCB.STEP_ADD,
        StoreCB.STEP_ADDB,
    )
    await callback.answer(texts.admin_store_steps_retired, show_alert=True)


@router.callback_query(F.data == StoreCB.HIST)
async def store_changes(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """The change log: what was done to the shop, newest first."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    tg_id = callback.from_user.id if callback.from_user else None
    entries = await list_store_changes(session)
    if not entries:
        await _edit_or_answer(message, texts.admin_store_changes_empty, store_back_hub_keyboard())
        return
    await _edit_or_answer(
        message,
        change_log_text(entries, quote=escape),
        store_changes_keyboard(entries, is_owner=_owner(settings, tg_id)),
    )


@router.callback_query(F.data.startswith(StoreCB.HIST_REST))
async def store_change_undo_ask(
    callback: CallbackQuery, settings: Settings, db_user: User | None, session: AsyncSession
) -> None:
    """Ask before undoing — and say what else goes with it.

    Undo restores a whole snapshot, so reverting an older change also reverts
    every change made after it. A button that just said «برگشت» would be lying
    about that, which is the one thing this screen must not do.
    """
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    data = callback.data or ""
    if data.startswith(StoreCB.HIST_OK):
        return
    raw = data[len(StoreCB.HIST_REST) :]
    if not raw.isdigit():
        await callback.answer()
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return

    entries = await list_store_changes(session)
    entry = next((e for e in entries if e.version_id == int(raw)), None)
    if entry is None:
        await _edit_or_answer(message, texts.admin_store_change_gone, store_back_hub_keyboard())
        return

    await _edit_or_answer(
        message,
        undo_confirm_text(entry, quote=escape),
        store_restore_confirm_keyboard(entry.version_id),
    )


@router.callback_query(F.data.startswith(StoreCB.HIST_OK))
async def store_history_restore_ok(
    callback: CallbackQuery, settings: Settings, db_user: User | None, session: AsyncSession
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.HIST_OK) :]
    if not raw.isdigit():
        await callback.answer()
        return
    try:
        # The undo is itself an entry, so it can be undone in turn and the log
        # keeps matching what the catalog has actually been through.
        description = await apply_undo(session, int(raw))
    except StoreAdminError as exc:
        await session.rollback()
        await callback.answer(str(exc)[:190], show_alert=True)
        return
    await callback.answer(texts.admin_store_undone)
    message = _tg_message(callback)
    if message:
        await _edit_or_answer(
            message,
            undone_text(description, quote=escape),
            store_back_hub_keyboard(),
        )


# --------------------------------------------------------------------------- #
# Creating and deleting catalog rows
#
# These operations simply did not exist: a seeded catalog holds only
# ``custom_template`` packages, «پلن‌های آماده» showed ``(0)`` forever, and no
# price a customer pays could be changed from inside Telegram.
# --------------------------------------------------------------------------- #


def _fmt_price(value: int | None) -> str:
    if value is None:
        return "قیمت‌گذاری پویا"
    return f"{format_toman(value, persian_digits=True)} تومان"


async def _prompt(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    body: str,
    next_state: State,
    data: dict[str, Any],
) -> None:
    """Arm one text prompt, always with a cancel button."""
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await state.update_data(**data)
    await state.set_state(next_state)
    await _edit_or_answer(message, body, store_cancel_input_keyboard())


def _parse_number(raw: str) -> int | None:
    """One positive integer (``services.store_plan_edit.parse_positive``)."""
    return parse_positive(raw)


async def _ask_duration(message: Message, state: FSMContext, *, kind: str, size: int) -> None:
    # The size has to be persisted here, not just rendered into the prompt: the
    # duration step runs in a *separate* handler and can only see FSM state.
    # Without this the wizard answered «حالت گم شد» on every day the owner tapped.
    await state.update_data(new_pkg_size_pending=size, new_pkg_kind=kind)
    tmpl = (
        texts.admin_store_pick_duration_unlimited
        if kind == "unlimited"
        else texts.admin_store_pick_duration_volume
    )
    await state.set_state(AdminStates.store_waiting_new_pkg_duration)
    await _edit_or_answer(
        message,
        tmpl.format(size=to_persian_digits(size)),
        store_new_pkg_duration_keyboard(kind=kind),
    )


async def _after_duration(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    *,
    kind: str,
    size: int,
    duration: int | None,
) -> None:
    """Creating asks for a price next; editing an existing plan just saves."""
    data = await state.get_data()
    editing_id = data.get("dims_pkg_id")
    if editing_id:
        try:
            await set_package_dims(
                session, int(editing_id), kind=kind, size=size, duration=duration
            )
        except StoreAdminError as exc:
            await session.rollback()
            await state.clear()
            await _edit_or_answer(message, str(exc), store_back_hub_keyboard())
            return
        await state.clear()
        view = await get_catalog_view(session)
        pkg = view.package(int(editing_id))
        if pkg is None:
            await _edit_or_answer(message, texts.admin_store_saved, store_back_hub_keyboard())
            return
        await _edit_or_answer(
            message,
            _with_badge(await _badge(session), _package_detail_text(pkg, view.prices)),
            store_package_detail_keyboard(pkg, is_owner=True),
        )
        return
    title = derive_plan_title(kind=kind, size=size, duration=duration)
    await state.update_data(
        new_pkg_size=size,
        new_pkg_duration=duration,
        new_pkg_title=title,
        new_pkg_size_pending=None,
    )
    if kind == "volume":
        await _ask_users(message, state)
        return
    await state.set_state(AdminStates.store_waiting_new_pkg_price)
    await _edit_or_answer(
        message,
        texts.admin_store_prompt_new_pkg_price.format(title=escape(title)),
        store_cancel_input_keyboard(),
    )


async def _ask_users(message: Message, state: FSMContext) -> None:
    """Volume wizard step after duration: device limit (0 = نامحدود)."""
    await state.set_state(AdminStates.store_waiting_new_pkg_users)
    await _edit_or_answer(
        message,
        texts.admin_store_pick_users_step,
        store_new_pkg_users_keyboard(),
    )


async def _after_users(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    *,
    hwid_limit: int,
) -> None:
    """Creating continues to price; editing a plan/category commits immediately."""
    data = await state.get_data()
    editing_pkg = data.get("hwid_pkg_id")
    editing_cat = data.get("hwid_cat_id")
    if editing_pkg is not None:
        try:
            await save_package_hwid(session, int(editing_pkg), hwid_limit=hwid_limit)
        except StoreAdminError as exc:
            await session.rollback()
            await state.clear()
            await _edit_or_answer(message, str(exc), store_back_hub_keyboard())
            return
        await state.clear()
        view = await get_catalog_view(session)
        pkg = view.package(int(editing_pkg))
        if pkg is None:
            await _edit_or_answer(message, texts.admin_store_saved, store_back_hub_keyboard())
            return
        await _edit_or_answer(
            message,
            _with_badge(await _badge(session), _package_detail_text(pkg, view.prices)),
            store_package_detail_keyboard(pkg, is_owner=True),
        )
        return
    if editing_cat is not None:
        try:
            await save_hwid_limit(session, int(editing_cat), hwid_limit=hwid_limit)
        except StoreAdminError as exc:
            await session.rollback()
            await state.clear()
            await _edit_or_answer(message, str(exc), store_back_hub_keyboard())
            return
        await state.clear()
        view = await get_catalog_view(session)
        cat = view.category(int(editing_cat))
        if cat is None:
            await _edit_or_answer(message, texts.admin_store_saved, store_back_hub_keyboard())
            return
        await _render_section(message, view, cat, SECTION_SALE, notice=texts.admin_store_saved)
        return
    title = str(data.get("new_pkg_title") or "")
    await state.update_data(new_pkg_hwid=int(hwid_limit))
    await state.set_state(AdminStates.store_waiting_new_pkg_price)
    await _edit_or_answer(
        message,
        texts.admin_store_prompt_new_pkg_price.format(title=escape(title)),
        store_cancel_input_keyboard(),
    )


@router.callback_query(F.data.startswith(StoreCB.PKG_ADD))
async def store_pkg_add_start(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """Step 1 of 3: pick a size by tapping."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.PKG_ADD) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    view = await get_catalog_view(session)
    cat = view.category(int(raw))
    if cat is None:
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    kind = category_kind(cat) or "volume"
    tmpl = (
        texts.admin_store_pick_size_unlimited
        if kind == "unlimited"
        else texts.admin_store_pick_size_volume
    )
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await state.update_data(new_pkg_category_id=int(raw), new_pkg_kind=kind)
    await state.set_state(AdminStates.store_waiting_new_pkg_size)
    await _edit_or_answer(
        message,
        tmpl.format(category=escape(cat.title_fa)),
        store_new_pkg_size_keyboard(kind=kind),
    )


@router.callback_query(F.data.startswith(StoreCB.PKG_SIZE_OTHER))
async def store_pkg_size_other(
    callback: CallbackQuery, settings: Settings, db_user: User | None, state: FSMContext
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    data = await state.get_data()
    kind = str(data.get("new_pkg_kind") or "volume")
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    prompt = (
        texts.admin_store_prompt_size_unlimited
        if kind == "unlimited"
        else texts.admin_store_prompt_size_volume
    )
    await state.set_state(AdminStates.store_waiting_new_pkg_size)
    await _edit_or_answer(message, prompt, store_cancel_input_keyboard())


@router.callback_query(F.data.startswith(StoreCB.PKG_SIZE))
async def store_pkg_size_pick(
    callback: CallbackQuery, settings: Settings, db_user: User | None, state: FSMContext
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(StoreCB.PKG_SIZE) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    data = await state.get_data()
    kind = str(data.get("new_pkg_kind") or "volume")
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _ask_duration(message, state, kind=kind, size=int(raw))


@router.message(AdminStates.store_waiting_new_pkg_size)
async def store_pkg_size_typed(
    message: Message, state: FSMContext, settings: Settings, db_user: User | None
) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None, settings, db_user):
        await state.clear()
        return
    data = await state.get_data()
    kind = str(data.get("new_pkg_kind") or "volume")
    try:
        size = read_plan_number(
            dim_keys(kind)[0], message.text or "", example="15" if kind == "volume" else "3"
        )
    except StoreAdminError as exc:
        await message.answer(str(exc), reply_markup=store_cancel_input_keyboard())
        return
    await _ask_duration(message, state, kind=kind, size=size)


@router.callback_query(F.data.startswith(StoreCB.PKG_DUR_OTHER))
async def store_pkg_duration_other(
    callback: CallbackQuery, settings: Settings, db_user: User | None, state: FSMContext
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    data = await state.get_data()
    kind = str(data.get("new_pkg_kind") or "volume")
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    prompt = (
        texts.admin_store_prompt_duration_unlimited
        if kind == "unlimited"
        else texts.admin_store_prompt_duration_volume
    )
    await state.set_state(AdminStates.store_waiting_new_pkg_duration)
    await _edit_or_answer(message, prompt, store_cancel_input_keyboard())


@router.callback_query(F.data.startswith(StoreCB.PKG_DUR))
async def store_pkg_duration_pick(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(StoreCB.PKG_DUR) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    data = await state.get_data()
    kind = str(data.get("new_pkg_kind") or "volume")
    # Only the in-flight key. `new_pkg_size` is the *confirmed* size of a wizard
    # that already finished, and falling back to it let an abandoned run donate
    # its size to the next plan.
    size = data.get("new_pkg_size_pending")
    if size is None:
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    # «بدون انقضا» is encoded as 0 so it needs no separate callback.
    duration = int(raw) or None
    await _after_duration(message, state, session, kind=kind, size=int(size), duration=duration)


@router.message(AdminStates.store_waiting_new_pkg_duration)
async def store_pkg_duration_typed(
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
    kind = str(data.get("new_pkg_kind") or "volume")
    # Only the in-flight key. `new_pkg_size` is the *confirmed* size of a wizard
    # that already finished, and falling back to it let an abandoned run donate
    # its size to the next plan.
    size = data.get("new_pkg_size_pending")
    example = "45" if kind == "volume" else "2"
    if size is None:
        await message.answer(
            texts.admin_store_number_invalid.format(example=example),
            reply_markup=store_cancel_input_keyboard(),
        )
        return
    try:
        duration = read_plan_number(dim_keys(kind)[1], message.text or "", example=example)
    except StoreAdminError as exc:
        await message.answer(str(exc), reply_markup=store_cancel_input_keyboard())
        return
    await _after_duration(message, state, session, kind=kind, size=int(size), duration=duration)


@router.callback_query(F.data.startswith(StoreCB.PKG_USERS_OTHER))
async def store_pkg_users_other(
    callback: CallbackQuery, settings: Settings, db_user: User | None, state: FSMContext
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await state.set_state(AdminStates.store_waiting_new_pkg_users)
    await _edit_or_answer(message, texts.admin_store_pick_users, store_cancel_input_keyboard())


@router.callback_query(F.data.startswith(StoreCB.PKG_USERS))
async def store_pkg_users_pick(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(StoreCB.PKG_USERS) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _after_users(message, state, session, hwid_limit=int(raw))


@router.message(AdminStates.store_waiting_new_pkg_users)
async def store_pkg_users_typed(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None, settings, db_user):
        await state.clear()
        return
    # Allow 0 (نامحدود): ``read_hwid`` is the reader that does, unlike a size's.
    try:
        value = read_hwid(message.text or "")
    except StoreAdminError as exc:
        await message.answer(str(exc), reply_markup=store_cancel_input_keyboard())
        return
    await _after_users(message, state, session, hwid_limit=value)


@router.message(AdminStates.store_waiting_new_pkg_price)
async def store_pkg_add_price(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    """Last question of the wizard. Nothing is written here — see the confirm."""
    if not _is_admin(message.from_user.id if message.from_user else None, settings, db_user):
        await state.clear()
        return
    data = await state.get_data()
    category_id = data.get("new_pkg_category_id")
    title = data.get("new_pkg_title")
    size = data.get("new_pkg_size")
    if not category_id or not title or not size:
        await state.clear()
        await message.answer(texts.admin_store_lost_context, reply_markup=store_back_hub_keyboard())
        return
    try:
        price = read_price(message.text or "")
    except StoreAdminError as exc:
        await message.answer(str(exc), reply_markup=store_cancel_input_keyboard())
        return
    # Nothing is written until the owner confirms. Holding a half-made plan in a
    # draft would be worse than the old flow: with every edit now publishing on
    # confirm, an unconfirmed plan left in the draft would ride along with the
    # next unrelated edit and go live on its own.
    await state.update_data(new_pkg_price=price)
    await state.set_state(AdminStates.store_waiting_new_pkg_confirm)
    await message.answer(
        await _pending_plan_preview(session, await state.get_data()),
        reply_markup=store_new_pkg_confirm_keyboard(),
    )


@router.message(AdminStates.store_waiting_new_pkg_title)
async def store_pkg_add_rename(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    """Optional rename of the pending plan, before it is written."""
    if not _is_admin(message.from_user.id if message.from_user else None, settings, db_user):
        await state.clear()
        return
    data = await state.get_data()
    if not data.get("new_pkg_price"):
        await state.clear()
        await message.answer(texts.admin_store_lost_context, reply_markup=store_back_hub_keyboard())
        return
    try:
        title = read_plan_title(message.text or "")
    except StoreAdminError as exc:
        await message.answer(str(exc), reply_markup=store_cancel_input_keyboard())
        return
    # Renaming edits the pending plan, it does not end the wizard — back to the
    # same confirm screen, still not live.
    await state.update_data(new_pkg_title=title)
    await state.set_state(AdminStates.store_waiting_new_pkg_confirm)
    await message.answer(
        await _pending_plan_preview(session, await state.get_data()),
        reply_markup=store_new_pkg_confirm_keyboard(),
    )


async def _pending_plan_preview(session: AsyncSession, data: dict[str, Any]) -> str:
    """The preview, plus a warning when the group's shelf has no tab for this duration."""
    body = _pending_plan_preview_text(data)
    cat = (await get_catalog_view(session)).category(int(data.get("new_pkg_category_id") or 0))
    warning = no_tab_warning(
        cat,
        kind=str(data.get("new_pkg_kind") or "volume"),
        duration=data.get("new_pkg_duration"),
    )
    return body if warning is None else f"{body}\n\n{warning}"


def _pending_plan_preview_text(data: dict[str, Any]) -> str:
    """The plan as the customer would see it, rendered from the wizard's own
    state because nothing has been written yet."""
    kind = str(data.get("new_pkg_kind") or "volume")
    size = int(data.get("new_pkg_size") or 0)
    duration = data.get("new_pkg_duration")
    price = int(data.get("new_pkg_price") or 0)
    title = str(data.get("new_pkg_title") or "")

    if kind == "unlimited":
        dims = (
            f"کاربر همزمان: {to_persian_digits(size)}\n"
            f"مدت: {to_persian_digits(duration or 0)} ماه"
        )
    else:
        hwid = data.get("new_pkg_hwid")
        try:
            hwid_n = 0 if hwid is None else max(0, int(hwid))
        except (TypeError, ValueError):
            hwid_n = 0
        users_line = f"کاربر همزمان: {hwid_limit_label_fa(hwid_n)}"
        if duration is None:
            dims = f"حجم: {to_persian_digits(size)} گیگ\nمدت: بدون انقضا\n{users_line}"
        else:
            dims = (
                f"حجم: {to_persian_digits(size)} گیگ\n"
                f"مدت: {to_persian_digits(duration)} روز\n"
                f"{users_line}"
            )

    return (
        f"{texts.admin_store_new_pkg_confirm_intro}\n\n"
        f"{escape(title)}\n{dims}\nقیمت: {_fmt_price(price)}"
    )


@router.callback_query(F.data == StoreCB.PKG_KEEP_TITLE)
async def store_pkg_add_rename_start(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    """«✏️ تغییر عنوان» — ask for a title, then come back to the confirm screen."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    data = await state.get_data()
    if not data.get("new_pkg_price"):
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    await state.set_state(AdminStates.store_waiting_new_pkg_title)
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _edit_or_answer(
        message, texts.admin_store_prompt_new_pkg_rename, store_cancel_input_keyboard()
    )


@router.callback_query(F.data == StoreCB.PKG_ACTIVATE)
async def store_pkg_activate(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """The second confirmation: the plan is written **and** goes live here.

    This is the whole of the old «بساز، بعد یادت باشه منتشر کنی» in one tap.
    """
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    data = await state.get_data()
    category_id = data.get("new_pkg_category_id")
    size = data.get("new_pkg_size")
    price = data.get("new_pkg_price")
    title = data.get("new_pkg_title")
    if not category_id or not size or not price or not title:
        await state.clear()
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return

    kind = str(data.get("new_pkg_kind") or "volume")
    try:
        await create_package(
            session,
            category_id=int(category_id),
            kind=kind,
            size=int(size),
            duration=data.get("new_pkg_duration"),
            hwid=data.get("new_pkg_hwid"),
            price=int(price),
            title=str(title),
        )
    except StoreAdminError as exc:
        await session.rollback()
        await state.clear()
        await callback.answer(str(exc)[:190], show_alert=True)
        message = _tg_message(callback)
        if message is not None:
            await _edit_or_answer(message, str(exc), store_back_hub_keyboard())
        return
    await state.clear()
    await callback.answer(texts.admin_store_plan_live)
    message = _tg_message(callback)
    if message is None:
        return
    await _finish_new_plan(message, session, int(category_id))


@router.callback_query(F.data == StoreCB.PKG_DISCARD)
async def store_pkg_discard_new(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """Drop the pending plan. Nothing was written, so there is nothing to undo."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    await state.clear()
    await callback.answer(texts.admin_store_plan_discarded)
    message = _tg_message(callback)
    if message is None:
        return
    await _render_hub(message, session, settings, callback.from_user)


async def _finish_new_plan(
    message: Message, session: AsyncSession, category_id: int | None
) -> None:
    view = await get_catalog_view(session)
    cat = view.category(category_id) if category_id else None
    if cat is None:
        await _edit_or_answer(message, texts.admin_store_saved, store_back_hub_keyboard())
        return
    await _render_category(message, view, cat, owner=True)


@router.callback_query(F.data.startswith(StoreCB.CAT_SUB_ADD))
async def store_sub_cat_start(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.CAT_SUB_ADD) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    view = await get_catalog_view(session)
    parent = view.category(int(raw))
    if parent is None:
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    await _prompt(
        callback,
        state,
        body=texts.admin_store_prompt_sub_cat.format(parent=escape(parent.title_fa)),
        next_state=AdminStates.store_waiting_sub_cat_title,
        data={"sub_cat_parent_id": int(raw)},
    )


@router.message(AdminStates.store_waiting_sub_cat_title)
async def store_sub_cat_title(
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
    parent_id = data.get("sub_cat_parent_id")
    if not parent_id:
        await state.clear()
        await message.answer(texts.admin_store_lost_context, reply_markup=store_back_hub_keyboard())
        return
    try:
        read_group_title(message.text or "")
    except StoreAdminError as exc:
        await message.answer(str(exc), reply_markup=store_cancel_input_keyboard())
        return
    try:
        # The service reads the *current* parent: FSM may still hold the id from a
        # keyboard rendered before the last publish, and writing that stale id as
        # parent_id orphans the child under a row that no longer exists.
        cat, parent = await create_subgroup(session, int(parent_id), message.text or "")
    except StoreAdminError as exc:
        await session.rollback()
        await message.answer(str(exc), reply_markup=store_back_hub_keyboard())
        await state.clear()
        return
    await state.clear()
    notice = texts.admin_store_sub_cat_created.format(
        title=escape(cat.title_fa),
        parent=escape(parent.title_fa),
    )
    fresh = await get_catalog_view(session)
    new_cat = fresh.category(cat.id)
    if new_cat is None:
        await message.answer(notice, reply_markup=store_back_hub_keyboard())
        return
    await _render_category(message, fresh, new_cat, owner=True, notice=notice)


def _parse_cat_duration_cb(data: str, prefix: str) -> tuple[int, int | None] | None:
    """``prefix{cat_id}:{encoded}`` → ``(cat_id, duration_days)``."""
    raw = (data or "")[len(prefix) :]
    if ":" not in raw:
        return None
    cat_s, enc = raw.split(":", 1)
    if not cat_s.isdigit():
        return None
    try:
        return int(cat_s), parse_duration_cb(enc)
    except ValueError:
        return None


async def _render_durations(
    message: Message, session: AsyncSession, category_id: int, *, notice: str | None = None
) -> None:
    cat = await get_draft_category(session, category_id)
    body = texts.admin_store_durs_title.format(title=escape(cat.title_fa))
    from services.store_pricing import category_durations

    durs = category_durations(cat)
    if not durs:
        body = f"{body}\n\n{texts.admin_store_durs_empty}"
    else:
        lines = [f"• {duration_label_fa(d)}" for d in durs]
        body = f"{body}\n\n" + "\n".join(lines)
    if notice:
        body = f"✅ {notice}\n\n{body}"
    await _edit_or_answer(message, body, store_category_durations_keyboard(cat))


@router.callback_query(F.data.startswith(StoreCB.CAT_DURS))
async def store_cat_durs(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    raw = (callback.data or "")[len(StoreCB.CAT_DURS) :]
    if not raw.isdigit():
        await callback.answer()
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _render_durations(message, session, int(raw))


@router.callback_query(F.data.startswith(StoreCB.CAT_DUR_ADD))
async def store_cat_dur_add(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.CAT_DUR_ADD) :]
    if not raw.isdigit():
        await callback.answer()
        return
    await state.set_state(AdminStates.store_waiting_cat_duration)
    await state.update_data(store_cat_id=int(raw))
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _edit_or_answer(
        message, texts.admin_store_dur_prompt, store_duration_add_keyboard(int(raw))
    )


@router.callback_query(F.data.startswith(StoreCB.CAT_DUR_NONE))
async def store_cat_dur_none(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.CAT_DUR_NONE) :]
    if not raw.isdigit():
        await callback.answer()
        return
    message = _tg_message(callback)
    if message is None:
        return
    try:
        await add_duration(session, int(raw), months=None)
        await state.clear()
    except StoreAdminError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(texts.admin_store_saved)
    await _render_durations(message, session, int(raw), notice=texts.admin_store_saved)


@router.message(AdminStates.store_waiting_cat_duration)
async def store_cat_dur_months(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    tg_id = message.from_user.id if message.from_user else None
    if not _is_admin(tg_id, settings, db_user):
        return
    try:
        assert_store_owner(settings, tg_id)
    except StoreOwnerRequired:
        await message.answer(texts.admin_store_owner_only, reply_markup=store_back_hub_keyboard())
        await state.clear()
        return
    data = await state.get_data()
    cat_id = data.get("store_cat_id")
    if not cat_id:
        await state.clear()
        await message.answer(texts.admin_store_lost_context, reply_markup=store_back_hub_keyboard())
        return
    text = (message.text or "").strip()
    months = parse_positive_int(text)
    if months is None:
        await message.answer(
            texts.admin_store_dur_invalid,
            reply_markup=store_duration_add_keyboard(int(cat_id)),
        )
        return
    try:
        cat = await add_duration(session, int(cat_id), months=months)
    except StoreAdminError as exc:
        await message.answer(
            str(exc) or texts.admin_store_dur_invalid,
            reply_markup=store_duration_add_keyboard(int(cat_id)),
        )
        return
    await state.clear()
    body = texts.admin_store_durs_title.format(title=escape(cat.title_fa))
    from services.store_pricing import category_durations

    listed = [f"• {duration_label_fa(d)}" for d in category_durations(cat)]
    await message.answer(
        "✅ " + texts.admin_store_saved + "\n\n" + body + "\n\n" + "\n".join(listed),
        reply_markup=store_category_durations_keyboard(cat),
    )


@router.callback_query(F.data.startswith(StoreCB.CAT_DUR_DEL_OK))
async def store_cat_dur_del_ok(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    parsed = _parse_cat_duration_cb(callback.data or "", StoreCB.CAT_DUR_DEL_OK)
    if parsed is None:
        await callback.answer()
        return
    cat_id, duration_days = parsed
    message = _tg_message(callback)
    if message is None:
        return
    try:
        await remove_duration(session, cat_id, duration_days=duration_days)
    except StoreAdminError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(texts.admin_store_saved)
    await _render_durations(message, session, cat_id, notice=texts.admin_store_saved)


@router.callback_query(F.data.startswith(StoreCB.CAT_DUR_DEL))
async def store_cat_dur_del_confirm(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    parsed = _parse_cat_duration_cb(callback.data or "", StoreCB.CAT_DUR_DEL)
    if parsed is None:
        await callback.answer()
        return
    cat_id, duration_days = parsed
    message = _tg_message(callback)
    if message is None:
        return
    try:
        cat = await get_draft_category(session, cat_id)
        count = await count_prices_for_duration(session, cat_id, duration_days)
    except StoreAdminError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()
    label = duration_label_fa(duration_days)
    await _edit_or_answer(
        message,
        texts.admin_store_dur_del_confirm.format(
            label=label,
            title=escape(cat.title_fa),
            count=count,
        ),
        store_duration_delete_confirm_keyboard(cat_id, duration_days),
    )


@router.callback_query(F.data.startswith(StoreCB.CAT_TIERS))
async def store_cat_toggle_tiers(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.CAT_TIERS) :]
    if not raw.isdigit():
        await callback.answer()
        return
    message = _tg_message(callback)
    if message is None:
        return
    try:
        await toggle_tiers(session, int(raw))
    except StoreAdminError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(texts.admin_store_saved)
    view = await get_catalog_view(session)
    live = view.category(int(raw))
    if live is None:
        await _edit_or_answer(message, "گروه محصول یافت نشد.", store_back_hub_keyboard())
        return
    await _render_section(message, view, live, SECTION_SALE, notice=texts.admin_store_saved)


@router.callback_query(F.data.startswith(StoreCB.CAT_TIER_GROUPS))
async def store_cat_edit_tier_groups(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.CAT_TIER_GROUPS) :]
    await _open_group_picker(callback, session, settings, state, PICK_TIER, raw)


@router.callback_query(F.data.startswith(StoreCB.CAT_DEL_OK))
async def store_cat_delete_apply(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.CAT_DEL_OK) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    try:
        title = await remove_group(session, int(raw))
    except StoreAdminError as exc:
        await session.rollback()
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(texts.admin_store_saved)
    message = _tg_message(callback)
    if message is None:
        return
    await _edit_or_answer(
        message,
        texts.admin_store_cat_deleted.format(title=escape(title)),
        store_back_hub_keyboard(),
    )


@router.callback_query(F.data.startswith(StoreCB.CAT_DEL))
async def store_cat_delete_confirm(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    """Preview step — changes nothing until the second tap."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.CAT_DEL) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    view = await get_catalog_view(session)
    cat = view.category(int(raw))
    if cat is None:
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _edit_or_answer(
        message,
        group_delete_confirm(view, cat, quote=escape),
        store_category_delete_confirm_keyboard(cat.id),
    )


@router.callback_query(F.data.startswith(StoreCB.PKG_DEL_OK))
async def store_pkg_delete_apply(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.PKG_DEL_OK) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    try:
        title, category_id = await remove_package(session, int(raw))
    except StoreAdminError as exc:
        await session.rollback()
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(texts.admin_store_saved)
    message = _tg_message(callback)
    if message is None:
        return
    notice = texts.admin_store_pkg_deleted.format(title=escape(title))
    fresh = await get_catalog_view(session)
    cat = fresh.category(int(category_id)) if category_id else None
    if cat is None:
        await _edit_or_answer(message, notice, store_back_hub_keyboard())
        return
    await _render_category(message, fresh, cat, owner=True, notice=notice)


@router.callback_query(F.data.startswith(StoreCB.PKG_DEL))
async def store_pkg_delete_confirm(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.PKG_DEL) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    view = await get_catalog_view(session)
    pkg = view.package(int(raw))
    if pkg is None:
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await _edit_or_answer(
        message,
        package_delete_confirm(pkg, list(view.prices), quote=escape),
        store_package_delete_confirm_keyboard(pkg.id),
    )


@router.callback_query(F.data.startswith(StoreCB.PKG_DIMS))
async def store_pkg_dims_start(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """Edit an existing plan's size and duration.

    Without this the only way to change 50 GB into 100 GB was to delete the
    plan and build it again.
    """
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.PKG_DIMS) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    view = await get_catalog_view(session)
    pkg = view.package(int(raw))
    if pkg is None:
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    cat = view.category(pkg.category_id)
    kind = category_kind(cat) or "volume"
    tmpl = (
        texts.admin_store_pick_size_unlimited
        if kind == "unlimited"
        else texts.admin_store_pick_size_volume
    )
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    # Same two taps as creating a plan; ``dims_pkg_id`` makes the shared flow
    # save into this package instead of asking for a price.
    await state.update_data(
        dims_pkg_id=int(raw), new_pkg_kind=kind, new_pkg_category_id=pkg.category_id
    )
    await state.set_state(AdminStates.store_waiting_new_pkg_size)
    await _edit_or_answer(
        message,
        tmpl.format(category=escape(pkg.title_fa)),
        store_new_pkg_size_keyboard(kind=kind),
    )


@router.callback_query(F.data.startswith(StoreCB.PKG_HWID))
async def store_pkg_hwid_start(
    callback: CallbackQuery,
    settings: Settings,
    db_user: User | None,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """Edit ``options.hwid_limit`` on a ready volume plan."""
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    if await _deny_if_not_owner_write(callback, settings):
        return
    raw = (callback.data or "")[len(StoreCB.PKG_HWID) :]
    if not raw.isdigit():
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    view = await get_catalog_view(session)
    pkg = view.package(int(raw))
    if pkg is None:
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    opts = dict(pkg.options or {})
    if opts.get("volume_gb") is None and opts.get("data_limit_gb") is None:
        await callback.answer(texts.admin_store_lost_context, show_alert=True)
        return
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await state.update_data(hwid_pkg_id=int(raw), hwid_cat_id=None)
    await state.set_state(AdminStates.store_waiting_new_pkg_users)
    await _edit_or_answer(message, texts.admin_store_pick_users, store_new_pkg_users_keyboard())

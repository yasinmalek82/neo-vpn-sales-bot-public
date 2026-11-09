"""Purchase flow: pricing, named service, wallet apply, one-open-order resume."""

from __future__ import annotations

import contextlib
import html
import logging
from dataclasses import dataclass
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from api_client.factory import get_pasarguard_client
from bot.config import Settings
from bot.delivery import deliver_provisioned_service_card
from bot.handlers.payment import begin_payment_wait
from bot.handlers.sale_gate import refuse_if_panel_down
from bot.keyboards.customer import provision_failed_keyboard, receipt_waiting_keyboard
from bot.keyboards.main_menu import MenuCallback
from bot.keyboards.purchase import (
    PurchaseCB,
    categories_keyboard,
    confirm_keyboard,
    options_keyboard,
    parse_duration_cb,
    plan_type_keyboard,
    promo_keyboard,
    ready_plans_keyboard,
    renew_custom_keyboard,
    renew_or_new_keyboard,
    renew_plan_off_keyboard,
)
from bot.states import PurchaseStates
from bot.texts import texts
from database.base import end_read_transaction
from database.models import OrderStatus, PasarGuardAccount, User
from database.models.order import RENEW_MODE_RESET
from database.models.store import StoreCategory
from services.accounts import get_live_account, service_list_label
from services.bank_invoices import reserve_bank_invoice
from services.checkout import (
    CheckoutUnavailable,
    checkout_delivery_terms,
    quote_checkout,
    refresh_ready_checkout,
)
from services.customer_label import CustomerLabel
from services.finance import (
    apply_checkout_debit,
    find_unfinished_wallet_order,
    park_unconfirmed_wallet_provision,
    settle_wallet_provision_failure,
)
from services.orders import (
    create_pending_order,
    find_renewable_account,
    get_open_order,
    get_or_create_dynamic_plan,
    volume_fingerprint,
)
from services.panel_settings import (
    live_store_category,
    live_store_package,
    published_category_groups,
    resolve_groups_for_plan_type,
)
from services.payments import approve_wallet_payment
from services.pricing import PlanType
from services.promo_rules import (
    PromoExpired,
    PromoFirstPurchaseOnly,
    PromoRuleError,
    PromoUsedUp,
    PromoUserLimit,
)
from services.provisioning import provision_order
from services.renewal import (
    CustomRenewOffer,
    RenewKind,
    buyer_can_be_priced_for_custom,
    custom_renew_still_allowed,
    resolve_renew,
)
from services.reseller import has_per_gb_rate, list_plan_prices
from services.service_names import sanitize_service_name
from services.store_catalog import (
    CUSTOM_VOLUME_GB_MAX,
    CUSTOM_VOLUME_GB_MIN,
    category_kind,
    category_volume_hwid_limit,
    custom_purchase_available,
    custom_purchase_offered,
    filter_ready_plans_by_time_limit,
    flat_plan_list_enabled,
    hwid_limit_label_fa,
    is_unlimited_like,
    is_volume_like,
    package_hwid_limit,
    parse_custom_volume_gb,
    ready_package_purchase_payload,
)
from services.store_config import get_published_store_config, load_pricing_config_async
from services.store_pricing import (
    DEFAULT_TIER,
    category_durations,
    category_tiers,
    duration_from_package_options,
    get_price,
)
from services.user_offers import OfferNoLongerLive
from services.ux_format import fa_toman as _fmt_toman
from services.ux_format import to_persian_digits
from services.wallet import InsufficientWalletFunds, WalletError, get_balance

logger = logging.getLogger(__name__)
router = Router(name="purchase")


def wallet_split(balance: int, final_price: int) -> tuple[int, int]:
    """Wallet share and card remainder from the live balance and the till price."""
    price = max(0, int(final_price))
    applied = min(max(0, int(balance)), price)
    return applied, price - applied


@dataclass(frozen=True)
class _Buyer:
    """What decides whether this person is offered the custom builder."""

    is_reseller: bool = False
    has_per_gb_rate: bool = False

    def offered_custom(self, cat: Any, pricing: Any) -> bool:
        return custom_purchase_offered(
            cat,
            pricing,
            is_reseller=self.is_reseller,
            has_per_gb_rate=self.has_per_gb_rate,
        )


async def _buyer(session: AsyncSession, db_user: User | None) -> _Buyer:
    if db_user is None or not getattr(db_user, "is_reseller", False):
        return _Buyer()
    return _Buyer(is_reseller=True, has_per_gb_rate=await has_per_gb_rate(session, db_user.id))


async def _cfg(session: AsyncSession, settings: Settings) -> dict[str, Any]:
    """Prefer published store DB document; file only before first publish (bootstrap)."""
    return await load_pricing_config_async(session, settings=settings)


async def _renewal_line(session: AsyncSession, data: dict[str, Any], db_user: User | None) -> str:
    """«تمدید سرویس: <name>» when this order will extend one of the buyer's services.

    The order renews exactly when it carries `renew_account_id` and is neither a
    forced new service nor a plan switch (which has its own note). The account
    is loaded by id and must be the buyer's own; `order.plan`-style lazy
    relationships are never touched here.
    """
    account_id = data.get("renew_account_id")
    if not account_id or db_user is None or data.get("force_new") or data.get("renew_resets"):
        return ""
    account = await session.get(PasarGuardAccount, int(account_id))
    if account is None or int(account.user_id) != int(db_user.id):
        return ""
    label = service_list_label(account, reseller=bool(db_user.is_reseller))
    return texts.purchase_summary_renew.format(name=html.escape(label))


def _duration_text(days: int) -> str:
    """«۱ ماه» for whole months, «۱۰ روز» otherwise: a rule the bot states, it keeps.

    A renew from scratch starts from the days the service was bought with, which
    need not be a multiple of 30; «۱۰ روز» used to read «۱ ماه».
    """
    if days >= 30 and days % 30 == 0:
        return f"{to_persian_digits(days // 30)} ماه"
    return f"{to_persian_digits(days)} روز"


def _summary_details(data: dict[str, Any], renewal_line: str = "") -> str:
    plan_type = data.get("plan_type")
    name_line = f"{renewal_line}\n" if renewal_line else ""
    if data.get("customer_label"):
        name_line += f"👤 مشتری: {data.get('customer_label')}\n"
    elif data.get("service_name"):
        name_line += f"📡 نام سرویس: {data.get('service_name')}\n"
    if plan_type == PlanType.VOLUME.value:
        raw_days = data.get("duration_days")
        if raw_days is None:
            # «بدون انقضا»: say so. It used to print «None ماه».
            duration = texts.purchase_duration_none
        else:
            duration = _duration_text(int(raw_days))
        hwid_raw = data.get("hwid_limit")
        try:
            hwid = 0 if hwid_raw is None else max(0, int(hwid_raw))
        except (TypeError, ValueError):
            hwid = 0
        return (
            f"{name_line}"
            f"🗂 نوع: حجمی\n"
            f"📦 حجم: {to_persian_digits(data.get('volume_gb'))} گیگ\n"
            f"📅 مدت: {duration}\n"
            f"👥 کاربر همزمان: {hwid_limit_label_fa(hwid)}"
        )
    return (
        f"{name_line}"
        f"🗂 نوع: نامحدود\n"
        f"👥 کاربر همزمان: {to_persian_digits(data.get('concurrent_users'))}\n"
        f"📅 مدت: {to_persian_digits(data.get('duration_months'))} ماه"
    )


async def _resolve_volume_hwid_limit(session: AsyncSession, data: dict[str, Any]) -> int:
    """Ready plan options, else category custom flag, else 0. Never ``or 1``."""
    if "hwid_limit" in data and data.get("hwid_limit") is not None:
        try:
            return max(0, int(data["hwid_limit"]))
        except (TypeError, ValueError):
            pass
    cat_id = data.get("store_category_id")
    if cat_id is not None:
        cfg = await get_published_store_config(session)
        if cfg is not None:
            cat = next(
                (c for c in cfg.categories if int(c.id) == int(cat_id)),
                None,
            )
            if cat is None:
                live = await live_store_category(session, int(cat_id))
                cat = next(
                    (c for c in cfg.categories if live is not None and int(c.id) == int(live.id)),
                    None,
                )
            return category_volume_hwid_limit(cat)
    return 0


async def resume_open_order_if_any(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User,
) -> bool:
    """Resume PENDING_PAYMENT / PAYMENT_REVIEW. Returns True if handled."""
    open_order = await get_open_order(session, db_user.id)
    if open_order is None:
        return False
    if open_order.status == OrderStatus.PAYMENT_REVIEW:
        await state.clear()
        await message.answer(
            texts.purchase_open_in_review.format(order_id=to_persian_digits(open_order.id)),
            reply_markup=receipt_waiting_keyboard(),
        )
        return True
    card_amount = max(0, int(open_order.price_toman) - int(open_order.wallet_applied_toman or 0))
    await message.answer(
        texts.purchase_resume_open.format(order_id=to_persian_digits(open_order.id))
    )
    await begin_payment_wait(
        message,
        state,
        settings,
        session=session,
        order_id=open_order.id,
        amount_toman=card_amount,
    )
    return True


def _sellable(cat: Any) -> bool:
    if not cat.enabled:
        return False
    return bool(dict(cat.sales_flags or {}).get("allow_new_sale", True))


async def _published_sale_categories(
    session: AsyncSession, *, parent_id: int | None = None, db_user: User | None = None
) -> list:
    """Groups a customer may buy from, at one level of the tree.

    ``parent_id=None`` returns the top-level groups only — without this filter a
    nested catalog («حجمی» → «ویژه» / «اقتصادی») would show parent and children
    side by side as if they were peers.
    """
    cfg = await get_published_store_config(session)
    if cfg is None:
        return []
    buyer = await _buyer(session, db_user)
    return [
        cat
        for cat in sorted(cfg.categories, key=lambda c: (int(c.sort_order), int(c.id)))
        if _sellable(cat)
        and getattr(cat, "parent_id", None) == parent_id
        and _has_something_to_sell(cfg, cat, buyer)
    ]


def _has_something_to_sell(cfg: Any, cat: Any, buyer: _Buyer) -> bool:
    """Whether tapping this group reaches anything, mirroring ``_enter_category``:
    a child group that does, else a ready plan, else the custom builder.

    Without this an empty group sits on the menu and every tap on it is a dead
    end — the fallback cannot honestly point anywhere but back.
    """
    if any(
        _sellable(child) and _has_something_to_sell(cfg, child, buyer)
        for child in cfg.categories
        if getattr(child, "parent_id", None) == int(cat.id)
    ):
        return True
    if not published_category_groups(cfg.categories, cat):
        # No panel group means nothing can be provisioned: the group is not for
        # sale, however many plans it holds.
        return False
    pkgs = [p for p in cfg.packages if int(p.category_id) == int(cat.id)]
    return bool(filter_ready_plans_by_time_limit(pkgs, category=cat)) or buyer.offered_custom(
        cat, getattr(cfg, "pricing", None)
    )


async def _enter_category(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    cat: Any,
    db_user: User | None = None,
) -> None:
    """Show a group's children when it has any, else go straight to its plans.

    This is the adaptive level: «نامحدود» with no children costs the customer no
    extra tap, while «حجمی» expands into «ویژه» / «اقتصادی».
    """
    children = await _published_sale_categories(session, parent_id=int(cat.id), db_user=db_user)
    if children:
        await state.update_data(store_parent_category_id=int(cat.id))
        await state.set_state(PurchaseStates.choosing_plan_type)
        await message.answer(
            texts.purchase_choose_subgroup.format(parent=cat.title_fa),
            reply_markup=categories_keyboard(children),
        )
        return
    if is_volume_like(cat):
        await _enter_volume_like_group(message, state, session, settings, cat, db_user=db_user)
        return
    await _after_category_entry(message, state, session, settings, cat=cat, db_user=db_user)


async def _start_flow(
    target_message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None = None,
) -> None:
    # Every way into a new purchase passes here, after the open order was resumed.
    if await refuse_if_panel_down(target_message, session, settings):
        return
    await state.clear()
    await state.set_state(PurchaseStates.choosing_plan_type)
    cats = await _published_sale_categories(session, db_user=db_user)
    if cats:
        await target_message.answer(
            texts.purchase_choose_type, reply_markup=categories_keyboard(cats)
        )
        return
    await target_message.answer(texts.purchase_choose_type, reply_markup=plan_type_keyboard())


async def _category_ready_packages(session: AsyncSession, category_id: int) -> list:
    cfg = await get_published_store_config(session)
    if cfg is None:
        return []
    cat = next((c for c in cfg.categories if int(c.id) == int(category_id)), None)
    pkgs = [p for p in cfg.packages if int(p.category_id) == int(category_id)]
    return filter_ready_plans_by_time_limit(pkgs, category=cat)


async def _category_has_custom(session: AsyncSession, category_id: int) -> bool:
    cfg = await get_published_store_config(session)
    if cfg is None:
        return False
    for p in cfg.packages:
        if int(p.category_id) != int(category_id):
            continue
        kind_val = p.kind.value if hasattr(p.kind, "value") else str(p.kind)
        if kind_val == "custom_template" and p.enabled and p.allow_new_sale:
            return True
    return False


def _kind_only(plan_type: PlanType | str) -> dict[str, Any]:
    """The group-less stand-in for a flow that never picked a store group."""
    value = plan_type.value if isinstance(plan_type, PlanType) else str(plan_type)
    return {"sales_flags": {"kind": value}}


def _volume_prompt() -> str:
    hint = texts.purchase_volume_type_hint.format(
        low=to_persian_digits(CUSTOM_VOLUME_GB_MIN), high=to_persian_digits(CUSTOM_VOLUME_GB_MAX)
    )
    return f"{texts.purchase_choose_volume}\n{hint}"


def _volume_keyboard(cfg: dict[str, Any]) -> Any:
    return options_keyboard(
        PurchaseCB.VOL_PREFIX,
        list(cfg["volume_plan"]["volume_options_gb"]),
        label_for=lambda gb: f"{to_persian_digits(gb)} گیگ",
    )


async def _refuse_custom(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: User | None,
    *,
    nothing_to_open: bool = False,
) -> None:
    """The builder is not offered to this buyer here: say so, back to the groups.

    A stale button (the owner changed the setting while the builder was open)
    or a forged callback ends here, never at a price or a missing key.
    ``nothing_to_open`` is the older case: the pricing document has no custom
    plan to ask about (a store built in the admin panel).
    """
    categories = await _published_sale_categories(session, db_user=db_user)
    keyboard = categories_keyboard(categories)
    await end_read_transaction(session)
    await state.set_state(PurchaseStates.choosing_plan_type)
    await message.answer(
        texts.purchase_no_ready_plans if nothing_to_open else texts.purchase_custom_not_offered,
        reply_markup=keyboard,
    )


async def _builder_category(session: AsyncSession, state: FSMContext) -> Any | None:
    """The group whose builder is open: its live row, or just the kind when the
    flow never picked a group. ``None`` when that group is gone."""
    data = await state.get_data()
    if data.get("store_category_id") is None:
        return _kind_only(data.get("plan_type") or PlanType.VOLUME.value)
    return await _load_shelf_category(session, state)


async def _builder_open_for(
    session: AsyncSession, settings: Settings, state: FSMContext, db_user: User | None
) -> bool:
    """Every step of the builder asks this first (see ``custom_purchase_offered``)."""
    cat = await _builder_category(session, state)
    if cat is None:
        return False
    return (await _buyer(session, db_user)).offered_custom(cat, await _cfg(session, settings))


async def _begin_custom_for_plan_type(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    plan_type: PlanType,
    *,
    cat: Any | None,
    db_user: User | None,
) -> None:
    cfg = await _cfg(session, settings)
    if cat is None:
        cat = _kind_only(plan_type)
    if not custom_purchase_available(cat, cfg):
        # The pricing document has no custom plan to ask about (a store built
        # in the admin panel): say so, never index into a missing key.
        await _refuse_custom(message, state, session, db_user, nothing_to_open=True)
        return
    if not (await _buyer(session, db_user)).offered_custom(cat, cfg):
        await _refuse_custom(message, state, session, db_user)
        return
    await state.update_data(plan_type=plan_type.value, custom_purchase=True)
    if plan_type is PlanType.VOLUME:
        await state.set_state(PurchaseStates.choosing_volume)
        await message.answer(_volume_prompt(), reply_markup=_volume_keyboard(cfg))
        return
    await state.set_state(PurchaseStates.choosing_users)
    await message.answer(
        texts.purchase_choose_users,
        reply_markup=options_keyboard(
            PurchaseCB.USERS_PREFIX,
            list(cfg["unlimited_plan"]["user_options"]),
            label_for=lambda n: f"{to_persian_digits(n)} کاربر",
        ),
    )


async def _reseller_shelf_prices(
    session: AsyncSession,
    db_user: User | None,
    packages: list[Any],
    *,
    duration_days: int | None,
    tier: str,
) -> dict[str, int]:
    """What this buyer actually pays for each listed plan, if they are a shop.

    Empty for an ordinary customer, so the shelf keeps showing retail for
    everyone else. A plan with no agreed price is left out — they pay retail
    for it and the shelf price is already correct.
    """
    if db_user is None or not getattr(db_user, "is_reseller", False):
        return {}
    agreed = await list_plan_prices(session, db_user.id, duration_days=duration_days, tier=tier)
    if not agreed:
        return {}
    out: dict[str, int] = {}
    for pkg in packages:
        slug = str(getattr(pkg, "slug", "") or "")
        if slug in agreed:
            out[slug] = int(agreed[slug])
    return out


async def _reseller_shelf_prices_flat(
    session: AsyncSession,
    db_user: User | None,
    packages: list[Any],
    *,
    tier: str,
) -> dict[str, int]:
    """Reseller agreed prices keyed per plan at that plan's own duration cell."""
    if db_user is None or not getattr(db_user, "is_reseller", False):
        return {}
    by_duration: dict[int | None, list[Any]] = {}
    for pkg in packages:
        dur = duration_from_package_options(dict(getattr(pkg, "options", None) or {}))
        by_duration.setdefault(dur, []).append(pkg)
    out: dict[str, int] = {}
    for dur, pkgs in by_duration.items():
        agreed = await list_plan_prices(session, db_user.id, duration_days=dur, tier=tier)
        if not agreed:
            continue
        for pkg in pkgs:
            slug = str(getattr(pkg, "slug", "") or "")
            if slug in agreed:
                out[slug] = int(agreed[slug])
    return out


def _retail_shelf_prices(
    packages: list[Any],
    cfg: Any,
    *,
    duration_days: int | None,
    tier: str,
) -> dict[str, int]:
    """Matrix cell for each package at the selected duration and tier."""
    if cfg is None:
        return {}
    prices = list(getattr(cfg, "prices", None) or [])
    out: dict[str, int] = {}
    for pkg in packages:
        cell = get_price(prices, slug=str(pkg.slug), duration_days=duration_days, tier=tier)
        if cell is not None:
            out[str(pkg.slug)] = int(cell)
    return out


def _plan_own_retail_price(pkg: Any, prices: list[Any], *, tier: str) -> int | None:
    """Economy (or ``tier``) cell at the plan's own duration, else fixed_price."""
    dur = duration_from_package_options(dict(getattr(pkg, "options", None) or {}))
    cell = get_price(prices, slug=str(pkg.slug), duration_days=dur, tier=tier)
    if cell is not None:
        return int(cell)
    fixed = getattr(pkg, "fixed_price_toman", None)
    if fixed is not None:
        try:
            return int(fixed)
        except (TypeError, ValueError):
            return None
    return None


def _retail_shelf_prices_flat(
    packages: list[Any],
    cfg: Any,
    *,
    tier: str,
) -> dict[str, int]:
    """Each plan priced at its own duration cell (or fixed_price fallback)."""
    if cfg is None:
        out: dict[str, int] = {}
        for pkg in packages:
            fixed = getattr(pkg, "fixed_price_toman", None)
            if fixed is not None:
                out[str(pkg.slug)] = int(fixed)
        return out
    prices = list(getattr(cfg, "prices", None) or [])
    out = {}
    for pkg in packages:
        price = _plan_own_retail_price(pkg, prices, tier=tier)
        if price is not None:
            out[str(pkg.slug)] = price
    return out


def _plans_for_selection(
    packages: list[Any],
    cfg: Any,
    *,
    duration_days: int | None,
    tier: str,
    category: Any,
) -> list[Any]:
    """Ready plans that have a price cell for the current toggle selection."""
    ready = filter_ready_plans_by_time_limit(list(packages), category=category)
    if cfg is None:
        return ready
    prices = list(getattr(cfg, "prices", None) or [])
    priced = [
        pkg
        for pkg in ready
        if get_price(prices, slug=str(pkg.slug), duration_days=duration_days, tier=tier) is not None
    ]
    return priced


def _plans_for_flat(
    packages: list[Any],
    cfg: Any,
    *,
    category: Any,
    tier: str,
) -> list[Any]:
    """All ready plans that have a price at their own duration (or fixed_price)."""
    ready = filter_ready_plans_by_time_limit(list(packages), category=category)
    prices = list(getattr(cfg, "prices", None) or []) if cfg is not None else []
    return [pkg for pkg in ready if _plan_own_retail_price(pkg, prices, tier=tier) is not None]


async def _render_ready_shelf(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    _settings: Settings,
    *,
    cat: StoreCategory,
    db_user: User | None = None,
    edit: bool = False,
) -> None:
    """Show or re-edit the duration/tier shelf for one category."""
    data = await state.get_data()
    durs = category_durations(cat)
    tiers = category_tiers(cat)
    flat = flat_plan_list_enabled(cat)
    if "sel_duration_days" in data:
        sel_duration = data.get("sel_duration_days")
    else:
        sel_duration = durs[0] if durs else 30
    sel_tier = str(data.get("sel_tier") or (tiers[0] if tiers else DEFAULT_TIER))
    await state.update_data(sel_duration_days=sel_duration, sel_tier=sel_tier)

    cfg = await get_published_store_config(session)
    pkgs: list[Any] = []
    if cfg is not None:
        pkgs = [p for p in cfg.packages if int(p.category_id) == int(cat.id)]
    custom_ok = (await _buyer(session, db_user)).offered_custom(cat, await _cfg(session, _settings))

    if flat:
        visible = _plans_for_flat(pkgs, cfg, category=cat, tier=DEFAULT_TIER)
        retail = _retail_shelf_prices_flat(visible, cfg, tier=DEFAULT_TIER)
        reseller = await _reseller_shelf_prices_flat(session, db_user, visible, tier=DEFAULT_TIER)
        shelf = dict(retail)
        shelf.update(reseller)
        if reseller:
            body = texts.reseller_choose_plan
        elif is_unlimited_like(cat):
            body = texts.purchase_flat_shelf_intro_unlimited
        else:
            body = texts.purchase_flat_shelf_intro
            if visible and all(package_hwid_limit(p) == 0 for p in visible):
                body = f"{body}\n{texts.purchase_flat_no_user_limit}"
        if not visible:
            body = f"{body}\n\n{texts.purchase_no_plans_here}"
        markup = ready_plans_keyboard(
            visible,
            include_custom=custom_ok,
            prices_by_slug=shelf,
            flat_mode=True,
        )
    else:
        visible = _plans_for_selection(
            pkgs, cfg, duration_days=sel_duration, tier=sel_tier, category=cat
        )
        retail = _retail_shelf_prices(visible, cfg, duration_days=sel_duration, tier=sel_tier)
        reseller = await _reseller_shelf_prices(
            session, db_user, visible, duration_days=sel_duration, tier=sel_tier
        )
        shelf = dict(retail)
        shelf.update(reseller)
        body = texts.reseller_choose_plan if reseller else texts.purchase_choose_ready_plan
        if not visible:
            body = f"{body}\n\n{texts.purchase_no_plans_here}"
        markup = ready_plans_keyboard(
            visible,
            include_custom=custom_ok,
            prices_by_slug=shelf,
            durations=durs,
            tiers=tiers,
            selected_duration=sel_duration,
            selected_tier=sel_tier,
        )
    if edit:
        with contextlib.suppress(Exception):
            # "message is not modified" or non-text message — ignore.
            await message.edit_text(body, reply_markup=markup)
        return
    await message.answer(body, reply_markup=markup)


async def _after_category_entry(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    *,
    cat: StoreCategory,
    db_user: User | None = None,
) -> None:
    """After group pick: ready-plan shelf with duration/tier toggles, or custom."""
    kind = category_kind(cat) or PlanType.VOLUME.value
    plan_type = PlanType.VOLUME if kind == "volume" else PlanType.UNLIMITED
    durs = category_durations(cat)
    tiers = category_tiers(cat)
    await state.update_data(
        plan_type=plan_type.value,
        store_category_id=cat.id,
        store_category_slug=str(cat.slug),
        sel_duration_days=durs[0] if durs else 30,
        sel_tier=tiers[0] if tiers else DEFAULT_TIER,
        hwid_limit=(category_volume_hwid_limit(cat) if plan_type is PlanType.VOLUME else None),
    )
    cfg = await get_published_store_config(session)
    pkgs: list[Any] = []
    if cfg is not None:
        pkgs = [p for p in cfg.packages if int(p.category_id) == int(cat.id)]
    ready = filter_ready_plans_by_time_limit(pkgs, category=cat)
    custom_ok = (await _buyer(session, db_user)).offered_custom(cat, await _cfg(session, settings))
    if ready:
        await state.set_state(PurchaseStates.choosing_ready_plan)
        await _render_ready_shelf(
            message, state, session, settings, cat=cat, db_user=db_user, edit=False
        )
        return
    if custom_ok:
        await _begin_custom_for_plan_type(
            message, state, session, settings, plan_type, cat=cat, db_user=db_user
        )
        return
    # Not the legacy type keyboard: its buttons fall through to the custom
    # builder, which is exactly what this buyer is not offered here.
    await message.answer(
        texts.purchase_no_ready_plans,
        reply_markup=categories_keyboard(
            await _published_sale_categories(session, db_user=db_user)
        ),
    )


async def _enter_volume_like_group(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    cat: StoreCategory,
    db_user: User | None = None,
) -> None:
    await _after_category_entry(message, state, session, settings, cat=cat, db_user=db_user)


def _panel_user_loader(session: AsyncSession, settings: Settings) -> Any:
    """Reads one live panel user, holding no database snapshot across the call."""

    async def load(username: str) -> Any:
        client = await get_pasarguard_client(session, settings)
        try:
            await end_read_transaction(session)
            return await client.get_user(username)
        finally:
            await client.aclose()

    return load


async def start_renew_purchase(
    target_message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User,
    *,
    account_id: int,
) -> None:
    """«📅 تمدید این سرویس»: what happens is decided by ``resolve_renew``, nowhere else.

    A ready plan that matches is quoted at today's price and **added** to what is
    left. A volume service no ready plan matches is renewed **from scratch**
    (one screen: volume and days). Anything else picks a plan for this same
    service. A group with «تمدید» off, or a trial, gets one message and no order.
    """
    if await resume_open_order_if_any(target_message, state, session, settings, db_user):
        return
    if await refuse_if_panel_down(target_message, session, settings):
        return
    decision = await resolve_renew(
        session,
        user=db_user,
        account_id=account_id,
        load_panel_user=_panel_user_loader(session, settings),
    )
    if decision.kind is RenewKind.PLAN and decision.payload is not None:
        await state.clear()
        await state.update_data(**decision.payload)
        # A renewal is a purchase like any other: a retail customer with a code
        # must be able to use it here too.
        await _continue_to_promo_or_summary(target_message, state, session, settings, db_user)
        return
    if decision.kind is RenewKind.CUSTOM and decision.custom is not None:
        await _begin_renew_custom(
            target_message, state, session, settings, db_user, decision.custom
        )
        return
    if decision.kind is RenewKind.SWITCH:
        # The plan it was bought as is gone, or this is not a volume service
        # the buyer can be priced for: any plan picked now goes onto this same
        # service.
        await start_switch_flow(
            target_message,
            state,
            session,
            db_user,
            account_id=account_id,
            intro=texts.purchase_switch_plan_gone,
        )
        return
    if decision.kind in (RenewKind.REFUSED, RenewKind.PANEL_DOWN):
        await target_message.answer(texts.account_error, reply_markup=renew_plan_off_keyboard())
        return
    # OFF (the group's «تمدید» is off, or a trial) and NO_GROUP: renewing is not
    # available here; the next step is a new service.
    await target_message.answer(
        texts.purchase_renew_plan_off, reply_markup=renew_plan_off_keyboard()
    )


# --------------------------------------------------------------------------- #
# «تمدید از نو»: the volume and time set anew, one screen
# --------------------------------------------------------------------------- #


async def _begin_renew_custom(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User,
    offer: CustomRenewOffer,
) -> None:
    """Start the screen from the defaults ``resolve_renew`` found.

    The order made at the end carries ``renew_mode='reset'`` and no plan slug, and
    no group is asked: the service keeps the panel groups it has.
    """
    await state.clear()
    await state.update_data(
        plan_type=PlanType.VOLUME.value,
        volume_gb=int(offer.volume_gb),
        duration_days=int(offer.duration_days),
        renew_account_id=int(offer.account_id),
        renew_mode=RENEW_MODE_RESET,
        custom_purchase=True,
        force_new=False,
        store_category_id=int(offer.category_id),
        store_category_slug=offer.category_slug,
        renew_default_gb=int(offer.volume_gb),
        renew_default_days=int(offer.duration_days),
    )
    await state.set_state(PurchaseStates.renewing_custom)
    await _show_renew_custom(message, state, session, settings, db_user, edit=False)


async def _show_renew_custom(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    *,
    edit: bool,
) -> None:
    """The one screen: today's price for the volume and days on it. Typing a number
    or tapping a duration comes back here with the new price."""
    data = await state.get_data()
    price = await quote_checkout(session, settings, data, promo_code=None, db_user=db_user)
    cfg = await _cfg(session, settings)
    account = await session.get(PasarGuardAccount, int(data["renew_account_id"]))
    name = (
        service_list_label(account, reseller=bool(getattr(db_user, "is_reseller", False)))
        if account is not None
        else ""
    )
    same = int(data["volume_gb"]) == int(data.get("renew_default_gb") or 0) and int(
        data["duration_days"]
    ) == int(data.get("renew_default_days") or 0)
    text = texts.purchase_renew_custom_screen.format(
        name=html.escape(name),
        lead=f"{texts.purchase_renew_custom_same} " if same else "",
        volume=to_persian_digits(int(data["volume_gb"])),
        duration=_duration_text(int(data["duration_days"])),
        price=_fmt_toman(price.final_price),
        low=to_persian_digits(CUSTOM_VOLUME_GB_MIN),
        high=to_persian_digits(CUSTOM_VOLUME_GB_MAX),
    )
    markup = renew_custom_keyboard(
        [int(d) for d in cfg["volume_plan"]["duration_options_days"]],
        selected_days=int(data["duration_days"]),
    )
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.callback_query(
    PurchaseStates.renewing_custom, F.data.startswith(PurchaseCB.RENEW_CUSTOM_DAYS_PREFIX)
)
async def renew_custom_days(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    await callback.answer()
    if callback.message is None:
        return
    raw = (callback.data or "").removeprefix(PurchaseCB.RENEW_CUSTOM_DAYS_PREFIX)
    cfg = await _cfg(session, settings)
    offered = [int(d) for d in cfg["volume_plan"]["duration_options_days"]]
    if not raw.isdigit() or int(raw) not in offered:
        return  # a forged or stale button: nothing changes
    data = await state.get_data()
    if int(raw) == int(data.get("duration_days") or 0):
        return  # already chosen; an edit with the same text is refused by Telegram
    await state.update_data(duration_days=int(raw))
    await _show_renew_custom(callback.message, state, session, settings, db_user, edit=True)


@router.message(PurchaseStates.renewing_custom)
async def renew_custom_typed(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """A typed volume: a whole number of gigabytes in the limits (the same
    ``parse_custom_volume_gb`` the builder uses), else refused with the range."""
    volume_gb = parse_custom_volume_gb(message.text or "")
    if volume_gb is None:
        data = await state.get_data()
        cfg = await _cfg(session, settings)
        await message.answer(
            texts.purchase_volume_typed_invalid.format(
                low=to_persian_digits(CUSTOM_VOLUME_GB_MIN),
                high=to_persian_digits(CUSTOM_VOLUME_GB_MAX),
            ),
            reply_markup=renew_custom_keyboard(
                [int(d) for d in cfg["volume_plan"]["duration_options_days"]],
                selected_days=int(data.get("duration_days") or 0),
            ),
        )
        return
    await state.update_data(volume_gb=volume_gb)
    await _show_renew_custom(message, state, session, settings, db_user, edit=False)


@router.callback_query(PurchaseStates.renewing_custom, F.data == PurchaseCB.RENEW_CUSTOM_OK)
async def renew_custom_ok(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """«✅ همین»: on to the promo question (retail) or the summary (a reseller).

    Asked again here, because the screen may be old: the service must still be
    this buyer's, its group must still allow renewing, and a reseller must still
    have a rate (without one the price would silently be retail).
    """
    await callback.answer()
    if callback.message is None or db_user is None:
        return
    data = await state.get_data()
    account = await session.get(PasarGuardAccount, int(data.get("renew_account_id") or 0))
    cfg = await get_published_store_config(session)
    if (
        account is None
        or int(account.user_id) != int(db_user.id)
        or cfg is None
        or not custom_renew_still_allowed(cfg, category_slug=str(data.get("store_category_slug")))
        or not await buyer_can_be_priced_for_custom(session, cfg, db_user)
    ):
        await state.clear()
        await callback.message.answer(
            texts.purchase_renew_plan_off, reply_markup=renew_plan_off_keyboard()
        )
        return
    await _continue_to_promo_or_summary(callback.message, state, session, settings, db_user)


async def start_switch_flow(
    target_message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: User,
    *,
    account_id: int,
    intro: str,
) -> None:
    """Browse the store with every pick bound to this one account.

    The order that follows carries ``renew_resets``: the account is rebuilt on
    the chosen plan, same username and link, and the summary says the leftover
    of the old plan is dropped before anything is paid.
    """
    account = await get_live_account(session, account_id)
    if account is None or int(account.user_id) != int(db_user.id):
        await target_message.answer(texts.account_error, reply_markup=renew_plan_off_keyboard())
        return
    await state.clear()
    await state.update_data(intent_renew_account_id=int(account.id), renew_resets=True)
    await state.set_state(PurchaseStates.choosing_plan_type)
    cats = await _published_sale_categories(session, db_user=db_user)
    if not cats:
        await target_message.answer(
            texts.purchase_renew_plan_off, reply_markup=renew_plan_off_keyboard()
        )
        return
    await target_message.answer(intro, reply_markup=categories_keyboard(cats))


@router.callback_query(F.data.startswith(PurchaseCB.SWITCH_PREFIX))
async def switch_plan_for_account(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    await callback.answer()
    raw = (callback.data or "").removeprefix(PurchaseCB.SWITCH_PREFIX)
    if callback.message is None or db_user is None or not raw.isdigit():
        return
    if await resume_open_order_if_any(callback.message, state, session, settings, db_user):
        return
    if await refuse_if_panel_down(callback.message, session, settings):
        return
    await start_switch_flow(
        callback.message,
        state,
        session,
        db_user,
        account_id=int(raw),
        intro=texts.purchase_switch_pick,
    )


async def start_purchase_flow(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None = None,
) -> None:
    """Public entry to the purchase flow, for the bottom keyboard."""
    await _start_flow(message, state, session, settings, db_user)


@router.callback_query(F.data == MenuCallback.BUY)
async def menu_buy(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    await callback.answer()
    if callback.message is None:
        return
    if db_user is not None and await resume_open_order_if_any(
        callback.message, state, session, settings, db_user
    ):
        return
    await _start_flow(callback.message, state, session, settings, db_user)


@router.callback_query(F.data == PurchaseCB.CANCEL)
async def purchase_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    # Clear FSM only; open PENDING order remains and is resumed via buy/start.
    await state.clear()
    await callback.answer()
    if callback.message:
        await callback.message.answer(texts.purchase_cancelled)


@router.callback_query(F.data == PurchaseCB.RESTART)
async def purchase_restart(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    await callback.answer()
    if callback.message is None:
        return
    current = await state.get_state()
    if (
        current == PurchaseStates.waiting_for_receipt.state
        and db_user is not None
        and await resume_open_order_if_any(callback.message, state, session, settings, db_user)
    ):
        return
    await _start_flow(callback.message, state, session, settings, db_user)


@router.callback_query(PurchaseStates.choosing_plan_type, F.data == PurchaseCB.TYPE_VOLUME)
async def choose_volume_type(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None = None,
) -> None:
    await callback.answer()
    if callback.message is None:
        return
    cats = await _published_sale_categories(session, db_user=db_user)
    vol = next((c for c in cats if is_volume_like(c)), None)
    if vol is not None:
        await _enter_volume_like_group(
            callback.message, state, session, settings, vol, db_user=db_user
        )
        return
    await _begin_custom_for_plan_type(
        callback.message, state, session, settings, PlanType.VOLUME, cat=None, db_user=db_user
    )


@router.callback_query(PurchaseStates.choosing_plan_type, F.data == PurchaseCB.TYPE_UNLIMITED)
async def choose_unlimited_type(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None = None,
) -> None:
    await callback.answer()
    if callback.message is None:
        return
    cats = await _published_sale_categories(session, db_user=db_user)
    unl = next((c for c in cats if category_kind(c) == "unlimited"), None)
    if unl is not None:
        await _after_category_entry(
            callback.message,
            state,
            session,
            settings,
            cat=unl,
            db_user=db_user,
        )
        return
    await _begin_custom_for_plan_type(
        callback.message, state, session, settings, PlanType.UNLIMITED, cat=None, db_user=db_user
    )


@router.callback_query(PurchaseStates.choosing_plan_type, F.data.startswith(PurchaseCB.CAT_PREFIX))
async def choose_category(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None = None,
) -> None:
    await callback.answer()
    if callback.message is None:
        return
    raw = (callback.data or "")[len(PurchaseCB.CAT_PREFIX) :]
    if not raw.isdigit():
        return
    cat_id = int(raw)
    # Accept a top-level group or a child of the group already opened.
    data = await state.get_data()
    parent_id = data.get("store_parent_category_id")
    candidates = await _published_sale_categories(session, db_user=db_user)
    if parent_id is not None:
        candidates += await _published_sale_categories(
            session, parent_id=int(parent_id), db_user=db_user
        )
    cat = next((c for c in candidates if int(c.id) == cat_id), None)
    if cat is None:
        # A button from before the last catalog edit: resolve through the slug.
        live = await live_store_category(session, cat_id)
        if live is not None:
            cat = next((c for c in candidates if int(c.id) == int(live.id)), None)
    if cat is None:
        await callback.message.answer(texts.purchase_error)
        return
    await _enter_category(callback.message, state, session, settings, cat, db_user=db_user)


async def _load_shelf_category(session: AsyncSession, state: FSMContext) -> StoreCategory | None:
    """The LIVE row of the group whose shelf the customer has open.

    The slug is the identity: a catalog edit mints new ids, so the id held in
    FSM may belong to an archived version (and carry its old panel groups).
    """
    data = await state.get_data()
    cfg = await get_published_store_config(session)
    if cfg is None:
        return None
    slug = data.get("store_category_slug")
    if slug:
        hit = next((c for c in cfg.categories if str(c.slug) == str(slug)), None)
        if hit is not None:
            return hit
    cat_id = data.get("store_category_id")
    if cat_id is None:
        return None
    live = await live_store_category(session, int(cat_id))
    if live is None:
        return None
    return next((c for c in cfg.categories if int(c.id) == int(live.id)), None)


@router.callback_query(PurchaseStates.choosing_ready_plan, F.data.startswith(PurchaseCB.DUR_PREFIX))
async def choose_duration_toggle(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None = None,
) -> None:
    raw = (callback.data or "")[len(PurchaseCB.DUR_PREFIX) :]
    try:
        selected = parse_duration_cb(raw)
    except ValueError:
        await callback.answer()
        return
    data = await state.get_data()
    current = data.get("sel_duration_days")
    if "sel_duration_days" in data and current == selected:
        await callback.answer()
        return
    await callback.answer()
    if callback.message is None:
        return
    await state.update_data(sel_duration_days=selected)
    cat = await _load_shelf_category(session, state)
    if cat is None:
        return
    await _render_ready_shelf(
        callback.message,
        state,
        session,
        settings,
        cat=cat,
        db_user=db_user,
        edit=True,
    )


@router.callback_query(
    PurchaseStates.choosing_ready_plan, F.data.startswith(PurchaseCB.TIER_PREFIX)
)
async def choose_tier_toggle(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None = None,
) -> None:
    raw = (callback.data or "")[len(PurchaseCB.TIER_PREFIX) :]
    if raw not in {"economy", "premium"}:
        await callback.answer()
        return
    data = await state.get_data()
    if data.get("sel_tier") == raw:
        await callback.answer()
        return
    await callback.answer()
    if callback.message is None:
        return
    await state.update_data(sel_tier=raw)
    cat = await _load_shelf_category(session, state)
    if cat is None:
        return
    await _render_ready_shelf(
        callback.message,
        state,
        session,
        settings,
        cat=cat,
        db_user=db_user,
        edit=True,
    )


@router.callback_query(PurchaseStates.choosing_ready_plan, F.data == PurchaseCB.CUSTOM)
async def choose_custom_from_ready(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await refuse_if_panel_down(callback, session, settings):
        return
    await callback.answer()
    if callback.message is None:
        return
    data = await state.get_data()
    cat = await _builder_category(session, state)
    if cat is None:
        await _refuse_custom(callback.message, state, session, db_user)
        return
    plan_type = PlanType(data.get("plan_type") or PlanType.VOLUME.value)
    await _begin_custom_for_plan_type(
        callback.message, state, session, settings, plan_type, cat=cat, db_user=db_user
    )


@router.callback_query(
    PurchaseStates.choosing_ready_plan, F.data.startswith(PurchaseCB.READY_PREFIX)
)
async def choose_ready_plan(  # noqa: PLR0911 — early exits for gone/incomplete plans
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if callback.message is None:
        return
    raw = (callback.data or "")[len(PurchaseCB.READY_PREFIX) :]
    if not raw.isdigit():
        await callback.answer()
        return
    pkg_id = int(raw)
    cfg = await get_published_store_config(session)
    if cfg is None:
        await callback.answer()
        await callback.message.answer(texts.purchase_error)
        return
    pkg = next((p for p in cfg.packages if int(p.id) == pkg_id), None)
    if pkg is None:
        # The id predates the last catalog edit: resolve it through its slug.
        live = await live_store_package(session, pkg_id)
        if live is not None:
            pkg = next((p for p in cfg.packages if int(p.id) == int(live.id)), None)
    cat = await _load_shelf_category(session, state)
    if pkg is None or not pkg.enabled:
        # The plan truly disappeared: say so and show the shelf again.
        await callback.answer(texts.purchase_plan_gone, show_alert=True)
        if cat is not None:
            await _render_ready_shelf(
                callback.message, state, session, settings, cat=cat, db_user=db_user, edit=True
            )
        else:
            await callback.message.answer(texts.purchase_error)
        return
    if cat is None or int(pkg.category_id) != int(cat.id):
        cat = next((c for c in cfg.categories if int(c.id) == int(pkg.category_id)), cat)
    if cat is not None:
        await state.update_data(store_category_id=cat.id, store_category_slug=str(cat.slug))
    data = await state.get_data()
    sel_duration = data.get("sel_duration_days")
    sel_tier = str(data.get("sel_tier") or DEFAULT_TIER)
    prices = list(getattr(cfg, "prices", None) or [])
    flat = cat is not None and flat_plan_list_enabled(cat)
    if flat:
        # Charge the tapped plan's own duration cell, not the FSM tab selection.
        plan_duration = duration_from_package_options(dict(pkg.options or {}))
        sel_duration = plan_duration
        sel_tier = DEFAULT_TIER
        await state.update_data(sel_duration_days=sel_duration, sel_tier=sel_tier)
        cell = get_price(prices, slug=str(pkg.slug), duration_days=sel_duration, tier=sel_tier)
        if cell is None:
            fixed = getattr(pkg, "fixed_price_toman", None)
            if fixed is not None:
                cell = int(fixed)
        if cell is None:
            await callback.answer(texts.purchase_plan_gone, show_alert=True)
            if cat is not None:
                await _render_ready_shelf(
                    callback.message,
                    state,
                    session,
                    settings,
                    cat=cat,
                    db_user=db_user,
                    edit=True,
                )
            return
        await callback.answer()
        payload = ready_package_purchase_payload(
            pkg,
            prices,
            duration_days=sel_duration,
            tier=sel_tier,
            use_selected_duration=False,
        )
    else:
        cell = get_price(prices, slug=str(pkg.slug), duration_days=sel_duration, tier=sel_tier)
        if cell is None:
            await callback.answer(texts.purchase_plan_gone, show_alert=True)
            if cat is not None:
                await _render_ready_shelf(
                    callback.message,
                    state,
                    session,
                    settings,
                    cat=cat,
                    db_user=db_user,
                    edit=True,
                )
            return
        await callback.answer()
        payload = ready_package_purchase_payload(
            pkg,
            prices,
            duration_days=sel_duration,
            tier=sel_tier,
            use_selected_duration=True,
        )
    if payload.get("fixed_price_toman") is None:
        payload["fixed_price_toman"] = int(cell)
    kind = data.get("plan_type") or PlanType.VOLUME.value
    if payload.get("volume_gb") is not None:
        kind = PlanType.VOLUME.value
    elif payload.get("concurrent_users") is not None:
        kind = PlanType.UNLIMITED.value
    await state.update_data(plan_type=kind, custom_purchase=False, **payload)
    # Volume needs GB; duration may be None («بدون انقضا») after T-006.
    if kind == PlanType.VOLUME.value and payload.get("volume_gb") is None:
        await _begin_custom_for_plan_type(
            callback.message, state, session, settings, PlanType.VOLUME, cat=cat, db_user=db_user
        )
        return
    if kind == PlanType.UNLIMITED.value and payload.get("concurrent_users") is None:
        await _begin_custom_for_plan_type(
            callback.message,
            state,
            session,
            settings,
            PlanType.UNLIMITED,
            cat=cat,
            db_user=db_user,
        )
        return
    await _ask_service_name(callback.message, state, db_user, session, settings)


async def _ask_duration_for_volume(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    volume_gb: int,
) -> None:
    """The step after the volume, whether it was tapped or typed: one place, so a
    typed value reaches the same price function and order fields a button does."""
    cfg = await _cfg(session, settings)
    await state.update_data(volume_gb=volume_gb)
    await state.set_state(PurchaseStates.choosing_duration)
    await message.answer(
        texts.purchase_choose_duration_months,
        reply_markup=options_keyboard(
            PurchaseCB.DAYS_PREFIX,
            list(cfg["volume_plan"]["duration_options_days"]),
            label_for=lambda days: f"{to_persian_digits(max(1, int(days) // 30))} ماه",
        ),
    )


@router.callback_query(PurchaseStates.choosing_volume, F.data.startswith(PurchaseCB.VOL_PREFIX))
async def choose_volume(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    await callback.answer()
    if callback.message is None:
        return
    if not await _builder_open_for(session, settings, state, db_user):
        await _refuse_custom(callback.message, state, session, db_user)
        return
    try:
        volume_gb = int((callback.data or "").split(":")[-1])
    except ValueError:
        return
    await _ask_duration_for_volume(callback.message, state, session, settings, volume_gb)


@router.message(PurchaseStates.choosing_volume)
async def volume_typed(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """The volume typed instead of tapped: a whole number of gigabytes in the limits.

    Anything else is refused here with the range and the buyer stays on the
    question (state unchanged, quick buttons under the refusal).
    """
    if not await _builder_open_for(session, settings, state, db_user):
        await _refuse_custom(message, state, session, db_user)
        return
    volume_gb = parse_custom_volume_gb(message.text or "")
    if volume_gb is None:
        await message.answer(
            texts.purchase_volume_typed_invalid.format(
                low=to_persian_digits(CUSTOM_VOLUME_GB_MIN),
                high=to_persian_digits(CUSTOM_VOLUME_GB_MAX),
            ),
            reply_markup=_volume_keyboard(await _cfg(session, settings)),
        )
        return
    await _ask_duration_for_volume(message, state, session, settings, volume_gb)


@router.callback_query(PurchaseStates.choosing_users, F.data.startswith(PurchaseCB.USERS_PREFIX))
async def choose_users(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    await callback.answer()
    if callback.message is None:
        return
    if not await _builder_open_for(session, settings, state, db_user):
        await _refuse_custom(callback.message, state, session, db_user)
        return
    users = int((callback.data or "").split(":")[-1])
    cfg = await _cfg(session, settings)
    await state.update_data(concurrent_users=users)
    await state.set_state(PurchaseStates.choosing_duration)
    await callback.message.answer(
        texts.purchase_choose_duration_months,
        reply_markup=options_keyboard(
            PurchaseCB.MONTHS_PREFIX,
            list(cfg["unlimited_plan"]["duration_options_months"]),
            label_for=lambda months: f"{to_persian_digits(months)} ماه",
        ),
    )


async def _ask_service_name(
    message: Message,
    state: FSMContext,
    db_user: User | None = None,
    session: AsyncSession | None = None,
    settings: Settings | None = None,
) -> None:
    """Who is this service for — asked differently depending on who is asking.

    A customer names their **own** service in one step. A reseller is naming
    somebody else's, and that takes two: the Latin name, which seeds the panel
    username exactly as a customer's does, and then the phone, which must
    **not**. Asked in one box, «reza 09123456789» would put eleven digits in
    every panel username the shop ever has to search for.

    Neither question is asked when the shop arrived here from a *named*
    service in «مشتری‌های من». They already told us who this is by tapping
    them; asking again would overwrite that customer's label with whatever
    they retype, and a shop retyping a phone from memory is how «رضا» becomes
    two people.
    """
    is_reseller = bool(db_user is not None and db_user.is_reseller)
    data = await state.get_data()
    # A purchase bound to a named service (a renew or a plan switch) keeps
    # that service's name; asking would overwrite it with a retyped answer.
    if data.get("intent_renew_account_id"):
        if session is not None and settings is not None:
            await _after_service_name(message, state, session, settings, db_user, None)
            return
        logger.warning("renew reached the name step with no session")
    if is_reseller:
        from bot.handlers.customer_label import FLOW_PURCHASE, ask_customer_name

        await ask_customer_name(message, state, flow=FLOW_PURCHASE)
        return
    await state.set_state(PurchaseStates.entering_service_name)
    await message.answer(texts.purchase_enter_service_name)


async def continue_with_customer_label(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    *,
    customer: CustomerLabel,
) -> None:
    """Resume checkout once `bot.handlers.customer_label` has both answers.

    The name goes to `service_name`, where it seeds the panel username the same
    way a customer's does. The phone only ever appears in `customer_label`.
    """
    await state.update_data(customer_label=str(customer))
    await _after_service_name(message, state, session, settings, db_user, customer.name)


@router.callback_query(PurchaseStates.choosing_duration, F.data.startswith(PurchaseCB.DAYS_PREFIX))
async def choose_days(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    await callback.answer()
    if callback.message is None:
        return
    if not await _builder_open_for(session, settings, state, db_user):
        await _refuse_custom(callback.message, state, session, db_user)
        return
    days = int((callback.data or "").split(":")[-1])
    await state.update_data(duration_days=days)
    await _ask_service_name(callback.message, state, db_user, session, settings)


@router.callback_query(
    PurchaseStates.choosing_duration, F.data.startswith(PurchaseCB.MONTHS_PREFIX)
)
async def choose_months(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    await callback.answer()
    if callback.message is None:
        return
    if not await _builder_open_for(session, settings, state, db_user):
        await _refuse_custom(callback.message, state, session, db_user)
        return
    months = int((callback.data or "").split(":")[-1])
    await state.update_data(duration_months=months)
    await _ask_service_name(callback.message, state, db_user, session, settings)


async def _after_service_name(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    _settings: Settings,
    db_user: User | None,
    service_name: str | None,
) -> None:
    """``service_name`` is ``None`` when the buyer was never asked for one.

    That happens for a reseller renewing a service they picked by name: leaving
    it unset keeps `Order.service_name` and `Order.customer_label` empty, and
    `_finalize_active` then leaves the existing label alone instead of
    rewriting it from a retyped answer.
    """
    data = await state.get_data()
    intent_renew = data.get("intent_renew_account_id")
    # Preserve quick-renew intent; otherwise reset renew flags for a fresh name entry.
    if intent_renew:
        await state.update_data(
            service_name=service_name,
            renew_account_id=int(intent_renew),
            force_new=False,
        )
        await _continue_to_promo_or_summary(message, state, session, _settings, db_user)
        return

    await state.update_data(service_name=service_name, renew_account_id=None, force_new=False)
    data = await state.get_data()
    plan_type = PlanType(data["plan_type"])
    # Build a temporary plan fingerprint via get_or_create would mutate — use meta.
    if plan_type is PlanType.VOLUME:
        fp = volume_fingerprint(
            volume_gb=int(data["volume_gb"]), duration_days=data.get("duration_days")
        )
    else:
        months = int(data["duration_months"])
        users = int(data["concurrent_users"])
        fp = f"unlimited:{users}:{months * 30}"

    # «تمدید کنم یا یکی جدید بسازم؟» only makes sense when the buyer owns the
    # service in question. A shop owns thirty of them, one per customer, and a
    # match on plan shape says nothing about which walk-in is standing there —
    # so the question is unanswerable and the answer is always «new». They
    # renew from the customer's own card in «مشتری‌های من», where the service
    # is identified by name rather than guessed at from a fingerprint.
    renewable = None
    if db_user is not None and not db_user.is_reseller:
        renewable = await find_renewable_account(session, db_user.id, fp)
    if renewable is not None:
        await state.update_data(matched_renew_account_id=renewable.id)
        await state.set_state(PurchaseStates.choosing_renew_mode)
        await message.answer(texts.purchase_renew_or_new, reply_markup=renew_or_new_keyboard())
        return

    await _continue_to_promo_or_summary(message, state, session, _settings, db_user)


@router.message(PurchaseStates.entering_service_name)
async def service_name_entered(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    raw = (message.text or "").strip()
    try:
        name = sanitize_service_name(raw)
    except ValueError:
        await message.answer(texts.purchase_invalid_service_name)
        return
    if name != raw:
        # The prompt promises «@ و فاصله رو خودم برمی‌دارم». Showing the result
        # is what keeps that a promise rather than a surprise: `Ali-Reza`
        # becomes `ali_reza` and `12ali` becomes `s_12ali`, and both of those
        # turn up in the panel username the customer reads back later.
        await message.answer(texts.purchase_service_name_cleaned.format(name=name))
    await _after_service_name(message, state, session, settings, db_user, name)


@router.message(PurchaseStates.choosing_renew_mode)
async def renew_mode_typed(message: Message) -> None:
    """A typed answer where only the two buttons answer.

    Live: the bot said nothing at all (the fallback stays quiet inside a
    state). The state is left as it is, so the buttons still work.
    """
    await message.answer(texts.purchase_renew_or_new_nudge, reply_markup=renew_or_new_keyboard())


@router.callback_query(PurchaseStates.choosing_renew_mode, F.data == PurchaseCB.RENEW_SAME)
async def renew_same(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    data = await state.get_data()
    await state.update_data(
        renew_account_id=data.get("matched_renew_account_id"),
        force_new=False,
    )
    await callback.answer()
    if callback.message:
        await _continue_to_promo_or_summary(callback.message, state, session, settings, db_user)


@router.callback_query(PurchaseStates.choosing_renew_mode, F.data == PurchaseCB.RENEW_NEW)
async def renew_new(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    await state.update_data(renew_account_id=None, force_new=True)
    await callback.answer()
    if callback.message:
        await _continue_to_promo_or_summary(callback.message, state, session, settings, db_user)


def promo_step_shown(db_user: User | None) -> bool:
    """Is this buyer asked for a promo code?

    A reseller is not: their agreed price replaces retail and checkout drops any
    code for them (`resolve_price`), so asking would promise a discount the bot
    never gives. Same test as the name step and the renew question use.
    """
    return not bool(db_user is not None and db_user.is_reseller)


async def _continue_to_promo_or_summary(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """The one step after the name and the renew question: the promo prompt, or,
    for a buyer who is not asked, the summary as «بدون کد تخفیف» would show it."""
    if promo_step_shown(db_user):
        await state.set_state(PurchaseStates.entering_promo)
        await message.answer(texts.purchase_enter_promo, reply_markup=promo_keyboard())
        return
    await _show_summary(message, state, session, settings, promo_code=None, db_user=db_user)


def _promo_rule_text(exc: PromoRuleError) -> str:
    """The customer's sentence for the rule that refused the code. Never ``str(exc)``."""
    if isinstance(exc, PromoExpired):
        return texts.purchase_promo_expired
    if isinstance(exc, PromoUsedUp):
        return texts.purchase_promo_used_up
    if isinstance(exc, PromoFirstPurchaseOnly):
        return texts.purchase_promo_first_only
    if isinstance(exc, PromoUserLimit):
        return texts.purchase_promo_user_limit
    return texts.purchase_invalid_promo


async def _drop_refused_promo(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User,
    exc: PromoRuleError,
) -> None:
    """The code was fine at the summary but a rule refuses it at the till.

    Say which rule, forget the code and show the summary again at the undiscounted price, so
    the customer can still confirm what they will be charged.
    """
    await message.answer(_promo_rule_text(exc))
    await state.update_data(promo_code=None)
    await _show_summary(message, state, session, settings, promo_code=None, db_user=db_user)


async def _show_summary(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    promo_code: str | None,
    db_user: User | None = None,
) -> None:
    data = await state.get_data()
    plan_type = PlanType(data["plan_type"])
    try:
        price = await quote_checkout(
            session, settings, data, promo_code=promo_code, db_user=db_user
        )
    except PromoRuleError as exc:
        await message.answer(_promo_rule_text(exc), reply_markup=promo_keyboard())
        return
    except ValueError as exc:
        if str(exc) == "invalid_promo_code":
            await message.answer(texts.purchase_invalid_promo, reply_markup=promo_keyboard())
            return
        if str(exc) == "promo_not_applicable":
            await message.answer(texts.purchase_promo_not_applicable, reply_markup=promo_keyboard())
            return
        raise

    wallet_balance = 0
    if db_user is not None:
        try:
            wallet_balance = await get_balance(session, db_user.id)
        except WalletError:
            wallet_balance = 0
    wallet_applied, card_amount = wallet_split(wallet_balance, int(price.final_price))

    await state.update_data(
        promo_code=price.meta.get("promo_code"),
        original_price=price.original_price,
        discount_amount=price.discount_amount,
        final_price=price.final_price,
        price_meta=price.meta,
        wallet_balance=wallet_balance,
        wallet_applied=wallet_applied,
        card_amount=card_amount,
        checkout_delivery=await checkout_delivery_terms(session, settings, data),
    )
    await state.set_state(PurchaseStates.confirming)
    switching = bool(data.get("renew_resets"))
    text = texts.purchase_summary_wallet.format(
        details=_summary_details(
            {**data, "plan_type": plan_type.value},
            await _renewal_line(session, data, db_user),
        ),
        original=_fmt_toman(price.original_price),
        discount=_fmt_toman(price.discount_amount),
        wallet=_fmt_toman(wallet_balance),
        wallet_applied=_fmt_toman(wallet_applied),
        card_amount=_fmt_toman(card_amount),
        final=_fmt_toman(price.final_price),
    )
    offer_percent = price.meta.get("user_offer_percent")
    if offer_percent is not None:
        text = f"{text}\n\n{texts.purchase_personal_offer_line.format(percent=to_persian_digits(int(offer_percent)))}"
    if switching:
        text = f"{text}\n\n{texts.purchase_switch_note}"
    elif data.get("renew_mode") == RENEW_MODE_RESET:
        text = f"{text}\n\n{texts.purchase_renew_reset_note}"
    intent = data.get("intent_renew_account_id")
    await message.answer(
        text,
        reply_markup=confirm_keyboard(
            switch_account_id=int(intent) if intent and not switching else None
        ),
    )


@router.callback_query(PurchaseStates.entering_promo, F.data == PurchaseCB.PROMO_SKIP)
async def promo_skip(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    await callback.answer()
    if callback.message:
        await _show_summary(
            callback.message, state, session, settings, promo_code=None, db_user=db_user
        )


@router.message(PurchaseStates.entering_promo)
async def promo_entered(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    code = (message.text or "").strip()
    if not code:
        await message.answer(texts.purchase_enter_promo, reply_markup=promo_keyboard())
        return
    await _show_summary(message, state, session, settings, promo_code=code, db_user=db_user)


async def _provision_paid_wallet_order(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    *,
    order_id: int,
    user_id: int,
) -> None:
    """Provision an order the wallet has paid in full, and answer the customer.

    One path for a fresh checkout and for the customer's second try after a
    panel failure: ``provision_order`` is idempotent on the stamped panel
    username, so a retry resumes the same order and never debits again.

    ``message`` is the order summary whose confirm was tapped. Whatever the
    outcome, the flow is over, so its buttons go (after the database work,
    before the answer).
    """
    client = await get_pasarguard_client(session, settings)
    try:
        try:
            account, _reward = await provision_order(
                session,
                client,
                order_id=order_id,
            )
        except Exception as exc:
            logger.exception("Wallet provisioning failed for order %s", order_id)
            refunded_amount = await settle_wallet_provision_failure(
                session,
                order_id=order_id,
                original_exception=exc,
                max_n=settings.wallet_provision_fail_credit_max_n,
            )
            if refunded_amount > 0:
                await session.commit()
                await state.clear()
                await _retire_summary_keyboard(message)
                await message.answer(
                    texts.payment_provision_failed_user.format(
                        order_id=to_persian_digits(order_id), amount=_fmt_toman(refunded_amount)
                    ),
                    reply_markup=provision_failed_keyboard(),
                )
                return
            # The panel may have accepted an unknown outcome, or the checkout
            # proof was incomplete. Revert the local failed marker to the
            # committed durable PROVISIONING intent, then say what happened.
            await session.rollback()
            held = await park_unconfirmed_wallet_provision(
                session, order_id=order_id, original_exception=exc
            )
            await state.clear()
            await _retire_summary_keyboard(message)
            if held is None:
                # The rollback took the whole checkout with it: nothing charged.
                await message.answer(texts.purchase_error)
            else:
                await message.answer(
                    texts.purchase_paid_provisioning_pending.format(
                        order_id=to_persian_digits(order_id), amount=_fmt_toman(held)
                    ),
                    reply_markup=provision_failed_keyboard(),
                )
            return
    finally:
        await client.aclose()
    await session.commit()
    await state.clear()
    await _retire_summary_keyboard(message)
    if account is not None:
        try:
            await deliver_provisioned_service_card(
                session,
                settings,
                user_id=user_id,
                account_id=account.id,
                order_id=order_id,
                message=message,
                intro=texts.payment_ready_intro.format(order_id=to_persian_digits(order_id)),
            )
        except Exception:
            logger.exception("Failed to deliver service card after wallet pay order %s", order_id)
            await message.answer(
                texts.payment_wallet_only_ok.format(order_id=to_persian_digits(order_id))
            )
    else:
        await message.answer(
            texts.payment_wallet_only_ok.format(order_id=to_persian_digits(order_id))
        )


async def _retire_summary_keyboard(message: Message) -> None:
    """Take the buttons off an order summary whose confirm was accepted.

    Live: «ثبت سفارش / شروع دوباره / انصراف» stayed under the summary after the
    service was delivered. Called once the order is committed, so a failed
    edit costs nothing; the double-tap guard is the dispatcher's isolation and
    the idempotency keys, not this.
    """
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001 - cosmetic: the order already stands
        logger.info("Could not remove the summary keyboard", exc_info=True)


@router.callback_query(PurchaseStates.confirming, F.data == PurchaseCB.CONFIRM)
async def confirm_order(  # noqa: PLR0911 - callback branches remain intentionally explicit
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    await callback.answer()
    if db_user is None:
        if callback.message:
            await callback.message.answer(texts.purchase_error)
        return
    if callback.message is None:
        return

    # One open order: resume instead of creating another.
    if await resume_open_order_if_any(callback.message, state, session, settings, db_user):
        return

    # Paid from the wallet, never delivered (the panel failed with no proof
    # either way): continue that order. A new one would debit a second time.
    unfinished = await find_unfinished_wallet_order(session, db_user.id)
    if unfinished is not None:
        unfinished_id = int(unfinished.id)
        await callback.message.answer(
            texts.purchase_resume_paid_order.format(order_id=to_persian_digits(unfinished_id))
        )
        await _provision_paid_wallet_order(
            callback.message,
            state,
            session,
            settings,
            order_id=unfinished_id,
            user_id=int(db_user.id),
        )
        return

    # Both unfinished-order branches above stay open; anything past here makes a new order.
    if await refuse_if_panel_down(callback.message, session, settings):
        return

    data = await state.get_data()
    # The builder setting can change after the summary. Reuse its buyer-specific
    # check before a new sale or switch, but never apply that setting to a true
    # renewal of an existing service (which has its own renewal rules).
    renewing = bool(data.get("renew_account_id")) and not (
        data.get("renew_resets") or data.get("force_new")
    )
    if (
        data.get("custom_purchase")
        and not renewing
        and not await _builder_open_for(session, settings, state, db_user)
    ):
        await _refuse_custom(callback.message, state, session, db_user)
        return
    previous_delivery = data.get("checkout_delivery")
    try:
        data = await refresh_ready_checkout(session, data)
    except CheckoutUnavailable:
        await state.clear()
        await callback.message.answer(
            texts.purchase_plan_gone, reply_markup=renew_plan_off_keyboard()
        )
        return
    delivery = await checkout_delivery_terms(session, settings, data)
    await state.update_data(**data)
    plan_type = PlanType(data["plan_type"])
    try:
        price = await quote_checkout(
            session,
            settings,
            data,
            promo_code=data.get("promo_code"),
            db_user=db_user,
        )
    except PromoRuleError as exc:
        # The limit filled or the code expired since the summary.
        await _drop_refused_promo(callback.message, state, session, settings, db_user, exc)
        return
    except ValueError:
        await callback.message.answer(texts.purchase_error)
        return
    try:
        balance = await get_balance(session, db_user.id)
    except WalletError:
        balance = 0
    wallet_applied, card_amount = wallet_split(balance, int(price.final_price))
    shown = (data.get("final_price"), data.get("wallet_applied"), data.get("card_amount"))
    if (previous_delivery is not None and previous_delivery != delivery) or (
        None not in shown and shown != (int(price.final_price), wallet_applied, card_amount)
    ):
        # The customer agreed to the summary they saw. The catalog price, a
        # promo or the wallet balance moved since, so nothing is written:
        # show the new numbers and let them confirm those. (No rollback: it
        # would expire ``db_user``, which the summary reads next.)
        await callback.message.answer(texts.purchase_price_updated)
        await _show_summary(
            callback.message,
            state,
            session,
            settings,
            promo_code=data.get("promo_code"),
            db_user=db_user,
        )
        return
    renew_account_id = data.get("renew_account_id")
    service_name = data.get("service_name")
    customer_label = data.get("customer_label")

    try:
        group_ids = await resolve_groups_for_plan_type(
            session,
            settings,
            plan_type,
            store_category_id=data.get("store_category_id"),
            tier=str(data.get("sel_tier") or DEFAULT_TIER),
        )
        if not group_ids:
            # Nothing to provision onto. Refuse before a plan, an order or a
            # wallet debit exists.
            await session.rollback()
            await callback.message.answer(texts.purchase_group_not_ready)
            return
        if plan_type is PlanType.VOLUME:
            hwid_limit = await _resolve_volume_hwid_limit(session, data)
        else:
            # get_or_create_dynamic_plan replaces this with concurrent_users.
            hwid_limit = int(data.get("concurrent_users") or 1)
        plan = await get_or_create_dynamic_plan(
            session,
            plan_type=plan_type,
            price=price,
            group_ids=group_ids,
            hwid_limit=hwid_limit,
        )
        # If user chose "new" despite match, leave renew_account_id None and
        # temporarily avoid auto-renew by not setting renew id (provision still
        # auto-matches fingerprint). Force new: set renew_account_id None and
        # stamp a sentinel via force_new handled below.
        slug = data.get("store_package_slug")
        order = await create_pending_order(
            session,
            user_id=db_user.id,
            plan=plan,
            price=price,
            service_name=service_name,
            customer_label=customer_label,
            renew_account_id=int(renew_account_id) if renew_account_id else None,
            wallet_applied_toman=wallet_applied,
            force_new=bool(data.get("force_new")),
            store_package_slug=str(slug) if slug else None,
            renew_resets=bool(data.get("renew_resets")),
            renew_mode=data.get("renew_mode"),
            store_category_slug=data.get("store_category_slug"),
            custom_purchase=bool(data.get("custom_purchase")),
        )
        # A ready plan bought from a duration tab: the tab is the delivery truth
        # (None = بدون انقضا). The custom builder never reads the tab, which is
        # still in FSM data from the shelf the customer came through, so a
        # custom order keeps the days it was priced for.
        if slug and "sel_duration_days" in data:
            order.delivery_duration_days = data.get("sel_duration_days")
        if wallet_applied > 0:
            await apply_checkout_debit(session, user_id=db_user.id, order=order)
        await session.flush()

        if card_amount <= 0:
            await approve_wallet_payment(session, order_id=order.id, user_id=db_user.id)
            await session.flush()
            await _provision_paid_wallet_order(
                callback.message,
                state,
                session,
                settings,
                order_id=int(order.id),
                user_id=int(db_user.id),
            )
            return

        await reserve_bank_invoice(session, settings, order_id=order.id)
        await session.commit()
    except OfferNoLongerLive:
        # The personal offer the summary showed was spent, voided or expired between the
        # quote and the write. The order's savepoint rolled back, but the dynamic plan row may
        # be flushed: drop it so no write lock is held across the sends below, then reload the
        # user the summary reads (a rollback expires it).
        await session.rollback()
        await session.refresh(db_user)
        await callback.message.answer(texts.purchase_price_updated)
        await _show_summary(
            callback.message,
            state,
            session,
            settings,
            promo_code=data.get("promo_code"),
            db_user=db_user,
        )
        return
    except PromoRuleError as exc:
        # The recheck in the order's own transaction found the last use gone (a race the
        # quote cannot see). Same discipline as the offer: drop the flushed plan row so no
        # write lock is held across the sends, reload the user the summary reads.
        await session.rollback()
        await session.refresh(db_user)
        await _drop_refused_promo(callback.message, state, session, settings, db_user, exc)
        return
    except InsufficientWalletFunds:
        logger.warning("Wallet short at confirm for user %s", db_user.id)
        await session.rollback()
        await callback.message.answer(texts.purchase_wallet_short)
        return
    except Exception:
        logger.exception("Failed to create pending order")
        await session.rollback()
        await callback.message.answer(texts.purchase_error)
        return

    await _retire_summary_keyboard(callback.message)
    await callback.message.answer(
        texts.purchase_created.format(order_id=to_persian_digits(order.id))
    )
    await begin_payment_wait(
        callback.message,
        state,
        settings,
        session=session,
        order_id=order.id,
        amount_toman=card_amount,
    )

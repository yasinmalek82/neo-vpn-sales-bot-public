"""«🎁 ساخت سرویس برای مشتری»: the owner creates a service for a customer.

Two ways to pay, both through the same door as a customer's own wallet purchase
(``bot/handlers/purchase.py::confirm_order``): a **gift** (price 0) or a debit
from the customer's **own wallet** at the plan's retail price. Nothing here
invents a new money path — it is create → debit → approve → provision, and on a
failure the same ``settle_wallet_provision_failure`` refund.

Idempotency is the reason this module exists as its own file. The confirm
button carries an 8-character request key; the order stores it in a unique
column (``orders.admin_request_key``). A replay — a double tap, a retry after a
lost reply, two taps racing — finds that order by its key and *resumes* it
(approve and provision are idempotent), so it can never create a second service
or take a second debit.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api_client.client import PasarGuardClient
from bot.config import Settings
from database.models import Order, OrderStatus, PasarGuardAccount, User
from database.models.store import StoreCategory, StorePackage
from services.checkout import price_from_fixed
from services.finance import apply_checkout_debit, settle_wallet_provision_failure
from services.orders import create_pending_order, get_open_order, get_or_create_dynamic_plan
from services.panel_settings import resolve_groups_for_plan_type
from services.payments import approve_wallet_payment
from services.pricing import PlanType
from services.provisioning import provision_order
from services.service_names import sanitize_service_name
from services.store_admin import CatalogView, category_off_sale
from services.store_catalog import (
    filter_ready_plans_by_time_limit,
    ready_package_purchase_payload,
)
from services.store_config import get_published_store_version
from services.store_pricing import DEFAULT_TIER, duration_from_package_options, get_price
from services.wallet import InsufficientWalletFunds

logger = logging.getLogger(__name__)

_KEY_RE = re.compile(r"^[0-9a-f]{8}$")


class AdminServiceError(Exception):
    """A refusal or failure with a stable ``code`` the handler maps to Persian."""

    def __init__(self, code: str, *, refunded_toman: int = 0) -> None:
        super().__init__(code)
        self.code = code
        self.refunded_toman = refunded_toman


@dataclass(frozen=True)
class ServiceOffer:
    """One ready plan as it would be created for a customer, priced at retail."""

    package_slug: str
    package_title: str
    category_id: int
    category_title: str
    retail_toman: int
    plan_type: PlanType
    duration_days: int | None
    hwid_limit: int
    group_ids: list[int]
    payload: dict[str, Any]


async def published_catalog_view(session: AsyncSession) -> CatalogView:
    """The **live** catalog as a ``CatalogView``.

    ``get_catalog_view`` prefers a draft, and a draft can hold an edit that no
    customer has ever seen. What the owner creates for a customer must be what
    the shop actually sells, so this reads the published version only. The
    class and its helpers (``unsellable``, slug fallbacks) are the same.
    """
    version = await get_published_store_version(session)
    if version is None:
        return CatalogView(version=None, is_draft=False, categories=[], packages=[], prices=[])
    from services.store_admin import _slug_index

    return CatalogView(
        version=version,
        is_draft=False,
        categories=sorted(version.categories, key=lambda c: (c.sort_order, c.id)),
        packages=sorted(version.packages, key=lambda p: (p.sort_order, p.id)),
        prices=list(version.package_prices or []),
        slug_by_category_id=await _slug_index(session, StoreCategory),
        slug_by_package_id=await _slug_index(session, StorePackage),
    )


def _category_sellable(view: CatalogView, cat: StoreCategory) -> bool:
    """On sale itself, under an on-sale parent, and with a panel group behind it."""
    if category_off_sale(cat):
        return False
    if cat.parent_id is not None:
        parent = view.category(int(cat.parent_id))
        if parent is not None and category_off_sale(parent):
            return False
    return not view.unsellable(cat)


def sellable_groups(view: CatalogView) -> list[StoreCategory]:
    """Groups a plan can be created from: leaves that are on sale, have a panel
    group and at least one ready plan on sale. A container is never listed — its
    children are."""
    groups: list[StoreCategory] = []
    for cat in view.categories:
        if view.children_of(cat.id):
            continue
        if not _category_sellable(view, cat):
            continue
        if not filter_ready_plans_by_time_limit(view.packages_of(cat.id), category=cat):
            continue
        groups.append(cat)
    return groups


def group_label(view: CatalogView, cat: StoreCategory) -> str:
    parent = view.category(int(cat.parent_id)) if cat.parent_id is not None else None
    return f"{parent.title_fa} / {cat.title_fa}" if parent is not None else str(cat.title_fa)


def sellable_plans(view: CatalogView, cat: StoreCategory) -> list[tuple[StorePackage, int | None]]:
    """Ready plans on sale in ``cat`` with their retail price (None = no price)."""
    plans = filter_ready_plans_by_time_limit(view.packages_of(cat.id), category=cat)
    return [(pkg, _retail_price(view, pkg)) for pkg in plans]


def _retail_price(view: CatalogView, pkg: StorePackage) -> int | None:
    """The plan's price cell at its **own** duration. No cell means not for sale:
    the price matrix is the truth, the package's fixed-price column is not read
    outside ``store_admin`` (``test_price_matrix``)."""
    own_days = duration_from_package_options(dict(pkg.options or {}))
    cell = get_price(view.prices, slug=str(pkg.slug), duration_days=own_days, tier=DEFAULT_TIER)
    return None if cell is None else int(cell)


async def resolve_offer(
    session: AsyncSession, settings: Settings, *, package_slug: str, view: CatalogView | None = None
) -> ServiceOffer:
    """Resolve a plan slug into everything creation needs, or refuse.

    Shared by the confirm screen and the creation itself, so the price the owner
    reads is the price that is charged.
    """
    view = view or await published_catalog_view(session)
    pkg = next((p for p in view.packages if str(p.slug) == package_slug), None)
    if pkg is None or not pkg.enabled or not pkg.allow_new_sale:
        raise AdminServiceError("package_gone")
    cat = view.category(int(pkg.category_id))
    if cat is None or not _category_sellable(view, cat):
        raise AdminServiceError("group_off_sale")
    retail = _retail_price(view, pkg)
    if retail is None:
        raise AdminServiceError("no_price")

    payload = ready_package_purchase_payload(
        pkg,
        view.prices,
        duration_days=duration_from_package_options(dict(pkg.options or {})),
        tier=DEFAULT_TIER,
        use_selected_duration=False,
    )
    if payload.get("volume_gb") is not None:
        plan_type = PlanType.VOLUME
    elif payload.get("concurrent_users") is not None:
        plan_type = PlanType.UNLIMITED
    else:
        raise AdminServiceError("incomplete_plan")

    group_ids = await resolve_groups_for_plan_type(
        session, settings, plan_type, store_category_id=int(cat.id), tier=DEFAULT_TIER
    )
    if not group_ids:
        raise AdminServiceError("no_panel_group")

    if plan_type is PlanType.VOLUME:
        # 0 = نامحدود and is a real value. Never `or 1`.
        raw = payload.get("hwid_limit")
        hwid_limit = 0 if raw is None else max(0, int(raw))
    else:
        hwid_limit = int(payload["concurrent_users"])
    return ServiceOffer(
        package_slug=str(pkg.slug),
        package_title=str(pkg.title_fa),
        category_id=int(cat.id),
        category_title=group_label(view, cat),
        retail_toman=retail,
        plan_type=plan_type,
        duration_days=duration_from_package_options(dict(pkg.options or {})),
        hwid_limit=hwid_limit,
        group_ids=[int(g) for g in group_ids],
        payload=payload,
    )


def wallet_option_available(*, balance_toman: int, retail_toman: int, is_reseller: bool) -> bool:
    """«💳 از کیف پول» is offered only when it would go through as a plain sale.

    A reseller buys at an agreed price the purchase flow resolves; charging one
    retail from here would bill them a price they never agreed to. Their wallet
    stays out of this screen and a gift remains available.
    """
    return (not is_reseller) and retail_toman > 0 and int(balance_toman) >= int(retail_toman)


def new_request_key() -> str:
    return secrets.token_hex(4)


async def find_order_by_key(session: AsyncSession, request_key: str) -> Order | None:
    result = await session.execute(select(Order).where(Order.admin_request_key == request_key))
    return result.scalar_one_or_none()


async def create_service_for_customer(
    session: AsyncSession,
    client: PasarGuardClient,
    settings: Settings,
    *,
    user_id: int,
    package_slug: str,
    service_name: str,
    charge_wallet: bool,
    request_key: str,
) -> PasarGuardAccount:
    """Create, pay (gift or the customer's wallet) and provision. Idempotent per key.

    Commits: ``provision_order`` commits its own progress, and this function
    commits the terminal states itself. Raises ``AdminServiceError`` — a refusal
    before anything was written, ``provision_failed[_refunded]`` after.
    """
    if not _KEY_RE.match(request_key or ""):
        raise AdminServiceError("bad_key")

    existing = await find_order_by_key(session, request_key)
    if existing is not None:
        return await _resume(session, client, settings, existing, user_id=user_id)

    user = await session.get(User, user_id)
    if user is None:
        raise AdminServiceError("user_not_found")
    is_reseller = bool(user.is_reseller)
    if charge_wallet and is_reseller:
        raise AdminServiceError("reseller_wallet")
    # `create_pending_order` would hand back the customer's *own* open order.
    if await get_open_order(session, user_id) is not None:
        raise AdminServiceError("open_order")

    try:
        name = sanitize_service_name(service_name)
    except ValueError as exc:
        raise AdminServiceError("invalid_service_name") from exc

    offer = await resolve_offer(session, settings, package_slug=package_slug)
    retail = offer.retail_toman
    payload = dict(offer.payload)
    # A gift is a 0 price, not a special case: `calculate_fixed_price` accepts it.
    payload["fixed_price_toman"] = retail if charge_wallet else 0
    price = price_from_fixed(
        payload, promo_code=None, extra_discount_percent=0, max_discount_percent=None
    )
    charged = int(price.final_price)

    try:
        plan = await get_or_create_dynamic_plan(
            session,
            plan_type=offer.plan_type,
            price=price,
            group_ids=offer.group_ids,
            hwid_limit=offer.hwid_limit,
        )
        order = await create_pending_order(
            session,
            user_id=user_id,
            plan=plan,
            price=price,
            service_name=name,
            force_new=True,
            store_package_slug=offer.package_slug,
            wallet_applied_toman=charged,
            admin_request_key=request_key,
        )
    except IntegrityError:
        # Another call with this key won the insert and is already past "open".
        # Everything this session held was rolled back; look it up afresh.
        await session.rollback()
        winner = await find_order_by_key(session, request_key)
        if winner is None:
            raise AdminServiceError("open_order") from None
        return await _resume(session, client, settings, winner, user_id=user_id)

    if order.admin_request_key != request_key:
        # `create_pending_order` returned an open order that is not ours: the
        # customer opened a checkout between the guard above and the insert.
        await session.rollback()
        raise AdminServiceError("open_order")

    if order.status is OrderStatus.PENDING_PAYMENT:
        # Selected shelf duration is the delivery truth (None = بدون انقضا).
        order.delivery_duration_days = offer.duration_days
    order_id = int(order.id)
    await session.flush()
    return await _settle_and_provision(
        session, client, settings, order_id=order_id, user_id=user_id
    )


async def _resume(
    session: AsyncSession,
    client: PasarGuardClient,
    settings: Settings,
    order: Order,
    *,
    user_id: int,
) -> PasarGuardAccount:
    if int(order.user_id) != int(user_id):
        # The key was minted for another customer's confirm screen.
        raise AdminServiceError("key_conflict")
    if order.status not in {
        OrderStatus.PENDING_PAYMENT,
        OrderStatus.PAYMENT_APPROVED,
        OrderStatus.PROVISIONING,
        OrderStatus.PROVISION_FAILED,
        OrderStatus.ACTIVE,
    }:
        raise AdminServiceError("order_not_resumable")
    return await _settle_and_provision(
        session, client, settings, order_id=int(order.id), user_id=user_id
    )


async def _settle_and_provision(
    session: AsyncSession,
    client: PasarGuardClient,
    settings: Settings,
    *,
    order_id: int,
    user_id: int,
) -> PasarGuardAccount:
    """Debit (if any) → approve → provision, mirroring the wallet purchase path."""
    order = await session.get(Order, order_id)
    if order is None:
        raise AdminServiceError("order_not_resumable")
    try:
        if order.status is OrderStatus.PENDING_PAYMENT:
            # Both steps are idempotent (ledger key, existing verified payment),
            # so a resume after a crash between them lands here safely.
            if int(order.wallet_applied_toman or 0) > 0:
                await apply_checkout_debit(session, user_id=user_id, order=order)
            await session.flush()
            await approve_wallet_payment(session, order_id=order_id, user_id=user_id)
            await session.flush()
    except InsufficientWalletFunds as exc:
        logger.warning("Wallet short creating a service for user %s", user_id)
        await session.rollback()
        raise AdminServiceError("wallet_short") from exc
    except Exception as exc:
        logger.exception("Admin-created service %s failed before provisioning", order_id)
        await session.rollback()
        raise AdminServiceError("create_failed") from exc

    try:
        account, _reward = await provision_order(session, client, order_id=order_id)
    except Exception as exc:
        logger.exception("Admin-created service provisioning failed for order %s", order_id)
        refunded = await settle_wallet_provision_failure(
            session,
            order_id=order_id,
            original_exception=exc,
            max_n=settings.wallet_provision_fail_credit_max_n,
        )
        if refunded > 0:
            await session.commit()
            raise AdminServiceError("provision_failed_refunded", refunded_toman=refunded) from exc
        # The panel may have accepted an unknown outcome, or the checkout proof
        # was incomplete. Revert the local failed marker to the committed durable
        # PROVISIONING intent; the same key resumes it.
        await session.rollback()
        raise AdminServiceError("provision_failed") from exc
    await session.commit()
    return account

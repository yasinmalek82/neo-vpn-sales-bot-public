"""Renew the panel account the customer already has.

Three answers to «📅 تمدید این سرویس», decided here and nowhere else
(:func:`resolve_renew`):

- **a ready plan matches** → today's plan price, and the volume and time are
  **added** to what is left;
- **none matches and it is a volume service** → «renew from scratch»: the
  buyer gives volume and days, pays gigabytes × rate, and the service is set
  anew (the old bot's way, which resellers are used to);
- anything else → the plan switch (``start_switch_flow``).

A group whose «✅ تمدید» switch is off refuses the first two.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api_client import UserResponse
from database.base import end_read_transaction
from database.models import Order, OrderStatus, PasarGuardAccount, User
from database.models.store import StoreCategory, StorePackage
from services import report_events
from services.account_orders import is_trial_account
from services.accounts import get_live_account
from services.pricing import PlanType
from services.report_outbox import enqueue_report
from services.reseller import has_per_gb_rate
from services.store_catalog import (
    custom_purchase_available,
    is_volume_like,
    ready_package_purchase_payload,
)
from services.store_config import PublishedStoreConfig, get_published_store_config
from services.store_pricing import DEFAULT_TIER, duration_from_package_options

_BYTES_PER_GB = 1024**3
# What a renew from scratch starts from when the order says no days.
DEFAULT_RENEW_DAYS = 30


class RenewUnavailable(Exception):
    """The bought plan is off, gone, or not uniquely identifiable.

    The handler tells the customer to buy a new service. Guessing a sibling
    plan (Germany vs Turkey, both ``unlimited:1:30``) would renew the wrong one.
    """


async def renew_checkout_data(
    session: AsyncSession,
    *,
    user_id: int,
    account_id: int,
) -> dict[str, Any]:
    """FSM fields that quote today's price of this account's original plan."""
    account = await get_live_account(session, account_id)
    if account is None or int(account.user_id) != int(user_id):
        raise RenewUnavailable
    order = await _latest_active_order(session, user_id=user_id, username=account.panel_username)
    if order is None:
        raise RenewUnavailable
    cfg = await get_published_store_config(session)
    if cfg is None:
        raise RenewUnavailable
    package = _package_for_order(cfg, order)
    if package is None:
        raise RenewUnavailable
    category = _live_category(cfg, int(package.category_id))
    if category is None:
        raise RenewUnavailable
    # A service bought from the «بدون انقضا» tab renews on that tab. The
    # package's own options may still say 30 days (the owner priced the same
    # plan in two tabs), and following them would quote the 30-day cell and
    # give a no-expiry account an expiry. A slug'd order's snapshot is the tab
    # it was sold from, so a null there is «بدون انقضا», not «unknown».
    no_expiry = bool(order.store_package_slug) and order.delivery_duration_days is None
    payload = ready_package_purchase_payload(
        package,
        list(cfg.prices),
        tier=DEFAULT_TIER,
        duration_days=None,
        use_selected_duration=no_expiry,
    )
    # The price cell is the price. The package column is not a second till.
    if payload.get("fixed_price_toman") is None:
        raise RenewUnavailable
    if payload.get("volume_gb") is not None:
        plan_type = PlanType.VOLUME.value
    elif payload.get("concurrent_users") is not None:
        plan_type = PlanType.UNLIMITED.value
    else:
        raise RenewUnavailable
    if order.delivery_hwid_limit is not None:
        payload["hwid_limit"] = int(order.delivery_hwid_limit)
    payload.update(
        {
            "plan_type": plan_type,
            "store_category_id": int(category.id),
            "sel_duration_days": (
                None if no_expiry else duration_from_package_options(dict(package.options or {}))
            ),
            "sel_tier": DEFAULT_TIER,
            "renew_account_id": int(account.id),
            "intent_renew_account_id": int(account.id),
            "force_new": False,
        }
    )
    return payload


async def _latest_active_order(
    session: AsyncSession, *, user_id: int, username: str
) -> Order | None:
    result = await session.execute(
        select(Order)
        .where(
            Order.user_id == user_id,
            Order.panel_username == username,
            Order.status == OrderStatus.ACTIVE,
        )
        .order_by(Order.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _package_for_order(cfg: PublishedStoreConfig, order: Order) -> StorePackage | None:
    by_id = {int(cat.id): cat for cat in cfg.categories}
    slug = (order.store_package_slug or "").strip()
    if slug:
        package = next((pkg for pkg in cfg.packages if pkg.slug == slug), None)
        if package is None or not _package_is_live(package, by_id):
            return None
        return package
    if order.custom_purchase:
        # A custom order has no plan slug on purpose. Matching it to a ready
        # plan by shape would renew a 50 GB custom service as the 50 GB plan
        # and ADD to it, at a price the buyer never agreed to for a renew.
        return None
    shape = _order_shape(order)
    if shape is None:
        return None
    matches = [
        pkg for pkg in cfg.packages if _package_is_live(pkg, by_id) and _package_shape(pkg) == shape
    ]
    groups = list(order.delivery_group_ids or [])
    if groups:
        located = [
            pkg
            for pkg in matches
            if _category_groups_match(by_id.get(int(pkg.category_id)), groups)
        ]
        return located[0] if len(located) == 1 else None
    return matches[0] if len(matches) == 1 else None


def _package_is_live(package: StorePackage, by_id: dict[int, StoreCategory]) -> bool:
    if not package.enabled:
        return False
    return _live_category_from(by_id, int(package.category_id)) is not None


def _live_category(cfg: PublishedStoreConfig, category_id: int) -> StoreCategory | None:
    by_id = {int(cat.id): cat for cat in cfg.categories}
    return _live_category_from(by_id, category_id)


def _live_category_from(by_id: dict[int, StoreCategory], category_id: int) -> StoreCategory | None:
    category = by_id.get(category_id)
    seen: set[int] = set()
    while category is not None:
        if int(category.id) in seen or not category.enabled:
            return None
        seen.add(int(category.id))
        parent_id = category.parent_id
        if parent_id is None:
            return by_id[category_id]
        category = by_id.get(int(parent_id))
    return None


def _category_groups_match(category: StoreCategory | None, groups: list[int]) -> bool:
    if category is None:
        return False
    return _groups_match([int(item) for item in (category.panel_group_ids or [])], groups)


def _groups_match(own: list[int], groups: list[int]) -> bool:
    """A group's panel groups against the groups an order or a live user carries."""
    wanted = [int(item) for item in groups]
    if not own:
        return False
    return own == wanted or (len(own) == 1 and own[0] in set(wanted))


def _order_shape(order: Order) -> tuple[str, int, int] | None:
    gb = order.delivery_data_limit_gb
    days = order.delivery_duration_days
    if gb is None or days is None:
        return None
    if int(gb) > 0:
        return ("volume", int(gb), int(days))
    if order.delivery_hwid_limit is None:
        return None
    return ("unlimited", int(order.delivery_hwid_limit), int(days))


def _package_shape(package: StorePackage) -> tuple[str, int, int] | None:
    opts = dict(package.options or {})
    days = duration_from_package_options(opts)
    if days is None:
        return None
    if opts.get("volume_gb") is not None:
        return ("volume", int(opts["volume_gb"]), days)
    users = opts.get("concurrent_users", opts.get("user_count"))
    if users is None:
        return None
    return ("unlimited", int(users), days)


# --------------------------------------------------------------------------- #
# The one decision: what does «📅 تمدید این سرویس» do for this service?
# --------------------------------------------------------------------------- #


class RenewKind(str, Enum):
    PLAN = "plan"  # a ready plan matches: today's price, added to what is left
    CUSTOM = "custom"  # renew from scratch: volume and days, gigabytes x rate
    SWITCH = "switch"  # nothing to renew: pick a plan for this same service
    OFF = "off"  # the group's «تمدید» switch is off, or the service is a trial
    REFUSED = "refused"  # not this buyer's service
    NO_GROUP = "no_group"  # a volume service that belongs to no single group
    PANEL_DOWN = "panel_down"  # the live service could not be read


@dataclass(frozen=True)
class CustomRenewOffer:
    """What the renew-from-scratch screen starts from."""

    account_id: int
    category_id: int
    category_slug: str
    volume_gb: int
    duration_days: int
    hwid_limit: int | None = None


@dataclass(frozen=True)
class RenewDecision:
    kind: RenewKind
    payload: dict[str, Any] | None = None  # PLAN: the FSM fields of the ready plan
    custom: CustomRenewOffer | None = None  # CUSTOM


# Reads the live panel user. Only asked for a service with no order, whose
# volume and groups exist nowhere else.
PanelUserLoader = Callable[[str], Awaitable[UserResponse]]


def category_renew_off(cfg: PublishedStoreConfig, category: StoreCategory | None) -> bool:
    """Is renewing off for this group, or for a group above it?

    One function for «✅ تمدید»: the ready-plan renew and the renew from scratch
    both ask it. The owner's own extend (the customer card) does not.
    """
    by_id = {int(cat.id): cat for cat in cfg.categories}
    seen: set[int] = set()
    while category is not None and int(category.id) not in seen:
        seen.add(int(category.id))
        if not bool(dict(category.sales_flags or {}).get("allow_renew", True)):
            return True
        category = by_id.get(int(category.parent_id)) if category.parent_id is not None else None
    return False


def _leaf_volume_categories(cfg: PublishedStoreConfig) -> list[StoreCategory]:
    """Live volume groups that hold plans (no live child group below them)."""
    by_id = {int(cat.id): cat for cat in cfg.categories}
    parents = {
        int(cat.parent_id)
        for cat in cfg.categories
        if cat.parent_id is not None and _live_category_from(by_id, int(cat.id)) is not None
    }
    return [
        cat
        for cat in cfg.categories
        if int(cat.id) not in parents
        and is_volume_like(cat)
        and _live_category_from(by_id, int(cat.id)) is not None
    ]


def _effective_groups(cfg: PublishedStoreConfig, category: StoreCategory) -> list[int]:
    own = [int(g) for g in (category.panel_group_ids or [])]
    if own or category.parent_id is None:
        return own
    parent = next((c for c in cfg.categories if int(c.id) == int(category.parent_id)), None)
    return [int(g) for g in (getattr(parent, "panel_group_ids", None) or [])]


def volume_category_of_service(
    cfg: PublishedStoreConfig, *, slug: str | None, groups: list[int]
) -> StoreCategory | None:
    """The store group a volume service belongs to, or ``None``. Never a silent default.

    In order: the group slug the order was sold from (slug is the identity);
    else the one group whose panel groups match the service's (the order's, or
    the live user's for a service with no order); else the single published
    volume group when exactly one exists.
    """
    leaves = _leaf_volume_categories(cfg)
    wanted = (slug or "").strip()
    if wanted:
        found = next((cat for cat in leaves if cat.slug == wanted), None)
        if found is not None:
            return found
    if groups:
        located = [cat for cat in leaves if _groups_match(_effective_groups(cfg, cat), groups)]
        if len(located) == 1:
            return located[0]
    return leaves[0] if len(leaves) == 1 else None


def _volume_shape_of_order(order: Order) -> tuple[int, int] | None:
    """(volume GB, days) when the order is a volume service, else ``None``."""
    gb = order.delivery_data_limit_gb
    if gb is None or int(gb) <= 0:
        return None
    days = order.delivery_duration_days
    return int(gb), int(days) if days else DEFAULT_RENEW_DAYS


async def buyer_can_be_priced_for_custom(
    session: AsyncSession, cfg: PublishedStoreConfig, user: User
) -> bool:
    """Can a custom volume be priced for this buyer at all?

    A reseller needs a per-GB rate (``has_per_gb_rate``); anyone else needs the
    pricing document's custom volume plan. The document is also what the
    reseller formula compares against, so both need it.

    **The group's «who sees custom» setting does not gate this.** It decides who
    may start a NEW custom purchase; this is the renew of a service the person
    already has. A retail customer of the old bot must be able to renew where
    custom is off for new sales.
    """
    if not custom_purchase_available({"sales_flags": {"kind": "volume"}}, cfg.pricing):
        return False
    if bool(user.is_reseller):
        return await has_per_gb_rate(session, int(user.id))
    return True


async def resolve_renew(  # noqa: PLR0911 - one early answer per kind
    session: AsyncSession,
    *,
    user: User,
    account_id: int,
    load_panel_user: PanelUserLoader,
) -> RenewDecision:
    """The one answer to «📅 تمدید این سرویس».

    ``load_panel_user`` is asked only for a service with no order, which is the
    only place its volume and panel groups can be read. No read snapshot is held
    across it (``end_read_transaction``).
    """
    account = await session.get(PasarGuardAccount, account_id)
    if account is None or int(account.user_id) != int(user.id):
        return RenewDecision(RenewKind.REFUSED)
    if await is_trial_account(session, account):
        return RenewDecision(RenewKind.OFF)
    cfg = await get_published_store_config(session)
    if cfg is None:
        return RenewDecision(RenewKind.SWITCH)

    try:
        payload = await renew_checkout_data(session, user_id=int(user.id), account_id=account_id)
    except RenewUnavailable:
        payload = None
    if payload is not None:
        category = next(
            (c for c in cfg.categories if int(c.id) == int(payload["store_category_id"])), None
        )
        if category_renew_off(cfg, category):
            return RenewDecision(RenewKind.OFF)
        return RenewDecision(RenewKind.PLAN, payload=payload)

    return await _resolve_custom(
        session, cfg, user=user, account=account, load_panel_user=load_panel_user
    )


async def _resolve_custom(  # noqa: PLR0911 - one early answer per kind
    session: AsyncSession,
    cfg: PublishedStoreConfig,
    *,
    user: User,
    account: PasarGuardAccount,
    load_panel_user: PanelUserLoader,
) -> RenewDecision:
    """No ready plan matches: a volume service the buyer can be priced for is renewed
    from scratch, anything else falls back to the plan switch."""
    if not await buyer_can_be_priced_for_custom(session, cfg, user):
        return RenewDecision(RenewKind.SWITCH)

    order = await _latest_active_order(
        session, user_id=int(user.id), username=account.panel_username
    )
    if order is not None:
        shape = _volume_shape_of_order(order)
        if shape is None:
            return RenewDecision(RenewKind.SWITCH)
        volume_gb, days = shape
        slug = order.store_category_slug
        groups = [int(g) for g in (order.delivery_group_ids or [])]
    else:
        await end_read_transaction(session)
        try:
            live = await load_panel_user(account.panel_username)
        except Exception:  # noqa: BLE001 - any panel failure: nothing to read defaults from
            return RenewDecision(RenewKind.PANEL_DOWN)
        limit = int(live.data_limit or 0)
        if limit <= 0:
            return RenewDecision(RenewKind.SWITCH)  # an unlimited service
        volume_gb, days = max(1, round(limit / _BYTES_PER_GB)), DEFAULT_RENEW_DAYS
        slug, groups = None, [int(g) for g in (live.group_ids or [])]

    category = volume_category_of_service(cfg, slug=slug, groups=groups)
    if category is None:
        await _report_no_group(session, user=user, account=account)
        return RenewDecision(RenewKind.NO_GROUP)
    if category_renew_off(cfg, category):
        return RenewDecision(RenewKind.OFF)
    return RenewDecision(
        RenewKind.CUSTOM,
        custom=CustomRenewOffer(
            account_id=int(account.id),
            category_id=int(category.id),
            category_slug=str(category.slug),
            volume_gb=int(volume_gb),
            duration_days=int(days),
        ),
    )


async def _report_no_group(
    session: AsyncSession, *, user: User, account: PasarGuardAccount
) -> None:
    """Tell the owner once (errors topic) that a service has no single group."""
    report = report_events.renew_group_not_found(
        telegram_id=int(user.telegram_id),
        name=user.first_name or user.username,
        username=user.username,
        account_id=int(account.id),
        panel_username=account.panel_username,
        link_user_id=int(user.id),
    )
    await enqueue_report(
        session,
        event_key=report.event_key,
        topic_key=report.topic_key,
        body=report.body,
        link_user_id=report.link_user_id,
    )


def custom_renew_still_allowed(cfg: PublishedStoreConfig, *, category_slug: str) -> bool:
    """A stale «✅ همین» after the owner turned the group's renew off is refused."""
    category = next((c for c in cfg.categories if c.slug == category_slug), None)
    return category is not None and not category_renew_off(cfg, category)

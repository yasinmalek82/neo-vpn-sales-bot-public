"""Order creation helpers for purchase flow + plan fingerprint / renew matching."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import Order, OrderStatus, PasarGuardAccount, Plan
from database.models.plan import DataLimitResetStrategy
from services.accounts import live_account_clause
from services.pricing import PlanType, PriceResult
from services.promo_redemptions import parse_expiry, record_redemption
from services.user_offers import OfferNoLongerLive, attach_offer


def plan_fingerprint(plan: Plan) -> str:
    """Stable fingerprint for same-plan renew detection.

    volume-like: ``volume:{data_limit_gb}:{duration_days}`` when data_limit_gb > 0
    unlimited-like: ``unlimited:{hwid_limit}:{duration_days}`` when data_limit_gb <= 0
    """
    if plan.data_limit_gb > 0:
        return f"volume:{plan.data_limit_gb}:{plan.duration_days}"
    return f"unlimited:{plan.hwid_limit}:{plan.duration_days}"


def volume_fingerprint(*, volume_gb: int, duration_days: int | None) -> str:
    """Fingerprint of a volume purchase before its plan row exists.

    «بدون انقضا» (``None``) is written as ``0``, the marker
    ``get_or_create_dynamic_plan`` stores on the plan row, so the question
    «تمدید کنم یا جدید؟» matches what provisioning stamps on the account.
    """
    return f"volume:{int(volume_gb)}:{0 if duration_days is None else int(duration_days)}"


async def _fingerprint_for_account(session: AsyncSession, account: PasarGuardAccount) -> str | None:
    if account.plan_fingerprint:
        return account.plan_fingerprint
    result = await session.execute(
        select(Order)
        .where(
            Order.panel_username == account.panel_username,
            Order.status == OrderStatus.ACTIVE,
        )
        .options(selectinload(Order.plan))
        .order_by(Order.id.desc())
        .limit(1)
    )
    order = result.scalar_one_or_none()
    if order is None or order.plan is None:
        return None
    return plan_fingerprint(order.plan)


async def _expires_at_for_account(
    session: AsyncSession, account: PasarGuardAccount
) -> datetime | None:
    result = await session.execute(
        select(Order)
        .where(
            Order.panel_username == account.panel_username,
            Order.status == OrderStatus.ACTIVE,
        )
        .order_by(Order.id.desc())
        .limit(1)
    )
    order = result.scalar_one_or_none()
    if order is None or order.expires_at is None:
        return None
    expires = order.expires_at
    if expires.tzinfo is None:
        return expires.replace(tzinfo=UTC)
    return expires.astimezone(UTC)


async def find_renewable_account(
    session: AsyncSession,
    user_id: int,
    fingerprint: str,
) -> PasarGuardAccount | None:
    """Find an existing account matching ``fingerprint`` for same-plan renew.

    Prefers accounts whose latest ACTIVE linked order is still not expired.
    Falls back to any fingerprint match (latest account id).
    """
    result = await session.execute(
        select(PasarGuardAccount)
        .where(PasarGuardAccount.user_id == user_id, live_account_clause())
        .order_by(PasarGuardAccount.id.desc())
    )
    accounts = list(result.scalars().all())
    if not accounts:
        return None

    now = datetime.now(UTC)
    matched: list[tuple[PasarGuardAccount, datetime | None]] = []
    for account in accounts:
        fp = await _fingerprint_for_account(session, account)
        if fp is None or fp != fingerprint:
            continue
        if fp.startswith("trial:"):
            continue
        expires = await _expires_at_for_account(session, account)
        matched.append((account, expires))

    if not matched:
        return None

    not_expired = [(acct, exp) for acct, exp in matched if exp is None or exp > now]
    pool = not_expired or matched
    return max(pool, key=lambda item: item[0].id)[0]


def _plan_variant_name(
    base_name: str, *, price_toman: int, hwid_limit: int, group_ids: list[int]
) -> str:
    """Disambiguate immutable plan rows when catalog knobs change."""
    g = "-".join(str(i) for i in group_ids) or "none"
    raw = f"{base_name}-p{price_toman}-h{hwid_limit}-g{g}"
    return raw[:64]


async def get_or_create_dynamic_plan(
    session: AsyncSession,
    *,
    plan_type: PlanType,
    price: PriceResult,
    group_ids: list[int],
    hwid_limit: int,
    reset_strategy: DataLimitResetStrategy = DataLimitResetStrategy.no_reset,
) -> Plan:
    """Get or create a catalog Plan row without mutating historical delivery specs.

    Existing plans are treated as immutable for price/groups/hwid/limits. When the
    requested specs differ, a new variant row is created. Order-level delivery
    snapshots remain the source of truth for provisioning.
    """
    meta = price.meta
    if plan_type is PlanType.VOLUME:
        volume_gb = int(meta["volume_gb"])
        raw_days = meta.get("duration_days")
        duration_days = 0 if raw_days is None else int(raw_days)
        base_name = f"volume-{volume_gb}GB-{duration_days}d"
        description = (
            f"Dynamic volume plan {volume_gb}GB / no expiry"
            if raw_days is None
            else f"Dynamic volume plan {volume_gb}GB / {duration_days} days"
        )
    else:
        users = int(meta["concurrent_users"])
        months = int(meta["duration_months"])
        volume_gb = 0  # unlimited marker
        duration_days = months * 30
        base_name = f"unlimited-{users}u-{months}m"
        description = f"Dynamic unlimited plan {users} users / {months} months"
        hwid_limit = users

    wanted_groups = list(group_ids)
    wanted_price = int(price.final_price)

    async def _get_by_name(name: str) -> Plan | None:
        result = await session.execute(select(Plan).where(Plan.name == name))
        return result.scalar_one_or_none()

    plan = await _get_by_name(base_name)
    if plan is not None:
        same = (
            int(plan.data_limit_gb) == volume_gb
            and int(plan.duration_days) == duration_days
            and int(plan.price_toman) == wanted_price
            and int(plan.hwid_limit) == int(hwid_limit)
            and list(plan.group_ids or []) == wanted_groups
        )
        if not same:
            variant = _plan_variant_name(
                base_name,
                price_toman=wanted_price,
                hwid_limit=int(hwid_limit),
                group_ids=wanted_groups,
            )
            plan = await _get_by_name(variant)
            base_name = variant
        else:
            plan.is_active = True
            return plan

    if plan is None:
        plan = Plan(
            name=base_name,
            description=description,
            data_limit_gb=volume_gb,
            duration_days=duration_days,
            price_toman=wanted_price,
            data_limit_reset_strategy=reset_strategy,
            group_ids=wanted_groups,
            hwid_limit=hwid_limit,
            is_active=True,
            sort_order=0,
        )
        session.add(plan)
        await session.flush()
    else:
        plan.is_active = True
    return plan


def snapshot_delivery_fields(plan: Plan) -> dict:
    """Capture delivery specs from Plan for Order (does not invent history for old rows)."""
    return {
        "delivery_data_limit_gb": int(plan.data_limit_gb),
        "delivery_duration_days": int(plan.duration_days),
        "delivery_hwid_limit": int(plan.hwid_limit),
        "delivery_group_ids": list(plan.group_ids or []),
    }


def effective_delivery_data_limit_gb(order: Order, plan: Plan | None = None) -> int:
    if order.delivery_data_limit_gb is not None:
        return int(order.delivery_data_limit_gb)
    src = plan or order.plan
    return int(src.data_limit_gb)


def effective_delivery_duration_days(order: Order, plan: Plan | None = None) -> int | None:
    """Return days, or ``None`` when the order is «بدون انقضا».

    A non-null snapshot wins. A null snapshot on a row that still has a plan
    falls through to the plan (legacy). Callers that wrote an explicit
    ``None`` after snapshot (T-006 ready shelf) must also clear the plan
    fallback by setting ``delivery_duration_days`` only — provision checks
    ``order.delivery_duration_days is None and order.delivery_data_limit_gb``
    is not enough. Instead: when price meta carried ``no_expiry``, we rely on
    ``delivery_duration_days is None`` together with ready-plan fixed price;
    for safety provision treats ``None`` from this function as no expiry when
    the plan name ends with ``-0d`` / unlimited marker. Simpler rule used here:
    if the column is None, return None only when ``plan is None``; otherwise
    plan days. T-006 confirm writes the selected int or leaves None and sets
    plan duration to 0 for no-expiry variants.
    """
    if order.delivery_duration_days is not None:
        return int(order.delivery_duration_days)
    src = plan or order.plan
    if src is None:
        return None
    days = int(src.duration_days)
    # Dynamic plan for «بدون انقضا» uses duration_days=0.
    return None if days == 0 else days


def effective_delivery_hwid_limit(order: Order, plan: Plan | None = None) -> int:
    if order.delivery_hwid_limit is not None:
        return int(order.delivery_hwid_limit)
    src = plan or order.plan
    return int(src.hwid_limit)


def effective_delivery_group_ids(order: Order, plan: Plan | None = None) -> list[int]:
    if order.delivery_group_ids is not None:
        return list(order.delivery_group_ids)
    src = plan or order.plan
    return list(src.group_ids or [])


async def create_pending_order(
    session: AsyncSession,
    *,
    user_id: int,
    plan: Plan,
    price: PriceResult,
    service_name: str | None = None,
    customer_label: str | None = None,
    renew_account_id: int | None = None,
    wallet_applied_toman: int = 0,
    force_new: bool = False,
    store_package_slug: str | None = None,
    renew_resets: bool = False,
    renew_mode: str | None = None,
    store_category_slug: str | None = None,
    custom_purchase: bool = False,
    admin_request_key: str | None = None,
) -> Order:
    """Insert the order, or hand back the user's open one.

    A quote that carries ``user_offer_id`` took a personal offer, so the order takes it
    here, in its own savepoint: the guarded UPDATE finds the offer taken, voided or
    expired since the summary and the order is NOT written at the discounted price
    (``OfferNoLongerLive``). An open order handed back attaches nothing.

    A quote that carries ``promo_redeem`` took a promo code, so the redemption row is written
    in the same savepoint and the code's limits are counted again: a limit filled since the
    summary raises the ``PromoRuleError`` and neither the order nor the row exists.
    """
    snap = snapshot_delivery_fields(plan)
    # Read once: the retry below rolls the session back, which expires ``plan``, and a lazy
    # load of ``plan.id`` in async code is a MissingGreenlet.
    plan_id = plan.id
    offer_id = price.meta.get("user_offer_id")
    redeem = price.meta.get("promo_redeem")
    slug = (store_package_slug or "").strip() or None
    category_slug = (store_category_slug or "").strip() or None
    last_locked: OperationalError | None = None
    for _attempt in range(5):
        order = Order(
            user_id=user_id,
            plan_id=plan_id,
            status=OrderStatus.PENDING_PAYMENT,
            price_toman=price.final_price,
            discount_toman=price.discount_amount,
            service_name=service_name,
            customer_label=customer_label,
            renew_account_id=renew_account_id,
            wallet_applied_toman=max(0, int(wallet_applied_toman)),
            force_new=bool(force_new),
            renew_resets=bool(renew_resets) and renew_account_id is not None,
            # A renew mode names an account; without one it means nothing.
            renew_mode=renew_mode if renew_account_id is not None else None,
            store_category_slug=category_slug[:64] if category_slug else None,
            custom_purchase=bool(custom_purchase),
            store_package_slug=slug[:64] if slug else None,
            admin_request_key=admin_request_key,
            **snap,
        )
        try:
            async with session.begin_nested():
                session.add(order)
                await session.flush()
                if offer_id is not None and not await attach_offer(
                    session, offer_id=int(offer_id), user_id=user_id, order_id=int(order.id)
                ):
                    raise OfferNoLongerLive(int(offer_id))
                if isinstance(redeem, dict):
                    await record_redemption(
                        session,
                        code=str(redeem["code"]),
                        user_id=user_id,
                        order_id=int(order.id),
                        per_user_limit=int(redeem.get("per_user_limit") or 0),
                        total_limit=int(redeem.get("total_limit") or 0),
                        counted_from=parse_expiry(redeem.get("counted_from")),
                    )
        except IntegrityError:
            # A concurrent checkout already holds the one open row. The failed
            # insert's transaction may still be looking at the snapshot from
            # before the winner committed, so drop it and read again.
            await session.rollback()
            existing = await get_open_order(session, user_id)
            if existing is not None:
                return existing
            raise
        except OperationalError as exc:
            # SQLite answers a colliding write with "database is locked" before
            # the unique index gets a chance. Wait out the other commit, then
            # either take that open order or try once more.
            last_locked = exc
            await session.rollback()
            existing = await get_open_order(session, user_id)
            if existing is not None:
                return existing
            await asyncio.sleep(0.05)
            continue
        return order
    if last_locked is not None:
        raise last_locked
    raise RuntimeError("create_pending_order made no attempt")


OPEN_ORDER_STATUSES = frozenset({OrderStatus.PENDING_PAYMENT, OrderStatus.PAYMENT_REVIEW})


async def get_open_order(session: AsyncSession, user_id: int) -> Order | None:
    """At most one PENDING_PAYMENT|PAYMENT_REVIEW per user (latest if legacy dupes)."""
    result = await session.execute(
        select(Order)
        .where(Order.user_id == user_id, Order.status.in_(OPEN_ORDER_STATUSES))
        .order_by(Order.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def user_has_open_order(session: AsyncSession, user_id: int) -> bool:
    """True when ``get_open_order`` would find one. Reads the id only, never the row."""
    found = await session.scalar(
        select(Order.id)
        .where(Order.user_id == user_id, Order.status.in_(OPEN_ORDER_STATUSES))
        .limit(1)
    )
    return found is not None


async def order_is_delivered(session: AsyncSession, order_id: int) -> bool:
    """True when the order is already ACTIVE.

    A replayed «✅ تایید» or «تلاش مجدد» re-runs provisioning, which is
    idempotent and hands back the existing account; the caller reads this
    **before** that call so only the one that moves the order to ACTIVE
    sends the customer their card and the referrer their notice.
    """
    status = await session.scalar(select(Order.status).where(Order.id == order_id))
    return status is OrderStatus.ACTIVE


async def user_has_successful_paid_order(session: AsyncSession, user_id: int) -> bool:
    """True if user already has an ACTIVE paid (price > 0) order."""
    result = await session.execute(
        select(Order.id)
        .where(
            Order.user_id == user_id,
            Order.status == OrderStatus.ACTIVE,
            Order.price_toman > 0,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


def trial_fingerprint(*, data_limit_gb: int, duration_days: int) -> str:
    return f"trial:{data_limit_gb}:{duration_days}"

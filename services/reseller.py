"""Reseller pricing: what a mobile shop pays, and the guard that keeps it honest.

Two shapes, because a reseller buys in two ways and one rule cannot cover both:

- **ready plans** carry a fixed price per (slug × duration × tier) cell;
- **custom volume** is an open calculator, so it needs a raw rate —
  toman per GB plus toman per day.

The rate is deliberately *not* a percentage off retail. The owner's reasoning:
when he changes a retail price he does not want reseller margin to move under
him, and he wants to know exactly what he earns per gigabyte.

That independence has a cost, and this module is where it is paid. Retail
volume pricing is `rate_per_gb × GB` minus a duration discount, with **no
per-day term at all**; the reseller formula adds one. Two different shapes means
they cross over somewhere, and at that crossing the reseller would be paying
more than a walk-in customer. `quote_*` therefore charges the **lower** of the
two and reports the crossing, rather than letting the owner find out from an
angry phone call.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ResellerPlanPrice, ResellerPriceOp, ResellerRate, User
from services.pricing import PlanType, calculate_price
from services.store_pricing import DEFAULT_TIER

__all__ = [
    "ResellerError",
    "ResellerQuote",
    "apply_percent_cut",
    "clear_plan_price",
    "get_plan_price",
    "get_rate",
    "has_per_gb_rate",
    "is_reseller",
    "latest_price_op",
    "list_plan_prices",
    "parse_percent_input",
    "percent_cut_preview",
    "quote_custom_volume",
    "quote_ready_plan",
    "round_percent_cut",
    "set_plan_price",
    "set_rate",
    "undo_percent_cut",
]


class ResellerError(Exception):
    """Handler-safe, user-facing reseller failure."""


@dataclass(frozen=True)
class ResellerQuote:
    """What the reseller is charged, and why.

    ``crossed_over`` means the agreed reseller price came out **above** retail
    for this combination and retail was charged instead. It is not an error —
    the sale goes through at the better price — but the owner needs to hear
    about it, because it means a rate needs fixing.
    """

    price_toman: int
    retail_price_toman: int
    reseller_price_toman: int
    crossed_over: bool

    @property
    def margin_toman(self) -> int:
        """What the owner keeps versus what a walk-in would have paid."""
        return self.retail_price_toman - self.price_toman


def _duration_clause(duration_days: int | None) -> Any:
    if duration_days is None:
        return ResellerPlanPrice.duration_days.is_(None)
    return ResellerPlanPrice.duration_days == int(duration_days)


async def is_reseller(session: AsyncSession, user_id: int) -> bool:
    user = await session.get(User, user_id)
    return bool(user and user.is_reseller)


async def get_rate(session: AsyncSession, user_id: int) -> ResellerRate | None:
    result = await session.execute(select(ResellerRate).where(ResellerRate.user_id == user_id))
    return result.scalar_one_or_none()


async def has_per_gb_rate(session: AsyncSession, user_id: int) -> bool:
    """Can this reseller be priced for a custom volume?

    The one test: a per-gigabyte rate above zero. Without it
    ``quote_custom_volume`` would charge retail and a shop would pay it
    unannounced, so neither the custom builder nor a renew from scratch is
    offered. Ask this, never ``get_rate`` directly, so a later general rate for
    every reseller changes one place.
    """
    rate = await get_rate(session, int(user_id))
    return bool(rate is not None and int(rate.price_per_gb_toman or 0) > 0)


async def set_rate(
    session: AsyncSession,
    user_id: int,
    *,
    price_per_gb_toman: int | None = None,
    price_per_day_toman: int | None = None,
    trial_quota_monthly: int | None = None,
    trial_price_toman: int | None = None,
) -> ResellerRate:
    """Upsert one reseller's rate card. ``None`` leaves a field alone."""
    row = await get_rate(session, user_id)
    if row is None:
        row = ResellerRate(user_id=user_id)
        session.add(row)
    for field, value in (
        ("price_per_gb_toman", price_per_gb_toman),
        ("price_per_day_toman", price_per_day_toman),
        ("trial_quota_monthly", trial_quota_monthly),
        ("trial_price_toman", trial_price_toman),
    ):
        if value is None:
            continue
        if int(value) < 0:
            raise ResellerError("مقدار نمی‌تواند منفی باشد.")
        setattr(row, field, int(value))
    await session.flush()
    return row


async def get_plan_price(
    session: AsyncSession,
    user_id: int,
    package_slug: str,
    *,
    duration_days: int | None,
    tier: str,
) -> int | None:
    result = await session.execute(
        select(ResellerPlanPrice.price_toman).where(
            ResellerPlanPrice.user_id == user_id,
            ResellerPlanPrice.package_slug == package_slug,
            _duration_clause(duration_days),
            ResellerPlanPrice.tier == tier,
        )
    )
    price = result.scalar_one_or_none()
    return int(price) if price is not None else None


async def list_plan_prices(
    session: AsyncSession,
    user_id: int,
    *,
    duration_days: int | None,
    tier: str,
) -> dict[str, int]:
    """Agreed prices for one reseller on one duration×tier tab, keyed by slug."""
    result = await session.execute(
        select(ResellerPlanPrice.package_slug, ResellerPlanPrice.price_toman).where(
            ResellerPlanPrice.user_id == user_id,
            _duration_clause(duration_days),
            ResellerPlanPrice.tier == tier,
        )
    )
    return {str(slug): int(price) for slug, price in result.all()}


async def set_plan_price(
    session: AsyncSession,
    user_id: int,
    package_slug: str,
    price_toman: int,
    *,
    duration_days: int | None,
    tier: str,
) -> ResellerPlanPrice:
    if int(price_toman) <= 0:
        raise ResellerError("قیمت باید بزرگ‌تر از صفر باشد.")
    result = await session.execute(
        select(ResellerPlanPrice).where(
            ResellerPlanPrice.user_id == user_id,
            ResellerPlanPrice.package_slug == package_slug,
            _duration_clause(duration_days),
            ResellerPlanPrice.tier == tier,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = ResellerPlanPrice(
            user_id=user_id,
            package_slug=package_slug,
            duration_days=duration_days,
            tier=tier,
            price_toman=int(price_toman),
        )
        session.add(row)
    else:
        row.price_toman = int(price_toman)
    await session.flush()
    return row


async def clear_plan_price(
    session: AsyncSession,
    user_id: int,
    package_slug: str,
    *,
    duration_days: int | None,
    tier: str,
) -> bool:
    """Drop an override so this reseller pays retail for that cell again."""
    result = await session.execute(
        select(ResellerPlanPrice).where(
            ResellerPlanPrice.user_id == user_id,
            ResellerPlanPrice.package_slug == package_slug,
            _duration_clause(duration_days),
            ResellerPlanPrice.tier == tier,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


def _resolve(*, retail: int, reseller: int) -> ResellerQuote:
    """Charge the lower of the two and say whether they crossed."""
    crossed = reseller > retail
    return ResellerQuote(
        price_toman=min(retail, reseller),
        retail_price_toman=retail,
        reseller_price_toman=reseller,
        crossed_over=crossed,
    )


async def quote_ready_plan(
    session: AsyncSession,
    *,
    user_id: int,
    package_slug: str,
    retail_price_toman: int,
    duration_days: int | None,
    tier: str = DEFAULT_TIER,
) -> ResellerQuote:
    """Price one ready-plan cell for one reseller.

    With no agreed price they simply pay retail — which is why a missing row is
    not an error. A shop can be made a reseller today and get its price list
    tomorrow without anything breaking in between.
    """
    agreed = await get_plan_price(
        session,
        user_id,
        package_slug,
        duration_days=duration_days,
        tier=tier,
    )
    if agreed is None:
        return _resolve(retail=int(retail_price_toman), reseller=int(retail_price_toman))
    return _resolve(retail=int(retail_price_toman), reseller=agreed)


async def quote_custom_volume(
    session: AsyncSession,
    cfg: dict[str, Any],
    *,
    user_id: int,
    volume_gb: int,
    duration_days: int,
) -> ResellerQuote:
    """Price a custom volume service for one reseller.

    Retail comes from the normal engine so the comparison is against what a
    customer would really be charged today, discounts and all — not against a
    list price nobody pays.
    """
    retail = calculate_price(
        cfg,
        plan_type=PlanType.VOLUME,
        volume_gb=volume_gb,
        duration_days=duration_days,
    ).final_price

    rate = await get_rate(session, user_id)
    if rate is None or (rate.price_per_gb_toman <= 0 and rate.price_per_day_toman <= 0):
        # No rate card agreed yet — retail, same as any customer.
        return _resolve(retail=retail, reseller=retail)

    reseller = int(rate.price_per_gb_toman) * int(volume_gb) + int(rate.price_per_day_toman) * int(
        duration_days
    )
    return _resolve(retail=retail, reseller=reseller)


def parse_percent_input(raw: str) -> int | None:
    """Accept ``10``, ``۱۰``, ``10%``, ``۱۰٪``. Valid range ``1..90``."""
    text = (raw or "").strip()
    if not text:
        return None
    trans = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    text = text.translate(trans)
    text = text.replace("%", "").replace("٪", "").strip()
    if not text.isdecimal():  # isdigit() lets «²» through and int() then raises
        return None
    value = int(text)
    if value < 1 or value > 90:
        return None
    return value


def round_percent_cut(old: int, pct: int) -> int:
    """``new = (old * (100 - pct)) // 100``, then floor to nearest 1_000 toman."""
    cut = (int(old) * (100 - int(pct))) // 100
    return (cut // 1000) * 1000


def percent_cut_preview(
    agreed: dict[str, int],
    *,
    pct: int,
) -> list[tuple[str, int, int]]:
    """Return ``(slug, before, after)`` for cells that already have an agreed price.

    Cells absent from ``agreed`` are skipped — never seeded from retail.
    """
    out: list[tuple[str, int, int]] = []
    for slug, old in sorted(agreed.items()):
        after = round_percent_cut(int(old), pct)
        if after <= 0:
            continue
        out.append((str(slug), int(old), after))
    return out


async def latest_price_op(
    session: AsyncSession,
    user_id: int,
    *,
    duration_days: int | None,
    tier: str,
    slugs: Collection[str] | None = None,
) -> ResellerPriceOp | None:
    """Newest undo snapshot for a (duration, tier) tab.

    Every group has a tab at a common duration, so the tab alone does not say
    whose cut a snapshot was. With ``slugs`` only a snapshot made entirely of
    those plans counts — the plans on the screen asking to undo.
    """
    clauses = [
        ResellerPriceOp.user_id == user_id,
        ResellerPriceOp.tier == tier,
    ]
    if duration_days is None:
        clauses.append(ResellerPriceOp.duration_days.is_(None))
    else:
        clauses.append(ResellerPriceOp.duration_days == int(duration_days))
    result = await session.execute(
        select(ResellerPriceOp).where(and_(*clauses)).order_by(ResellerPriceOp.id.desc())
    )
    scope = None if slugs is None else set(slugs)
    for op in result.scalars():
        if scope is None or set(op.before_json or {}) <= scope:
            return op
    return None


async def apply_percent_cut(
    session: AsyncSession,
    user_id: int,
    *,
    duration_days: int | None,
    tier: str,
    pct: int,
    changes: list[tuple[str, int, int]],
    created_at: datetime | None = None,
) -> int:
    """Write the percent cut and one undo snapshot. Returns cells changed.

    A flat-list cut spans several durations and so writes several snapshots;
    it passes one ``created_at`` to all of them, which is how its undo tells
    that cut apart from an older one (see ``undo_flat_percent_cut``).
    """
    if int(pct) < 1 or int(pct) > 90:
        raise ResellerError("percent out of range")
    if not changes:
        return 0
    before: dict[str, int | None] = {slug: before for slug, before, _ in changes}
    # One step of undo: an older snapshot of any of these plans would restore a
    # price this cut has already moved past. Another group's snapshot on the
    # same tab shares no plan with this cut and is kept.
    existing = await session.execute(
        select(ResellerPriceOp).where(
            ResellerPriceOp.user_id == user_id,
            ResellerPriceOp.tier == tier,
            (
                ResellerPriceOp.duration_days.is_(None)
                if duration_days is None
                else ResellerPriceOp.duration_days == int(duration_days)
            ),
        )
    )
    for old_op in existing.scalars().all():
        if set(old_op.before_json or {}) & set(before):
            await session.delete(old_op)

    session.add(
        ResellerPriceOp(
            user_id=user_id,
            duration_days=duration_days,
            tier=tier,
            before_json=before,
            created_at=created_at or datetime.now(UTC),
        )
    )
    for slug, _before, after in changes:
        await set_plan_price(
            session,
            user_id,
            slug,
            after,
            duration_days=duration_days,
            tier=tier,
        )
    await session.flush()
    return len(changes)


async def undo_percent_cut(
    session: AsyncSession,
    user_id: int,
    *,
    duration_days: int | None,
    tier: str,
    slugs: Collection[str] | None = None,
) -> int | None:
    """Restore ``before_json`` exactly. ``None`` value deletes the cell.

    ``slugs`` scopes the lookup to the plans on screen (see ``latest_price_op``).
    Returns the number of cells restored, or ``None`` when there is nothing to undo.
    """
    op = await latest_price_op(
        session, user_id, duration_days=duration_days, tier=tier, slugs=slugs
    )
    if op is None:
        return None
    return await _restore_price_op(session, user_id, op)


async def undo_flat_percent_cut(
    session: AsyncSession,
    user_id: int,
    *,
    durations: Collection[int | None],
    tier: str,
    slugs: Collection[str],
) -> int | None:
    """Undo the newest flat-list cut: only the snapshots sharing its timestamp.

    ``slugs`` are the list's plans, so another group's cut on a shared duration
    is never mistaken for this one. An older snapshot on a duration the newest
    cut did not reach is not part of it and stays where it is.
    """
    ops: list[ResellerPriceOp] = []
    for duration_days in dict.fromkeys(durations):
        op = await latest_price_op(
            session, user_id, duration_days=duration_days, tier=tier, slugs=slugs
        )
        if op is not None:
            ops.append(op)
    if not ops:
        return None
    newest = max(op.created_at for op in ops)
    count = 0
    for op in ops:
        if op.created_at == newest:
            count += await _restore_price_op(session, user_id, op)
    return count


async def _restore_price_op(session: AsyncSession, user_id: int, op: ResellerPriceOp) -> int:
    before = dict(op.before_json or {})
    count = 0
    for slug, old in before.items():
        if old is None:
            await clear_plan_price(
                session,
                user_id,
                str(slug),
                duration_days=op.duration_days,
                tier=op.tier,
            )
        else:
            await set_plan_price(
                session,
                user_id,
                str(slug),
                int(old),
                duration_days=op.duration_days,
                tier=op.tier,
            )
        count += 1
    await session.delete(op)
    await session.flush()
    return count

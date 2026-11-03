"""The «upgrade» wording of the volume warning (marketing campaign 4).

Not a message of its own: ``services/volume_warnings.py`` asks here, right before it
sends a warning it has already claimed, whether this service should get the
second wording instead. All of these must hold, and any «no» leaves the standard
wording untouched:

* the campaign is on and ``services/marketing.py::may_nudge`` says yes (so a
  reseller, a banned user or a walk-in customer's service never gets it);
* the service is **burning fast**: at least ``UPGRADE_MIN_TIME_LEFT_PERCENT`` of
  the current period's time is still left. The period starts at ``activated_at``
  of the latest delivered non-trial order that references the service and ends at
  the panel's ``expire``, which the pass has already read. No start or no end: not fast;
* the service's own published store group has an enabled ready package with more
  volume than the current limit. The suggestion is the smallest such package,
  found through the catalog readers and keyed on its slug.

It makes no panel call and writes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Order, OrderStatus, PasarGuardAccount
from database.models.store import StorePackage
from services.accounts import normalize_expire
from services.marketing import UPGRADE_MIN_TIME_LEFT_PERCENT, may_nudge
from services.renewal import volume_category_of_service
from services.store_catalog import filter_ready_plans_by_time_limit
from services.store_config import get_published_store_config

_BYTES_PER_GB = 1024**3


@dataclass(frozen=True)
class UpgradeOffer:
    package_slug: str
    package_title: str
    days_left: int


async def _period_order(session: AsyncSession, account: PasarGuardAccount) -> Order | None:
    """The latest delivered non-trial order that references the service."""
    return (
        await session.scalars(
            select(Order)
            .where(
                or_(
                    Order.panel_username == account.panel_username,
                    Order.renew_account_id == account.id,
                ),
                Order.status == OrderStatus.ACTIVE,
                Order.trial_kind.is_(None),
            )
            .order_by(Order.activated_at.desc().nulls_last(), Order.id.desc())
            .limit(1)
        )
    ).first()


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def burning_fast(start: datetime | None, expire: datetime | None, *, now: datetime) -> int | None:
    """Whole days of time left when at least the threshold of the period remains, else None."""
    if start is None or expire is None:
        return None
    total = (expire - _aware(start)).total_seconds()
    left = (expire - now).total_seconds()
    if total <= 0 or left <= 0:
        return None
    if left * 100 < total * UPGRADE_MIN_TIME_LEFT_PERCENT:
        return None
    return int(left // 86400)


async def _smallest_bigger_package(
    session: AsyncSession, order: Order, *, data_limit_bytes: int
) -> StorePackage | None:
    """The smallest enabled ready package of the service's own group with more volume."""
    cfg = await get_published_store_config(session)
    if cfg is None:
        return None
    category = volume_category_of_service(
        cfg,
        slug=order.store_category_slug,
        groups=[int(g) for g in (order.delivery_group_ids or [])],
    )
    if category is None:
        return None
    ready = filter_ready_plans_by_time_limit(
        [p for p in cfg.packages if int(p.category_id) == int(category.id)], category=category
    )
    bigger = [
        (int(p.options["volume_gb"]), int(p.id), p)
        for p in ready
        if p.options.get("volume_gb") is not None
        and int(p.options["volume_gb"]) * _BYTES_PER_GB > int(data_limit_bytes)
    ]
    if not bigger:
        return None
    return min(bigger, key=lambda item: (item[0], item[1]))[2]


async def upgrade_offer(
    session: AsyncSession,
    *,
    account_id: int,
    user_id: int,
    data_limit_bytes: int,
    expire: Any,
    now: datetime,
) -> UpgradeOffer | None:
    """The package to suggest, or None for the standard wording."""
    if not await may_nudge(session, user_id=user_id, account_id=account_id):
        return None
    account = await session.get(PasarGuardAccount, account_id)
    order = None if account is None else await _period_order(session, account)
    if order is None:
        return None
    days_left = burning_fast(
        order.activated_at,
        normalize_expire(expire) if expire not in (None, 0) else None,
        now=now,
    )
    if days_left is None:
        return None
    package = await _smallest_bigger_package(session, order, data_limit_bytes=data_limit_bytes)
    if package is None:
        return None
    return UpgradeOffer(
        package_slug=str(package.slug),
        package_title=str(package.title_fa),
        days_left=days_left,
    )

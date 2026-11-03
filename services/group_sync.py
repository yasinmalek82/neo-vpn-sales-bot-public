"""Move existing panel users onto a store group's new panel group.

A PasarGuard user gets its ``group_ids`` once, at create time. When the owner
later points a store group at a different panel group, nothing updated the
services already sold, and their links went empty. This module finds those
services and moves them, keeping username, link, volume and expiry.

The panel is the only source of a user's current groups, so selection here is
by store group; the read-first comparison happens per account in
:func:`move_accounts_to_groups`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api_client import UserModify
from api_client.client import PasarGuardClient
from bot.config import Settings
from database.models import Order, OrderStatus, PasarGuardAccount
from services.accounts import live_account_clause
from services.panel_settings import resolve_groups_for_plan_type
from services.pricing import PlanType
from services.store_admin import get_catalog_view
from services.store_catalog import category_kind

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GroupSyncResult:
    moved: int
    already: int
    failed: list[str] = field(default_factory=list)
    # On neither the old nor the new group: a premium tier, or a service the
    # customer already switched elsewhere. Never touched.
    skipped: int = 0


async def accounts_of_category(session: AsyncSession, category_id: int) -> list[PasarGuardAccount]:
    """Panel accounts whose live service belongs to this store group.

    A child group that sets no panel group of its own inherits the parent's, so
    it moves with it. An account belongs by its **latest** ACTIVE order, either
    the plan it bought or the group it took a trial from (trials carry
    ``trial_category_slug``, not a package). Not by any active order: after
    «پلن دیگه روی همین سرویس» the old order stays ACTIVE, and counting it would
    pull a customer back onto the product they paid to leave.
    """
    view = await get_catalog_view(session)
    cat = view.category(category_id)
    if cat is None:
        return []
    affected = [cat] + [c for c in view.children_of(cat.id) if not (c.panel_group_ids or [])]
    cat_ids = {c.id for c in affected}
    cat_slugs = [str(c.slug) for c in affected]
    package_slugs = [str(p.slug) for p in view.packages if p.category_id in cat_ids]

    conditions = []
    if package_slugs:
        conditions.append(Order.store_package_slug.in_(package_slugs))
    if cat_slugs:
        conditions.append(Order.trial_category_slug.in_(cat_slugs))
    if not conditions:
        return []
    rows = await session.execute(
        select(PasarGuardAccount, Order.id, or_(*conditions))
        .join(
            Order,
            (Order.panel_username == PasarGuardAccount.panel_username)
            & (Order.user_id == PasarGuardAccount.user_id),
        )
        .where(Order.status == OrderStatus.ACTIVE, live_account_clause())
        .order_by(PasarGuardAccount.id, Order.id)
    )
    latest: dict[int, tuple[PasarGuardAccount, bool]] = {}
    for account, _order_id, belongs in rows.all():
        latest[int(account.id)] = (account, bool(belongs))  # ordered: last wins
    return [account for account, belongs in latest.values() if belongs]


async def target_groups_for_category(
    session: AsyncSession, settings: Settings, category_id: int
) -> list[int]:
    """The panel groups a new purchase in this store group would get."""
    view = await get_catalog_view(session)
    cat = view.category(category_id)
    if cat is None:
        return []
    kind = category_kind(cat)
    if kind is None and cat.parent_id is not None:
        kind = category_kind(view.category(int(cat.parent_id)))
    plan_type = PlanType.UNLIMITED if kind == "unlimited" else PlanType.VOLUME
    return await resolve_groups_for_plan_type(
        session, settings, plan_type, store_category_id=cat.id
    )


async def move_accounts_to_groups(
    client: PasarGuardClient,
    accounts: list[PasarGuardAccount],
    group_ids: list[int],
    *,
    from_groups: list[int],
) -> GroupSyncResult:
    """Move accounts that the panel shows on ``from_groups`` onto ``group_ids``.

    The panel is the truth about where a user is: only a user on exactly the
    old group moves. A premium-tier user or one already switched to another
    product is on some other group and is left alone. Read first, then write
    the absolute list, so a retry cannot double-apply; one failure never stops
    the rest. An empty target is refused: it would strip every inbound.
    """
    if not group_ids:
        raise ValueError("group_ids must not be empty")
    target = sorted(int(g) for g in group_ids)
    source = sorted(int(g) for g in from_groups)
    moved = 0
    already = 0
    skipped = 0
    failed: list[str] = []
    for account in accounts:
        username = account.panel_username
        try:
            live = await client.get_user(username)
            current = sorted(int(g) for g in (live.group_ids or []))
            if current == target:
                already += 1
                continue
            if current != source:
                skipped += 1
                continue
            await client.modify_user(username, UserModify(group_ids=list(group_ids)))
            moved += 1
        except Exception:
            logger.exception("group move failed for panel user %s", username)
            failed.append(username)
    return GroupSyncResult(moved=moved, already=already, failed=failed, skipped=skipped)

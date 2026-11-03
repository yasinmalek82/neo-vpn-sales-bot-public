"""A realistic published catalog, built through the real store services.

It is the owner's shape: a flat unlimited group, and a volume group that nests
two child groups with their own panel groups. Every plan carries its dimensions
(``volume_gb`` + ``duration_days`` or ``concurrent_users`` + ``duration_months``),
because a plan without them diverts the customer into the custom builder.

    store = await publish_store(app)
    store.plans["special_50"].price        # 150_000
    store.groups["special"].panel_groups   # [2]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from services.store_admin import (
    commit_and_activate,
    create_draft_category,
    create_draft_package,
    set_category_trial,
    update_draft_category,
)

if TYPE_CHECKING:
    from tests.e2e.harness.app import App


@dataclass(frozen=True)
class GroupInfo:
    key: str
    title: str
    kind: str
    panel_groups: list[int]
    parent: str | None = None


@dataclass(frozen=True)
class PlanInfo:
    key: str
    group: str
    title: str
    price: int
    volume_gb: int | None = None
    duration_days: int | None = None
    concurrent_users: int | None = None
    duration_months: int | None = None


@dataclass
class Store:
    groups: dict[str, GroupInfo] = field(default_factory=dict)
    plans: dict[str, PlanInfo] = field(default_factory=dict)
    category_ids: dict[str, int] = field(default_factory=dict)


# Panel groups the fake panel knows: 1 direct, 2 tunnel, 3 premium.
_GROUPS = (
    GroupInfo("unlimited", "نامحدود", "unlimited", [1]),
    GroupInfo("volume", "حجمی", "volume", []),
    GroupInfo("special", "ویژه", "volume", [2], parent="volume"),
    GroupInfo("economy", "اقتصادی", "volume", [3], parent="volume"),
)

_PLANS = (
    PlanInfo(
        "unlimited_1",
        "unlimited",
        "۱ کاربره / ۱ ماه",
        250_000,
        concurrent_users=1,
        duration_months=1,
    ),
    PlanInfo(
        "unlimited_2",
        "unlimited",
        "۲ کاربره / ۱ ماه",
        400_000,
        concurrent_users=2,
        duration_months=1,
    ),
    PlanInfo("special_50", "special", "۵۰ گیگ / ۳۰ روز", 150_000, volume_gb=50, duration_days=30),
    PlanInfo(
        "special_100", "special", "۱۰۰ گیگ / ۹۰ روز", 390_000, volume_gb=100, duration_days=90
    ),
    PlanInfo("economy_50", "economy", "۵۰ گیگ / ۳۰ روز", 90_000, volume_gb=50, duration_days=30),
)


async def publish_store(app: App, *, trial_gb: int = 1, trial_days: int = 1) -> Store:
    """Publish the standard catalog. ``trial_gb=0`` leaves every group without a trial."""
    store = Store()
    async with app.session() as session:
        for group in _GROUPS:
            parent_id = store.category_ids[group.parent] if group.parent else None
            category = await create_draft_category(
                session, title_fa=group.title, kind=group.kind, parent_id=parent_id
            )
            if group.panel_groups:
                await update_draft_category(
                    session, category.id, panel_group_ids=",".join(map(str, group.panel_groups))
                )
            store.category_ids[group.key] = category.id
            store.groups[group.key] = group
        for plan in _PLANS:
            await create_draft_package(
                session,
                category_id=store.category_ids[plan.group],
                title_fa=plan.title,
                fixed_price_toman=plan.price,
                volume_gb=plan.volume_gb,
                duration_days=plan.duration_days,
                concurrent_users=plan.concurrent_users,
                duration_months=plan.duration_months,
            )
            store.plans[plan.key] = plan
        if trial_gb:
            for key in ("unlimited", "special"):
                await set_category_trial(
                    session,
                    store.category_ids[key],
                    data_limit_gb=trial_gb,
                    duration_days=trial_days,
                )
        await commit_and_activate(session, notes="کاتالوگ آزمون")
    return store

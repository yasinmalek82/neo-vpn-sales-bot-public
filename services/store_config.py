"""Store catalog config: seed from pricing.json + published DB loader.

Bootstrap / tests: when no published StoreVersion exists, callers may read
``config/pricing.json`` via ``load_pricing_config``. After a published row
exists, purchase path MUST use the DB document — never silent file fallback.

Owner telegram IDs: explicit ``STORE_OWNER_TELEGRAM_IDS`` only (never auto-first-admin).
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.config import Settings
from database.models.store import (
    StoreCategory,
    StorePackage,
    StorePackageKind,
    StorePackagePrice,
    StoreVersion,
    StoreVersionStatus,
)
from services.pricing import load_pricing_config

DEFAULT_PURCHASE_STEPS: list[dict[str, str]] = [
    {"block": "plan_type"},
    {"block": "volume_or_users"},
    {"block": "duration"},
    {"block": "service_name"},
    {"block": "renew_mode"},
    {"block": "promo"},
    {"block": "confirm"},
    {"block": "receipt"},
]

DEFAULT_SALES_RULES: dict[str, Any] = {
    # Owner: best of promo vs referral/extra (not sum). Engine reads this key.
    "discount_combine": "best_of_promo_vs_extra",
    "renew_show_current_price": True,
    "renew_require_customer_confirm": True,
    # Cannot be toggled off by admin UI later.
    "payment_correctness_locked": True,
    "service_ownership_locked": True,
}


@dataclass(frozen=True)
class PublishedStoreConfig:
    version: StoreVersion
    categories: list[StoreCategory]
    packages: list[StorePackage]
    prices: list[StorePackagePrice]

    @property
    def pricing(self) -> dict[str, Any]:
        doc = self.version.pricing_document
        if not isinstance(doc, dict):
            raise ValueError("published store pricing_document must be a JSON object")
        return doc

    @property
    def sales_rules(self) -> dict[str, Any]:
        rules = self.version.sales_rules or {}
        return dict(rules) if isinstance(rules, dict) else {}

    @property
    def purchase_steps(self) -> list[Any]:
        steps = self.version.purchase_steps or []
        return list(steps) if isinstance(steps, list) else []


def parse_store_owner_telegram_ids(value: object) -> list[int]:
    """Parse STORE_OWNER_TELEGRAM_IDS (comma-separated or list). Empty → no owners."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [int(v) for v in value]
    if isinstance(value, int):
        return [value]
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
        return [int(p) for p in parts]
    raise TypeError("STORE_OWNER_TELEGRAM_IDS must be comma-separated ints")


def get_store_owner_telegram_ids(settings: Settings) -> list[int]:
    return list(settings.store_owner_telegram_ids)


def is_store_owner(settings: Settings, telegram_id: int | None) -> bool:
    if telegram_id is None:
        return False
    owners = get_store_owner_telegram_ids(settings)
    return int(telegram_id) in owners


async def get_published_store_version(session: AsyncSession) -> StoreVersion | None:
    stmt = (
        select(StoreVersion)
        .where(StoreVersion.status == StoreVersionStatus.published)
        .options(
            selectinload(StoreVersion.categories),
            selectinload(StoreVersion.packages),
            selectinload(StoreVersion.package_prices),
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_published_store_config(session: AsyncSession) -> PublishedStoreConfig | None:
    """Return the single published store config, or None if not seeded/published yet."""
    version = await get_published_store_version(session)
    if version is None:
        return None
    categories = sorted(version.categories, key=lambda c: (c.sort_order, c.id))
    packages = sorted(version.packages, key=lambda p: (p.sort_order, p.id))
    prices = list(version.package_prices or [])
    return PublishedStoreConfig(
        version=version, categories=categories, packages=packages, prices=prices
    )


async def load_pricing_config_async(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Load pricing for purchase/calculate_price.

    1. If a published StoreVersion exists → return its ``pricing_document`` (required).
       Never falls back to file in this case.
    2. Else (bootstrap / tests before seed) → read JSON from path/settings.
    """
    published = await get_published_store_version(session)
    if published is not None:
        doc = published.pricing_document
        if not isinstance(doc, dict):
            raise ValueError("published store pricing_document must be a JSON object")
        # Merge sales_rules into a copy so calculate_price can read discount_combine
        # without mutating the persisted ORM JSON.
        out = copy.deepcopy(doc)
        rules = published.sales_rules if isinstance(published.sales_rules, dict) else {}
        if rules:
            out.setdefault("sales_rules", {}).update(rules)
        out["_store_version"] = published.version
        out["_store_version_id"] = published.id
        out["_store_config_source"] = "db"
        return out

    if path is None:
        if settings is None:
            from bot.config import get_settings

            settings = get_settings()
        path = settings.pricing_config_path
    cfg = load_pricing_config(path)
    cfg = copy.deepcopy(cfg)
    cfg["_store_config_source"] = "file"
    return cfg


async def publish_store_version(
    session: AsyncSession, version: StoreVersion, *, notes: str | None = None
) -> StoreVersion:
    """Archive any current published version, then publish ``version``."""
    result = await session.execute(
        select(StoreVersion).where(StoreVersion.status == StoreVersionStatus.published)
    )
    for row in result.scalars().all():
        if row.id == version.id:
            continue
        row.status = StoreVersionStatus.archived
        row.updated_at = datetime.now(UTC)

    version.status = StoreVersionStatus.published
    version.published_at = datetime.now(UTC)
    version.updated_at = datetime.now(UTC)
    if notes is not None:
        version.notes = notes
    await session.flush()
    return version


def _next_version_number(existing_max: int | None) -> int:
    return 1 if existing_max is None else int(existing_max) + 1


async def seed_store_from_pricing_json(
    session: AsyncSession,
    path: str | Path,
    *,
    publish: bool = True,
) -> StoreVersion | None:
    """Idempotent seed: if a published store exists → no-op (return None).

    Otherwise create a draft from ``pricing.json`` (categories volume+unlimited,
    custom_template packages) and optionally publish it.
    """
    existing = await get_published_store_version(session)
    if existing is not None:
        return None

    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("pricing config must be a JSON object")

    # Strip non-engine keys that belong on StoreVersion / categories.
    pricing_document = copy.deepcopy(raw)
    # Dropped on purpose: copying it onto the categories is what hid the owner's
    # own setting behind a seeded [1, 2]. A new group starts with no panel group.
    pricing_document.pop("default_group_ids", None)
    default_hwid = pricing_document.pop("default_hwid_limit_volume", None)

    max_ver = await session.scalar(
        select(StoreVersion.version).order_by(StoreVersion.version.desc())
    )
    version = StoreVersion(
        version=_next_version_number(max_ver),
        status=StoreVersionStatus.draft,
        pricing_document=pricing_document,
        purchase_steps=list(DEFAULT_PURCHASE_STEPS),
        sales_rules=dict(DEFAULT_SALES_RULES),
        source="seed_json",
        notes=f"seeded from {path.name}",
    )
    session.add(version)
    await session.flush()

    vol_plan = raw.get("volume_plan") or {}
    unl_plan = raw.get("unlimited_plan") or {}

    cat_volume = StoreCategory(
        store_version_id=version.id,
        slug="volume",
        title_fa="حجمی",
        enabled=True,
        sort_order=10,
        panel_group_ids=[],
        sales_flags={
            "allow_new_sale": True,
            "allow_renew": True,
            "kind": "volume",
            "plan_type": "volume",
            "time_limit_filter_enabled": False,
            "volume_hwid_limit": (int(default_hwid) if default_hwid is not None else 0),
            "durations": [30],
            "tiers_enabled": False,
            "premium_panel_group_ids": [],
        },
    )
    cat_unlimited = StoreCategory(
        store_version_id=version.id,
        slug="unlimited",
        title_fa="نامحدود",
        enabled=True,
        sort_order=20,
        panel_group_ids=[],
        sales_flags={
            "allow_new_sale": True,
            "allow_renew": True,
            "kind": "unlimited",
            "plan_type": "unlimited",
            "time_limit_filter_enabled": False,
            "durations": [30],
            "tiers_enabled": False,
            "premium_panel_group_ids": [],
        },
    )
    session.add_all([cat_volume, cat_unlimited])
    await session.flush()

    pkg_volume = StorePackage(
        store_version_id=version.id,
        category_id=cat_volume.id,
        slug="volume_custom",
        kind=StorePackageKind.custom_template,
        title_fa="خرید حجمی سفارشی",
        enabled=True,
        sort_order=10,
        fixed_price_toman=None,
        pricing_ref="volume_plan",
        options={
            "volume_options_gb": list(vol_plan.get("volume_options_gb") or []),
            "duration_options_days": list(vol_plan.get("duration_options_days") or []),
        },
        forbid_extra_discount=False,
        allow_new_sale=True,
        allow_renew=True,
        sales_flags={},
    )
    pkg_unlimited = StorePackage(
        store_version_id=version.id,
        category_id=cat_unlimited.id,
        slug="unlimited_custom",
        kind=StorePackageKind.custom_template,
        title_fa="خرید نامحدود سفارشی",
        enabled=True,
        sort_order=10,
        fixed_price_toman=None,
        pricing_ref="unlimited_plan",
        options={
            "user_options": list(unl_plan.get("user_options") or []),
            "duration_options_months": list(unl_plan.get("duration_options_months") or []),
        },
        forbid_extra_discount=False,
        allow_new_sale=True,
        allow_renew=True,
        sales_flags={},
    )
    session.add_all([pkg_volume, pkg_unlimited])
    await session.flush()

    if publish:
        await publish_store_version(session, version)

    await session.flush()
    return version


async def category_title_by_slug(session: AsyncSession, slug: str | None) -> str | None:
    """A live store group's title by slug, or None (no catalog, no such group)."""
    if not slug:
        return None
    cfg = await get_published_store_config(session)
    if cfg is None:
        return None
    return next((str(c.title_fa) for c in cfg.categories if str(c.slug) == slug), None)


async def product_title_by_package_slug(session: AsyncSession, slug: str | None) -> str | None:
    """«group — plan», the way the shelf names a ready plan, or None.

    Keyed on the slug because publishing mints new row ids. None when the
    order has no slug (custom, legacy) or the plan has left the catalog.
    """
    slug = (slug or "").strip()
    if not slug:
        return None
    cfg = await get_published_store_config(session)
    if cfg is None:
        return None
    package = next((p for p in cfg.packages if str(p.slug) == slug), None)
    if package is None:
        return None
    group = next((c for c in cfg.categories if int(c.id) == int(package.category_id)), None)
    if group is None:
        return str(package.title_fa)
    return f"{group.title_fa} — {package.title_fa}"

"""STORE_ADMIN service: draft lifecycle, category/package edits, publish, preview.

Business rules live here — handlers stay thin. Edits apply only to draft versions.
``payment_correctness_locked`` / ``service_ownership_locked`` cannot be turned off.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import flag_modified

from database.models.reseller import ResellerPlanPrice
from database.models.store import (
    StoreCategory,
    StorePackage,
    StorePackageKind,
    StorePackagePrice,
    StoreVersion,
    StoreVersionStatus,
)
from services.store_catalog import (
    ALLOWED_CATEGORY_KINDS,
    CATEGORY_KIND_VOLUME,
    CUSTOM_PURCHASE_EVERYONE,
    CUSTOM_PURCHASE_OFF,
    TRIAL_DAYS_MAX,
    TRIAL_GB_MAX,
    normalize_category_sales_flags,
    plan_order_mode,
    set_package_has_time_limit,
    sort_packages_for_display,
)
from services.store_config import (
    DEFAULT_PURCHASE_STEPS,
    DEFAULT_SALES_RULES,
    get_published_store_version,
    is_store_owner,
    publish_store_version,
    seed_store_from_pricing_json,
)
from services.store_pricing import (
    DEFAULT_TIER,
    category_durations,
    duration_from_package_options,
)
from services.ux_format import to_persian_digits

__all__ = [
    "StoreAdminError",
    "StoreOwnerRequired",
    "StoreOverview",
    "DraftPreview",
    "assert_store_owner",
    "get_store_overview",
    "get_draft_version",
    "get_or_create_draft",
    "CatalogView",
    "get_catalog_view",
    "list_draft_categories",
    "list_draft_packages",
    "move_draft_package",
    "set_category_plan_order_mode",
    "duration_label_fa",
    "set_category_premium_panel_groups",
    "set_category_tiers_enabled",
    "set_category_flat_plan_list",
    "count_prices_for_duration",
    "remove_category_duration",
    "add_category_duration",
    "get_draft_category",
    "get_draft_package",
    "update_draft_category",
    "create_draft_category",
    "delete_draft_category",
    "create_draft_package",
    "delete_draft_package",
    "discard_draft",
    "MAX_CATEGORY_DEPTH",
    "update_draft_package",
    "update_draft_purchase_steps",
    "publish_draft",
    "preview_draft",
    "preview_catalog",
    "seed_store_if_empty",
    "build_published_preview",
    "format_preview_fa",
    "is_store_owner",
    "clone_version_as_draft",
    "load_store_version",
    "locked_sales_rules",
]


MAX_CATEGORY_DEPTH = 2
"""Top-level group → child group. Deeper nesting is refused."""


class StoreAdminError(Exception):
    """User-facing / handler-safe store admin error."""


class StoreOwnerRequired(StoreAdminError):
    """Raised when a write requires STORE_OWNER_TELEGRAM_IDS membership."""


@dataclass(frozen=True)
class StoreOverview:
    published_id: int | None
    published_version: int | None
    published_status: str | None
    draft_id: int | None
    draft_version: int | None
    category_count: int
    package_count: int
    ready_package_count: int
    custom_package_count: int
    has_published: bool
    has_draft: bool


@dataclass(frozen=True)
class DraftPreview:
    draft_id: int
    draft_version: int
    categories: list[dict[str, Any]]
    packages: list[dict[str, Any]]
    pricing_summary: dict[str, Any]
    purchase_steps: list[Any]
    sales_rules: dict[str, Any]


def assert_store_owner(settings: Any, telegram_id: int | None) -> None:
    if not is_store_owner(settings, telegram_id):
        raise StoreOwnerRequired("فقط مالک فروشگاه می‌تواند این عملیات را انجام دهد.")


def _status_value(status: StoreVersionStatus | str) -> str:
    return status.value if isinstance(status, StoreVersionStatus) else str(status)


async def _load_version(session: AsyncSession, version_id: int) -> StoreVersion | None:
    stmt = (
        select(StoreVersion)
        .where(StoreVersion.id == version_id)
        .options(
            selectinload(StoreVersion.categories),
            selectinload(StoreVersion.packages),
            selectinload(StoreVersion.package_prices),
        )
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_draft_version(session: AsyncSession) -> StoreVersion | None:
    """Return the current draft (at most one expected), or None."""
    stmt = (
        select(StoreVersion)
        .where(StoreVersion.status == StoreVersionStatus.draft)
        .options(
            selectinload(StoreVersion.categories),
            selectinload(StoreVersion.packages),
            selectinload(StoreVersion.package_prices),
        )
        .order_by(StoreVersion.version.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_store_overview(session: AsyncSession) -> StoreOverview:
    published = await get_published_store_version(session)
    draft = await get_draft_version(session)
    focus = draft or published
    cats = list(focus.categories) if focus else []
    pkgs = list(focus.packages) if focus else []
    ready = sum(1 for p in pkgs if p.kind == StorePackageKind.ready)
    custom = sum(1 for p in pkgs if p.kind == StorePackageKind.custom_template)
    return StoreOverview(
        published_id=published.id if published else None,
        published_version=published.version if published else None,
        published_status=_status_value(published.status) if published else None,
        draft_id=draft.id if draft else None,
        draft_version=draft.version if draft else None,
        category_count=len(cats),
        package_count=len(pkgs),
        ready_package_count=ready,
        custom_package_count=custom,
        has_published=published is not None,
        has_draft=draft is not None,
    )


def _next_version_number(existing_max: int | None) -> int:
    return 1 if existing_max is None else int(existing_max) + 1


def _locked_sales_rules(rules: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(DEFAULT_SALES_RULES)
    if isinstance(rules, dict):
        out.update(rules)
    # Invariants — cannot be toggled off by admin UI.
    out["payment_correctness_locked"] = True
    out["service_ownership_locked"] = True
    # Purchase never reads these. Leaving them editable would let the store
    # claim it can hide a renewal price or skip customer confirmation.
    out["renew_show_current_price"] = True
    out["renew_require_customer_confirm"] = True
    return out


async def _clone_version_as_draft(session: AsyncSession, source: StoreVersion) -> StoreVersion:
    max_ver = await session.scalar(select(func.max(StoreVersion.version)))
    draft = StoreVersion(
        version=_next_version_number(max_ver),
        status=StoreVersionStatus.draft,
        pricing_document=copy.deepcopy(source.pricing_document or {}),
        purchase_steps=copy.deepcopy(source.purchase_steps or list(DEFAULT_PURCHASE_STEPS)),
        sales_rules=_locked_sales_rules(
            source.sales_rules if isinstance(source.sales_rules, dict) else {}
        ),
        source="admin_clone",
        notes=f"draft cloned from published v{source.version}",
    )
    session.add(draft)
    await session.flush()

    # Map old category id → new category for package re-link.
    cat_id_map: dict[int, int] = {}
    # Parents first, so a child's parent_id can be remapped when it is created.
    ordered = sorted(source.categories, key=lambda c: (c.parent_id is not None, c.sort_order, c.id))
    for cat in ordered:
        new_cat = StoreCategory(
            store_version_id=draft.id,
            parent_id=cat_id_map.get(cat.parent_id) if cat.parent_id else None,
            slug=cat.slug,
            title_fa=cat.title_fa,
            enabled=cat.enabled,
            sort_order=cat.sort_order,
            panel_group_ids=copy.deepcopy(list(cat.panel_group_ids or [])),
            sales_flags=copy.deepcopy(dict(cat.sales_flags or {})),
        )
        session.add(new_cat)
        await session.flush()
        cat_id_map[cat.id] = new_cat.id

    for pkg in sorted(source.packages, key=lambda p: (p.sort_order, p.id)):
        new_cat_id = cat_id_map.get(pkg.category_id)
        if new_cat_id is None:
            continue
        session.add(
            StorePackage(
                store_version_id=draft.id,
                category_id=new_cat_id,
                slug=pkg.slug,
                kind=pkg.kind,
                title_fa=pkg.title_fa,
                enabled=pkg.enabled,
                sort_order=pkg.sort_order,
                fixed_price_toman=pkg.fixed_price_toman,
                pricing_ref=pkg.pricing_ref,
                options=copy.deepcopy(dict(pkg.options or {})),
                forbid_extra_discount=pkg.forbid_extra_discount,
                allow_new_sale=pkg.allow_new_sale,
                allow_renew=pkg.allow_renew,
                sales_flags=copy.deepcopy(dict(pkg.sales_flags or {})),
            )
        )
    for price in list(source.package_prices or []):
        session.add(
            StorePackagePrice(
                store_version_id=draft.id,
                package_slug=price.package_slug,
                duration_days=price.duration_days,
                tier=price.tier,
                price_toman=int(price.price_toman),
            )
        )
    await session.flush()
    loaded = await _load_version(session, draft.id)
    assert loaded is not None
    return loaded


async def get_or_create_draft(session: AsyncSession) -> StoreVersion:
    """Return existing draft, or clone from published, or create empty draft."""
    existing = await get_draft_version(session)
    if existing is not None:
        return existing

    published = await get_published_store_version(session)
    if published is not None:
        return await _clone_version_as_draft(session, published)

    max_ver = await session.scalar(select(func.max(StoreVersion.version)))
    draft = StoreVersion(
        version=_next_version_number(max_ver),
        status=StoreVersionStatus.draft,
        pricing_document={},
        purchase_steps=list(DEFAULT_PURCHASE_STEPS),
        sales_rules=_locked_sales_rules({}),
        source="admin_empty",
        notes="empty draft (seed from pricing.json recommended)",
    )
    session.add(draft)
    await session.flush()
    loaded = await _load_version(session, draft.id)
    assert loaded is not None
    return loaded


@dataclass(frozen=True)
class CatalogView:
    """Read-only catalog snapshot that never creates a draft.

    Nine browse screens used to call ``get_or_create_draft`` and commit, so
    merely *looking* at the catalog produced a draft the operator never asked
    for and burned a version number on every visit.

    ``is_draft`` also drives the «پیش‌نویس v3» / «نسخهٔ فعال v2» badge, which no
    editing screen used to show at all.
    """

    version: StoreVersion | None
    is_draft: bool
    categories: list[StoreCategory]
    packages: list[StorePackage]
    prices: list[StorePackagePrice] = field(default_factory=list)
    # id → slug for rows in *every* version, so a button rendered before the
    # last edit can still be resolved. See :meth:`category`.
    slug_by_category_id: dict[int, str] = field(default_factory=dict)
    slug_by_package_id: dict[int, str] = field(default_factory=dict)

    @property
    def exists(self) -> bool:
        return self.version is not None

    def category(self, category_id: int) -> StoreCategory | None:
        """Resolve an id that may predate the current version.

        Publishing clones the catalog, and a clone mints new row ids while
        carrying each slug across unchanged. Since every edit now publishes, a
        button sitting on the owner's screen holds an id from the version
        before — tapping it found nothing and «پلن‌های این گروه» came back
        empty. The slug is the identity that survives, so fall back to it.
        """
        hit = next((c for c in self.categories if c.id == category_id), None)
        if hit is not None:
            return hit
        slug = self.slug_by_category_id.get(category_id)
        if slug is None:
            return None
        return next((c for c in self.categories if c.slug == slug), None)

    def package(self, package_id: int) -> StorePackage | None:
        """Same id-across-versions resolution as :meth:`category`."""
        hit = next((p for p in self.packages if p.id == package_id), None)
        if hit is not None:
            return hit
        slug = self.slug_by_package_id.get(package_id)
        if slug is None:
            return None
        return next((p for p in self.packages if p.slug == slug), None)

    def children_of(self, category_id: int | None) -> list[StoreCategory]:
        if category_id is None:
            return [c for c in self.categories if c.parent_id is None]
        cat = self.category(category_id)
        if cat is None:
            return []
        return [c for c in self.categories if c.parent_id == cat.id]

    def packages_of(self, category_id: int) -> list[StorePackage]:
        cat = self.category(category_id)
        if cat is None:
            return []
        pkgs = [p for p in self.packages if p.category_id == cat.id]
        return sort_packages_for_display(pkgs, cat)

    def effective_panel_groups(self, cat: StoreCategory) -> list[int]:
        """The group's own panel groups, else its parent's (mirrors resolution)."""
        own = [int(item) for item in (cat.panel_group_ids or [])]
        if own:
            return own
        parent = self.category(int(cat.parent_id)) if cat.parent_id is not None else None
        return [int(item) for item in ((parent.panel_group_ids if parent else None) or [])]

    def unsellable(self, cat: StoreCategory) -> bool:
        """No panel group anywhere it would be sold from.

        A container is never sold from itself — its children are — so it is
        flagged only through them.
        """
        kids = self.children_of(cat.id)
        if kids:
            return any(self.unsellable(kid) for kid in kids)
        return not self.effective_panel_groups(cat)

    def on_sale_without_panel_group(self) -> list[StoreCategory]:
        """Top-level groups offered for sale that cannot provision — the home's ⚠️."""
        return [
            cat
            for cat in self.children_of(None)
            if not category_off_sale(cat) and self.unsellable(cat)
        ]


def category_off_sale(cat: StoreCategory) -> bool:
    flags = cat.sales_flags or {}
    return (not cat.enabled) or (not flags.get("allow_new_sale", True))


async def _slug_index(session: AsyncSession, model: Any) -> dict[int, str]:
    """``{row id: slug}`` across every version.

    Two small columns over a table that holds one row per catalog entry per
    version. It is read on admin screens only, and it is what lets a button
    minted before the last publish still resolve.
    """
    result = await session.execute(select(model.id, model.slug))
    return {int(row_id): str(slug) for row_id, slug in result.all()}


async def get_catalog_view(session: AsyncSession) -> CatalogView:
    """Draft when one exists, else the published catalog, else empty."""
    version = await get_draft_version(session)
    is_draft = version is not None
    if version is None:
        version = await get_published_store_version(session)
    if version is None:
        return CatalogView(version=None, is_draft=False, categories=[], packages=[], prices=[])
    return CatalogView(
        version=version,
        is_draft=is_draft,
        categories=sorted(version.categories, key=lambda c: (c.sort_order, c.id)),
        packages=sorted(version.packages, key=lambda p: (p.sort_order, p.id)),
        prices=list(version.package_prices or []),
        slug_by_category_id=await _slug_index(session, StoreCategory),
        slug_by_package_id=await _slug_index(session, StorePackage),
    )


async def list_draft_categories(session: AsyncSession) -> list[StoreCategory]:
    draft = await get_or_create_draft(session)
    return sorted(draft.categories, key=lambda c: (c.sort_order, c.id))


async def list_draft_packages(
    session: AsyncSession, *, category_id: int | None = None
) -> list[StorePackage]:
    draft = await get_or_create_draft(session)
    pkgs = list(draft.packages)
    if category_id is not None:
        cat = next((c for c in draft.categories if c.id == category_id), None)
        if cat is None:
            # id may be from a prior version — resolve via slug like get_draft_category
            try:
                cat = await get_draft_category(session, category_id)
            except StoreAdminError:
                cat = None
        if cat is None:
            return []
        # Filter by the remapped draft id — a stale button id never matches
        # package.category_id after a publish clones the catalog.
        pkgs = [p for p in pkgs if p.category_id == cat.id]
        return sort_packages_for_display(pkgs, cat)
    return sorted(pkgs, key=lambda p: (p.sort_order, p.id))


async def _slug_of(session: AsyncSession, model: Any, row_id: int) -> str | None:
    """The slug of a row in *any* version — the identity that survives a clone."""
    row = await session.get(model, row_id)
    return str(row.slug) if row is not None else None


async def get_draft_category(session: AsyncSession, category_id: int) -> StoreCategory:
    """Resolve an id that may belong to the *published* version.

    Cloning a version into a draft mints new row ids and carries the slug over,
    so an id rendered into a button before the last edit went live no longer
    exists in the draft. That used to be impossible to hit — the owner opened a
    draft once and edited within it — but every edit now publishes, so the very
    next tap arrives with an id from the version before. Falling back to the
    slug is what keeps the second edit in a row from failing.
    """
    draft = await get_or_create_draft(session)
    for cat in draft.categories:
        if cat.id == category_id:
            return cat
    slug = await _slug_of(session, StoreCategory, category_id)
    if slug is not None:
        for cat in draft.categories:
            if cat.slug == slug:
                return cat
    raise StoreAdminError("گروه محصول یافت نشد؛ شاید همین الان حذف شده.")


async def get_draft_package(session: AsyncSession, package_id: int) -> StorePackage:
    """Same id-across-versions resolution as :func:`get_draft_category`."""
    draft = await get_or_create_draft(session)
    for pkg in draft.packages:
        if pkg.id == package_id:
            return pkg
    slug = await _slug_of(session, StorePackage, package_id)
    if slug is not None:
        for pkg in draft.packages:
            if pkg.slug == slug:
                return pkg
    raise StoreAdminError("پلن یافت نشد؛ شاید همین الان حذف شده.")


MAX_PANEL_GROUPS = 8


def parse_panel_group_ids(raw: str) -> list[int]:
    """Panel group ids typed by the owner, or none.

    The picker is the normal way in; typing is the fallback when the panel
    cannot list its groups. Several are allowed — a store group may sell from
    two panel groups — separated by comma, «،» or space. Duplicates collapse.
    The cap keeps the move button's callback (old ids ride in it) under
    Telegram's 64 bytes.
    """
    parts = [p for p in raw.replace("،", ",").replace(" ", ",").split(",") if p.strip()]
    ids: list[int] = []
    for part in parts:
        text = part.strip()
        if not text.isdecimal() or int(text) < 1:  # isdigit() lets «²» through, int() raises
            raise StoreAdminError("فقط شناسهٔ عددی گروه‌ها رو بفرست، با کاما جدا.")
        if int(text) not in ids:
            ids.append(int(text))
    if len(ids) > MAX_PANEL_GROUPS:
        raise StoreAdminError(f"حداکثر {MAX_PANEL_GROUPS} گروه پنل.")
    return ids


def _parse_group_ids(raw: Any) -> list[int]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return [int(x) for x in raw]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("["):
            parsed = json.loads(text)
            if not isinstance(parsed, list):
                raise StoreAdminError("فرمت JSON گروه‌ها نامعتبر است.")
            return [int(x) for x in parsed]
        parts = [p.strip() for p in text.replace("،", ",").split(",") if p.strip()]
        return [int(p) for p in parts]
    raise StoreAdminError("فرمت شناسه‌های گروه نامعتبر است.")


async def update_draft_category(
    session: AsyncSession,
    category_id: int,
    *,
    title_fa: str | None = None,
    enabled: bool | None = None,
    sort_order: int | None = None,
    panel_group_ids: Any | None = None,
    allow_new_sale: bool | None = None,
    allow_renew: bool | None = None,
    kind: str | None = None,
    time_limit_filter_enabled: bool | None = None,
    custom_purchase_mode: str | None = None,
    reseller_trial: bool | None = None,
) -> StoreCategory:
    cat = await get_draft_category(session, category_id)
    if title_fa is not None:
        cleaned = title_fa.strip()
        if not cleaned:
            raise StoreAdminError("عنوان گروه محصول خالی است.")
        if len(cleaned) > 128:
            raise StoreAdminError("عنوان گروه محصول بیش از حد بلند است.")
        cat.title_fa = cleaned
    if enabled is not None:
        cat.enabled = bool(enabled)
    if sort_order is not None:
        cat.sort_order = int(sort_order)
    if panel_group_ids is not None:
        cat.panel_group_ids = _parse_group_ids(panel_group_ids)
    flags = dict(cat.sales_flags or {})
    if allow_new_sale is not None:
        flags["allow_new_sale"] = bool(allow_new_sale)
    if allow_renew is not None:
        flags["allow_renew"] = bool(allow_renew)
    try:
        flags = normalize_category_sales_flags(
            flags,
            kind=kind,
            time_limit_filter_enabled=time_limit_filter_enabled,
            custom_purchase_mode=custom_purchase_mode,
            reseller_trial=reseller_trial,
        )
    except ValueError as exc:
        raise StoreAdminError("نوع گروه باید volume یا unlimited باشد.") from exc
    cat.sales_flags = flags
    cat.updated_at = datetime.now(UTC)
    draft = await get_draft_version(session)
    if draft is not None:
        draft.updated_at = datetime.now(UTC)
        draft.sales_rules = _locked_sales_rules(
            draft.sales_rules if isinstance(draft.sales_rules, dict) else {}
        )
    await session.flush()
    return cat


async def create_draft_category(
    session: AsyncSession,
    *,
    title_fa: str,
    kind: str,
    sort_order: int | None = None,
    parent_id: int | None = None,
) -> StoreCategory:
    """Create a product group on the draft, optionally under a parent group.

    Nesting stops at :data:`MAX_CATEGORY_DEPTH`, so «حجمی → ویژه» is allowed but
    «حجمی → ویژه → طلایی» is not: a customer must never tap more than three
    times to see a price.
    """
    cleaned = title_fa.strip()
    if not cleaned:
        raise StoreAdminError("عنوان گروه محصول خالی است.")
    if len(cleaned) > 128:
        raise StoreAdminError("عنوان گروه محصول بیش از حد بلند است.")
    k = str(kind).strip().lower()
    if k not in ALLOWED_CATEGORY_KINDS:
        raise StoreAdminError("نوع گروه باید volume یا unlimited باشد.")
    draft = await get_or_create_draft(session)
    resolved_parent_id: int | None = None
    if parent_id is not None:
        parent = await get_draft_category(session, int(parent_id))
        if parent.parent_id is not None:
            raise StoreAdminError("زیرگروه فقط یک سطح مجاز است؛ این گروه خودش زیرگروه است.")
        if any(p.category_id == parent.id for p in (draft.packages or [])):
            raise StoreAdminError(
                "این گروه پلن مستقیم دارد. اول پلن‌ها را جابه‌جا یا حذف کنید، "
                "بعد زیرگروه بسازید."
            )
        # parent_id may be from a prior published version (button / FSM). The
        # remapped draft row's id is what children_of and FK both need.
        resolved_parent_id = int(parent.id)
    base_slug = k
    slug = _unique_slug(base_slug, await _taken_category_slugs(session))
    max_sort = max((int(c.sort_order) for c in (draft.categories or [])), default=0)
    order = int(sort_order) if sort_order is not None else max_sort + 10
    flags = normalize_category_sales_flags(
        {
            "allow_new_sale": True,
            "allow_renew": True,
        },
        kind=k,
        time_limit_filter_enabled=False,
        # An unlimited group never offers custom (`custom_purchase_offered`):
        # a setting nothing reads is the inert control this project refuses.
        custom_purchase_mode=(
            CUSTOM_PURCHASE_EVERYONE if k == CATEGORY_KIND_VOLUME else CUSTOM_PURCHASE_OFF
        ),
    )
    cat = StoreCategory(
        store_version_id=draft.id,
        parent_id=resolved_parent_id,
        slug=slug,
        title_fa=cleaned,
        enabled=True,
        sort_order=order,
        panel_group_ids=[],
        sales_flags=flags,
    )
    session.add(cat)
    draft.updated_at = datetime.now(UTC)
    await session.flush()
    session.expire(draft, ["categories"])
    return cat


async def delete_draft_category(session: AsyncSession, category_id: int) -> None:
    """Remove a group from the draft, with its child groups and their plans.

    Only the draft is touched; the published catalog customers see is unchanged
    until the next publish. Historical orders keep their own price and are not
    affected by catalog edits.
    """
    draft = await get_or_create_draft(session)
    cat = await get_draft_category(session, category_id)
    remaining = [c for c in (draft.categories or []) if c.id != cat.id and c.parent_id is None]
    if cat.parent_id is None and not remaining:
        raise StoreAdminError("آخرین گروه اصلی حذف نمی‌شود؛ فروشگاه بدون گروه منتشر نمی‌شود.")
    await session.delete(cat)
    draft.updated_at = datetime.now(UTC)
    await session.flush()
    session.expire(draft)


async def discard_draft(session: AsyncSession) -> None:
    """Throw the draft away and go back to the published catalog.

    Without this the only exits from a draft were publishing it or restoring an
    archived version — there was no way to abandon a half-finished edit.
    """
    draft = await get_draft_version(session)
    if draft is None:
        raise StoreAdminError("پیش‌نویسی برای دور ریختن وجود ندارد.")
    await session.delete(draft)
    await session.flush()


def _unique_slug(base: str, taken: set[str]) -> str:
    slug = base
    n = 2
    while slug in taken:
        slug = f"{base}-{n}"
        n += 1
    return slug


async def _taken_package_slugs(session: AsyncSession) -> set[str]:
    """Every package identity that must never be minted again.

    Prices key on slug across versions and reseller overrides, so a deleted
    draft plan's name is still live money until those rows are gone — and even
    then a published version may still hold the row.
    """
    pkg_rows = await session.execute(select(StorePackage.slug).distinct())
    price_rows = await session.execute(select(StorePackagePrice.package_slug).distinct())
    reseller_rows = await session.execute(select(ResellerPlanPrice.package_slug).distinct())
    return (
        {str(s) for s in pkg_rows.scalars().all()}
        | {str(s) for s in price_rows.scalars().all()}
        | {str(s) for s in reseller_rows.scalars().all()}
    )


async def _taken_category_slugs(session: AsyncSession) -> set[str]:
    """Category slugs prefix every plan slug; recycling one recreates collisions."""
    rows = await session.execute(select(StoreCategory.slug).distinct())
    return {str(s) for s in rows.scalars().all()}


# Upper bounds are sanity rails, not product policy: they exist so a price typed
# into a size prompt («60000») is refused instead of becoming a 60000 GB plan.
DIM_LIMITS: dict[str, tuple[int, int]] = {
    "volume_gb": (1, 10_000),
    "duration_days": (1, 3_650),
    "concurrent_users": (1, 1_000),
    "duration_months": (1, 120),
    "hwid_limit": (0, 1_000),
}


def _positive_dim(value: int | None, label: str, *, key: str | None = None) -> int | None:
    """Plan dimensions are real quantities: absent is fine, absurd is not."""
    if value is None:
        return None
    number = int(value)
    low, high = DIM_LIMITS.get(key or "", (1, 10**9))
    if number < low or number > high:
        raise StoreAdminError(f"{label} باید بین {low} و {high} باشد.")
    return number


DIM_LABELS: dict[str, str] = {
    "volume_gb": "حجم",
    "duration_days": "مدت (روز)",
    "concurrent_users": "تعداد کاربر",
    "duration_months": "مدت (ماه)",
    "hwid_limit": "تعداد کاربر همزمان",
}


def check_dim(key: str, value: int | None) -> int | None:
    """One plan dimension against ``DIM_LIMITS``; raises with the allowed range.

    The wizard asks this at each question, and the writes below ask it again:
    one table, so a value the question accepts is never refused at the confirm.
    """
    return _positive_dim(value, DIM_LABELS[key], key=key)


def _hwid_dim(value: int | None) -> int | None:
    """Device limit: 0 means نامحدود. Absent is fine; negative is not."""
    return check_dim("hwid_limit", value)


async def create_draft_package(
    session: AsyncSession,
    *,
    category_id: int,
    title_fa: str,
    fixed_price_toman: int,
    volume_gb: int | None = None,
    duration_days: int | None = None,
    concurrent_users: int | None = None,
    duration_months: int | None = None,
    hwid_limit: int | None = None,
    options: dict[str, Any] | None = None,
) -> StorePackage:
    """Create a ready plan with an explicit price and real dimensions.

    The dimensions are the point: a title is only a label, so a plan created
    with just «۵۰ گیگ / ۱ ماه» and a price told the purchase flow nothing about
    50 GB or 30 days. ``choose_ready_plan`` treats missing dimensions as
    "incomplete" and diverts the customer into the dynamic گیگ/ماه builder,
    which throws the fixed price away — the plan is unbuyable as specified.
    """
    cleaned = title_fa.strip()
    if not cleaned:
        raise StoreAdminError("عنوان پلن خالی است.")
    if len(cleaned) > 128:
        raise StoreAdminError("عنوان پلن بیش از حد بلند است.")
    price = int(fixed_price_toman)
    if price <= 0:
        # A free service is the trial feature, not a zero-priced catalog plan.
        raise StoreAdminError("قیمت باید بزرگ‌تر از صفر باشد.")
    gb = check_dim("volume_gb", volume_gb)
    days = check_dim("duration_days", duration_days)
    users = check_dim("concurrent_users", concurrent_users)
    months = check_dim("duration_months", duration_months)
    devices = _hwid_dim(hwid_limit)
    draft = await get_or_create_draft(session)
    cat = await get_draft_category(session, int(category_id))
    if any(c.parent_id == cat.id for c in (draft.categories or [])):
        raise StoreAdminError("این گروه زیرگروه دارد؛ پلن را داخل یکی از زیرگروه‌ها بسازید.")
    taken = await _taken_package_slugs(session)
    max_sort = max(
        (int(p.sort_order) for p in (draft.packages or []) if p.category_id == cat.id),
        default=0,
    )
    opts: dict[str, Any] = dict(options or {})
    for key, value in (
        ("volume_gb", gb),
        ("duration_days", days),
        ("concurrent_users", users),
        ("duration_months", months),
    ):
        if value is not None:
            opts[key] = value
    if devices is not None:
        opts["hwid_limit"] = devices
    elif gb is not None and "hwid_limit" not in opts:
        # Volume plans default to no device limit when the wizard omits it.
        opts["hwid_limit"] = 0
    # A plan that expires is time-limited. Without this flag the category's
    # «فیلتر محدودیت زمانی» step hides every plan the owner just created.
    has_expiry = days is not None or months is not None
    pkg = StorePackage(
        store_version_id=draft.id,
        category_id=cat.id,
        slug=_unique_slug(f"{cat.slug}-plan", taken),
        kind=StorePackageKind.ready,
        title_fa=cleaned,
        enabled=True,
        sort_order=max_sort + 10,
        fixed_price_toman=price,
        pricing_ref=None,
        options=opts,
        forbid_extra_discount=False,
        allow_new_sale=True,
        allow_renew=True,
        sales_flags={"has_time_limit": has_expiry},
    )
    session.add(pkg)
    draft.updated_at = datetime.now(UTC)
    await session.flush()
    await _upsert_ready_economy_price(
        session,
        store_version_id=int(pkg.store_version_id),
        slug=str(pkg.slug),
        options=dict(pkg.options or {}),
        price_toman=pkg.fixed_price_toman,
    )
    await session.flush()
    session.expire(draft, ["packages"])
    return pkg


async def delete_draft_package(session: AsyncSession, package_id: int) -> None:
    """Remove one plan from the draft. Customers see it until the next publish."""
    draft = await get_or_create_draft(session)
    pkg = await get_draft_package(session, package_id)
    # Drop this draft version's matrix cells only — never reseller_plan_prices,
    # and never another version's rows (published catalog stays put).
    await session.execute(
        delete(StorePackagePrice).where(
            StorePackagePrice.store_version_id == int(pkg.store_version_id),
            StorePackagePrice.package_slug == str(pkg.slug),
        )
    )
    await session.delete(pkg)
    draft.updated_at = datetime.now(UTC)
    await session.flush()
    session.expire(draft)


async def update_draft_package(
    session: AsyncSession,
    package_id: int,
    *,
    title_fa: str | None = None,
    enabled: bool | None = None,
    sort_order: int | None = None,
    fixed_price_toman: int | None = None,
    forbid_extra_discount: bool | None = None,
    allow_new_sale: bool | None = None,
    allow_renew: bool | None = None,
    clear_fixed_price: bool = False,
    has_time_limit: bool | None = None,
    volume_gb: int | None = None,
    duration_days: int | None = None,
    concurrent_users: int | None = None,
    duration_months: int | None = None,
    clear_duration: bool = False,
) -> StorePackage:
    pkg = await get_draft_package(session, package_id)
    dims = {
        "volume_gb": check_dim("volume_gb", volume_gb),
        "duration_days": check_dim("duration_days", duration_days),
        "concurrent_users": check_dim("concurrent_users", concurrent_users),
        "duration_months": check_dim("duration_months", duration_months),
    }
    if any(v is not None for v in dims.values()) or clear_duration:
        opts = dict(pkg.options or {})
        for key, value in dims.items():
            if value is not None:
                opts[key] = value
        if clear_duration:
            opts.pop("duration_days", None)
            opts.pop("duration_months", None)
        pkg.options = opts
        # Keep the time-limit flag honest: it is derived, not independently set.
        flags = dict(pkg.sales_flags or {})
        flags["has_time_limit"] = (
            opts.get("duration_days") is not None or opts.get("duration_months") is not None
        )
        pkg.sales_flags = flags
    if title_fa is not None:
        cleaned = title_fa.strip()
        if not cleaned:
            raise StoreAdminError("عنوان پلن خالی است.")
        if len(cleaned) > 128:
            raise StoreAdminError("عنوان پلن بیش از حد بلند است.")
        pkg.title_fa = cleaned
    if enabled is not None:
        pkg.enabled = bool(enabled)
    if sort_order is not None:
        pkg.sort_order = int(sort_order)
    if clear_fixed_price:
        pkg.fixed_price_toman = None
    elif fixed_price_toman is not None:
        if pkg.kind != StorePackageKind.ready:
            raise StoreAdminError("فقط پلن‌های آماده قیمت ثابت دارند.")
        if int(fixed_price_toman) < 0:
            raise StoreAdminError("قیمت نمی‌تواند منفی باشد.")
        pkg.fixed_price_toman = int(fixed_price_toman)
    if forbid_extra_discount is not None:
        pkg.forbid_extra_discount = bool(forbid_extra_discount)
    if allow_new_sale is not None:
        pkg.allow_new_sale = bool(allow_new_sale)
    if allow_renew is not None:
        pkg.allow_renew = bool(allow_renew)
    if has_time_limit is not None:
        if pkg.kind != StorePackageKind.ready:
            raise StoreAdminError("تگ محدودیت زمانی فقط برای پلن آماده است.")
        flags, opts = set_package_has_time_limit(
            pkg.sales_flags, pkg.options, has_time_limit=bool(has_time_limit)
        )
        pkg.sales_flags = flags
        pkg.options = opts
    pkg.updated_at = datetime.now(UTC)
    draft = await get_draft_version(session)
    if draft is not None:
        draft.updated_at = datetime.now(UTC)
    await session.flush()
    kind_val = pkg.kind.value if hasattr(pkg.kind, "value") else str(pkg.kind)
    if kind_val == "ready":
        await _upsert_ready_economy_price(
            session,
            store_version_id=int(pkg.store_version_id),
            slug=str(pkg.slug),
            options=dict(pkg.options or {}),
            price_toman=pkg.fixed_price_toman,
        )
        await session.flush()
    return pkg


async def update_draft_purchase_steps(session: AsyncSession, steps: list[Any]) -> StoreVersion:
    """Replace purchase_steps on draft (composable ready blocks)."""
    if not isinstance(steps, list) or not steps:
        raise StoreAdminError("لیست گام‌های خرید خالی یا نامعتبر است.")
    draft = await get_or_create_draft(session)
    normalized: list[Any] = []
    for item in steps:
        if isinstance(item, str):
            normalized.append({"block": item.strip(), "enabled": True})
        elif isinstance(item, dict) and item.get("block"):
            normalized.append(
                {
                    "block": str(item["block"]).strip(),
                    "enabled": bool(item.get("enabled", True)),
                }
            )
        else:
            raise StoreAdminError("هر گام باید شناسهٔ معتبر داشته باشد.")
    draft.purchase_steps = normalized
    draft.updated_at = datetime.now(UTC)
    await session.flush()
    return draft


async def publish_draft(session: AsyncSession, *, notes: str | None = None) -> StoreVersion:
    draft = await get_draft_version(session)
    if draft is None:
        raise StoreAdminError("پیش‌نویسی برای انتشار وجود ندارد.")
    if not draft.categories:
        raise StoreAdminError("پیش‌نویس بدون دسته قابل انتشار نیست. ابتدا seed کنید.")
    draft.sales_rules = _locked_sales_rules(
        draft.sales_rules if isinstance(draft.sales_rules, dict) else {}
    )
    published = await publish_store_version(session, draft, notes=notes)
    await session.flush()
    return published


def _touch_sales_flags(cat: StoreCategory) -> None:
    """Mark JSON ``sales_flags`` dirty for SQLAlchemy; no-op on plain stand-ins."""
    if hasattr(cat, "_sa_instance_state"):
        flag_modified(cat, "sales_flags")


def _freeze_plan_sort_orders(
    packages: list[StorePackage], category: StoreCategory
) -> list[StorePackage]:
    """Write current visual order into sort_order gaps of 10 (manual starting point)."""
    ordered = sort_packages_for_display(packages, category)
    for i, pkg in enumerate(ordered):
        pkg.sort_order = (i + 1) * 10
    return ordered


async def set_category_plan_order_mode(
    session: AsyncSession, category_id: int, *, mode: str
) -> StoreCategory:
    """Set ``sales_flags.plan_order_mode`` to ``auto`` or ``manual``.

    Switching to manual freezes the current visual order into ``sort_order``
    first so the first arrow tap cannot jump on an order that was never written.
    """
    mode_norm = str(mode).strip().lower()
    if mode_norm not in {"auto", "manual"}:
        raise StoreAdminError("حالت ترتیب نامعتبر است.")
    cat = await get_draft_category(session, category_id)
    draft = await get_or_create_draft(session)
    pkgs = [p for p in draft.packages if p.category_id == cat.id]
    flags = dict(cat.sales_flags or {})
    current = plan_order_mode(cat)
    if mode_norm == "manual" and current != "manual":
        _freeze_plan_sort_orders(pkgs, cat)
    flags["plan_order_mode"] = mode_norm
    cat.sales_flags = flags
    _touch_sales_flags(cat)
    return cat


async def move_draft_package(
    session: AsyncSession, package_id: int, *, direction: str
) -> tuple[str, bool, StorePackage, StoreCategory]:
    """Swap a plan with its display neighbour.

    Returns ``(status, flipped_to_manual, package, category)`` where status is
    ``moved`` or ``at_edge``. The first arrow tap on an ``auto`` category flips
    to ``manual`` and freezes the current visual order into ``sort_order``
    before applying the move.
    """
    if direction not in {"up", "down"}:
        raise StoreAdminError("جهت جابه‌جایی نامعتبر است.")
    pkg = await get_draft_package(session, package_id)
    cat = await get_draft_category(session, int(pkg.category_id))
    draft = await get_or_create_draft(session)
    pkgs = [p for p in draft.packages if p.category_id == cat.id]
    flags = dict(cat.sales_flags or {})
    flipped = False
    if plan_order_mode(cat) != "manual":
        ordered = _freeze_plan_sort_orders(pkgs, cat)
        flags["plan_order_mode"] = "manual"
        cat.sales_flags = flags
        _touch_sales_flags(cat)
        ordered = sort_packages_for_display(ordered, cat)
        flipped = True
    else:
        ordered = sort_packages_for_display(pkgs, cat)

    idx = next((i for i, p in enumerate(ordered) if p.id == pkg.id), None)
    if idx is None:
        raise StoreAdminError("پلن در این گروه یافت نشد.")
    if direction == "up" and idx == 0:
        return "at_edge", flipped, pkg, cat
    if direction == "down" and idx >= len(ordered) - 1:
        return "at_edge", flipped, pkg, cat
    neighbour = ordered[idx - 1] if direction == "up" else ordered[idx + 1]
    ordered[idx].sort_order, neighbour.sort_order = (
        int(neighbour.sort_order),
        int(ordered[idx].sort_order),
    )
    return "moved", flipped, pkg, cat


async def _upsert_ready_economy_price(
    session: AsyncSession,
    *,
    store_version_id: int,
    slug: str,
    options: dict,
    price_toman: int | None,
) -> None:
    """Keep the legacy single-cell economy price in sync with ``fixed_price_toman`` writes."""
    duration = duration_from_package_options(options)
    existing = None
    from sqlalchemy import select as _select

    result = await session.execute(
        _select(StorePackagePrice).where(
            StorePackagePrice.store_version_id == store_version_id,
            StorePackagePrice.package_slug == slug,
            StorePackagePrice.tier == DEFAULT_TIER,
        )
    )
    candidates = list(result.scalars().all())
    for row in candidates:
        if row.duration_days == duration:
            existing = row
            break
    if price_toman is None:
        if existing is not None:
            await session.delete(existing)
        return
    if existing is None:
        # remove other economy rows for this slug at same duration already handled;
        # if duration changed, leave old cells (admin may still want them) — brief
        # says today's behaviour: one cell matching the plan's own duration.
        session.add(
            StorePackagePrice(
                store_version_id=store_version_id,
                package_slug=slug,
                duration_days=duration,
                tier=DEFAULT_TIER,
                price_toman=int(price_toman),
            )
        )
    else:
        existing.price_toman = int(price_toman)


def duration_label_fa(duration_days: int | None) -> str:
    """Owner-facing label: «۲ ماهه» or «بدون انقضا»."""
    if duration_days is None:
        return "بدون انقضا"
    months = max(1, int(duration_days) // 30)
    return f"{to_persian_digits(months)} ماهه"


def _durations_list(cat: StoreCategory) -> list[int | None]:
    return list(category_durations(cat))


async def count_prices_for_duration(
    session: AsyncSession, category_id: int, duration_days: int | None
) -> int:
    """How many store_package_prices cells exist for this category at ``duration_days``."""
    cat = await get_draft_category(session, category_id)
    slugs = [str(p.slug) for p in (await list_draft_packages(session, category_id=cat.id))]
    if not slugs:
        return 0
    q = select(StorePackagePrice).where(
        StorePackagePrice.store_version_id == int(cat.store_version_id),
        StorePackagePrice.package_slug.in_(slugs),
    )
    if duration_days is None:
        q = q.where(StorePackagePrice.duration_days.is_(None))
    else:
        q = q.where(StorePackagePrice.duration_days == int(duration_days))
    rows = (await session.execute(q)).scalars().all()
    return len(rows)


async def add_category_duration(
    session: AsyncSession,
    category_id: int,
    *,
    months: int | None,
) -> StoreCategory:
    """Append one duration. ``months is None`` → بدون انقضا (``None`` in the list).

    Rejects ``0`` / negatives / ``>24`` — zero days is an instantly-expired service,
    not «no expiry».
    """
    cat = await get_draft_category(session, category_id)
    if months is None:
        new_days: int | None = None
    else:
        m = int(months)
        if m < 1 or m > 24:
            raise StoreAdminError("عدد بین ۱ تا ۲۴ بفرست. برای بدون انقضا از دکمه استفاده کن.")
        new_days = m * 30
    current = _durations_list(cat)
    if new_days in current:
        raise StoreAdminError("این مدت از قبل هست.")
    current.append(new_days)
    flags = dict(cat.sales_flags or {})
    flags["durations"] = current
    cat.sales_flags = flags
    _touch_sales_flags(cat)
    cat.updated_at = datetime.now(UTC)
    draft = await get_draft_version(session)
    if draft is not None:
        draft.updated_at = datetime.now(UTC)
    await session.flush()
    return cat


async def remove_category_duration(
    session: AsyncSession,
    category_id: int,
    *,
    duration_days: int | None,
) -> StoreCategory:
    """Remove one duration tab. Does **not** delete price cells."""
    cat = await get_draft_category(session, category_id)
    current = _durations_list(cat)
    if duration_days not in current:
        raise StoreAdminError("این مدت در لیست نیست.")
    if len(current) <= 1:
        raise StoreAdminError("آخرین مدت را نمی‌شود برداشت — گروه بدون مدت برای مشتری خالی می‌شود.")
    current = [d for d in current if d != duration_days]
    flags = dict(cat.sales_flags or {})
    flags["durations"] = current
    cat.sales_flags = flags
    _touch_sales_flags(cat)
    cat.updated_at = datetime.now(UTC)
    draft = await get_draft_version(session)
    if draft is not None:
        draft.updated_at = datetime.now(UTC)
    await session.flush()
    return cat


async def set_category_tiers_enabled(
    session: AsyncSession, category_id: int, *, enabled: bool
) -> StoreCategory:
    """Toggle ``tiers_enabled``. Refuse ON while premium panel groups are empty."""
    cat = await get_draft_category(session, category_id)
    flags = dict(cat.sales_flags or {})
    if enabled:
        premium = list(flags.get("premium_panel_group_ids") or [])
        if not premium:
            raise StoreAdminError(
                "اول گروه‌های پنل ویژه را تنظیم کن. بدون آن، مشتری پول ویژه می‌دهد "
                "و همان سرورهای اقتصادی را می‌گیرد."
            )
    flags["tiers_enabled"] = bool(enabled)
    cat.sales_flags = flags
    _touch_sales_flags(cat)
    cat.updated_at = datetime.now(UTC)
    draft = await get_draft_version(session)
    if draft is not None:
        draft.updated_at = datetime.now(UTC)
    await session.flush()
    return cat


async def set_category_flat_plan_list(
    session: AsyncSession, category_id: int, *, enabled: bool
) -> StoreCategory:
    """Toggle ``flat_plan_list`` — one ascending ladder, no duration/tier tabs."""
    cat = await get_draft_category(session, category_id)
    flags = normalize_category_sales_flags(
        dict(cat.sales_flags or {}),
        flat_plan_list=bool(enabled),
    )
    cat.sales_flags = flags
    _touch_sales_flags(cat)
    cat.updated_at = datetime.now(UTC)
    draft = await get_draft_version(session)
    if draft is not None:
        draft.updated_at = datetime.now(UTC)
    await session.flush()
    return cat


async def set_package_hwid_limit(
    session: AsyncSession, package_id: int, *, hwid_limit: int
) -> StorePackage:
    """Write ``options.hwid_limit`` on a ready volume plan (0 = نامحدود)."""
    pkg = await get_draft_package(session, package_id)
    devices = _hwid_dim(int(hwid_limit))
    if devices is None:
        raise StoreAdminError("تعداد کاربر همزمان نامعتبر است.")
    opts = dict(pkg.options or {})
    opts["hwid_limit"] = devices
    pkg.options = opts
    pkg.updated_at = datetime.now(UTC)
    draft = await get_draft_version(session)
    if draft is not None:
        draft.updated_at = datetime.now(UTC)
    await session.flush()
    return pkg


async def set_category_trial(
    session: AsyncSession, category_id: int, *, data_limit_gb: int, duration_days: int
) -> StoreCategory:
    """Set this group's free trial; ``data_limit_gb=0`` switches it off.

    Off keeps the stored days, so switching it back on is one number, not two.
    """
    cat = await get_draft_category(session, category_id)
    gb = int(data_limit_gb)
    days = int(duration_days)
    if gb < 0 or gb > TRIAL_GB_MAX:
        raise StoreAdminError(f"حجم تست باید بین ۰ و {to_persian_digits(TRIAL_GB_MAX)} گیگ باشد.")
    if gb > 0 and not 1 <= days <= TRIAL_DAYS_MAX:
        raise StoreAdminError(f"مدت تست باید بین ۱ و {to_persian_digits(TRIAL_DAYS_MAX)} روز باشد.")
    flags = normalize_category_sales_flags(
        dict(cat.sales_flags or {}),
        trial_gb=gb,
        trial_days=days if gb > 0 else None,
    )
    cat.sales_flags = flags
    _touch_sales_flags(cat)
    cat.updated_at = datetime.now(UTC)
    draft = await get_draft_version(session)
    if draft is not None:
        draft.updated_at = datetime.now(UTC)
    await session.flush()
    return cat


async def set_category_volume_hwid_limit(
    session: AsyncSession, category_id: int, *, hwid_limit: int
) -> StoreCategory:
    """Set ``sales_flags.volume_hwid_limit`` for custom volume purchases."""
    cat = await get_draft_category(session, category_id)
    devices = _hwid_dim(int(hwid_limit))
    if devices is None:
        raise StoreAdminError("تعداد کاربر همزمان نامعتبر است.")
    flags = normalize_category_sales_flags(
        dict(cat.sales_flags or {}),
        volume_hwid_limit=devices,
    )
    cat.sales_flags = flags
    _touch_sales_flags(cat)
    cat.updated_at = datetime.now(UTC)
    draft = await get_draft_version(session)
    if draft is not None:
        draft.updated_at = datetime.now(UTC)
    await session.flush()
    return cat


async def set_category_premium_panel_groups(
    session: AsyncSession, category_id: int, panel_group_ids: Any
) -> tuple[StoreCategory, bool]:
    """Set premium panel groups. Returns ``(cat, tiers_was_disabled)``.

    Clearing groups while the switch is on turns the switch off in the same write.
    """
    cat = await get_draft_category(session, category_id)
    parsed = _parse_group_ids(panel_group_ids)
    flags = dict(cat.sales_flags or {})
    flags["premium_panel_group_ids"] = list(parsed)
    disabled = False
    if not parsed and bool(flags.get("tiers_enabled")):
        flags["tiers_enabled"] = False
        disabled = True
    cat.sales_flags = flags
    _touch_sales_flags(cat)
    cat.updated_at = datetime.now(UTC)
    draft = await get_draft_version(session)
    if draft is not None:
        draft.updated_at = datetime.now(UTC)
    await session.flush()
    return cat, disabled


async def commit_and_activate(
    session: AsyncSession, *, notes: str | None = None
) -> StoreVersion | None:
    """Make the pending edit live in one step, and commit.

    The owner's verdict on the draft/publish cycle was blunt: making a plan and
    then remembering to publish it is «اضافه‌کاری». So every confirmed edit ends
    here instead of leaving a draft for a second trip.

    The *schema* keeps draft and published versions, because that pair is the
    transaction boundary and the only reason a mispriced plan can be rolled
    back from history — which the owner asked to keep. What went away is the
    ceremony, not the safety net.

    Returns the now-published version, or ``None`` when there was no draft to
    activate (a no-op edit), so callers never have to special-case that.
    """
    draft = await get_draft_version(session)
    if draft is None:
        await session.commit()
        return None
    # Always overwrite. A fresh draft carries an internal English note
    # («empty draft (seed from pricing.json recommended)», «draft cloned from
    # published v3») and this field is now read straight back to the owner in
    # «تغییرات فروشگاه» — internal scaffolding must not surface there.
    published = await publish_draft(session, notes=notes or "تغییر در فروشگاه")
    await session.commit()
    return published


async def preview_draft(session: AsyncSession) -> DraftPreview:
    draft = await get_or_create_draft(session)
    cats = sorted(draft.categories, key=lambda c: (c.sort_order, c.id))
    pkgs = sorted(draft.packages, key=lambda p: (p.sort_order, p.id))
    cat_by_id = {c.id: c for c in cats}
    pricing = draft.pricing_document if isinstance(draft.pricing_document, dict) else {}
    vol = pricing.get("volume_plan") or {}
    unl = pricing.get("unlimited_plan") or {}
    pricing_summary = {
        "currency": pricing.get("currency", "IRT"),
        "volume_options_gb": list(vol.get("volume_options_gb") or []),
        "duration_options_days": list(vol.get("duration_options_days") or []),
        "user_options": list(unl.get("user_options") or []),
        "duration_options_months": list(unl.get("duration_options_months") or []),
        "promo_codes": list((pricing.get("promo_codes") or {}).keys()),
        "tiers_count": len(vol.get("tiers") or []),
    }
    cat_rows = [
        {
            "id": c.id,
            "slug": c.slug,
            "title_fa": c.title_fa,
            "enabled": c.enabled,
            "sort_order": c.sort_order,
            "panel_group_ids": list(c.panel_group_ids or []),
            "sales_flags": dict(c.sales_flags or {}),
        }
        for c in cats
    ]
    pkg_rows = []
    for p in pkgs:
        cat = cat_by_id.get(p.category_id)
        pkg_rows.append(
            {
                "id": p.id,
                "slug": p.slug,
                "kind": p.kind.value if isinstance(p.kind, StorePackageKind) else str(p.kind),
                "title_fa": p.title_fa,
                "enabled": p.enabled,
                "category_slug": cat.slug if cat else "?",
                "category_title": cat.title_fa if cat else "?",
                "fixed_price_toman": p.fixed_price_toman,
                "pricing_ref": p.pricing_ref,
                "options": dict(p.options or {}),
                "forbid_extra_discount": p.forbid_extra_discount,
                "allow_new_sale": p.allow_new_sale,
                "allow_renew": p.allow_renew,
            }
        )
    return DraftPreview(
        draft_id=draft.id,
        draft_version=draft.version,
        categories=cat_rows,
        packages=pkg_rows,
        pricing_summary=pricing_summary,
        purchase_steps=list(draft.purchase_steps or []),
        sales_rules=_locked_sales_rules(
            draft.sales_rules if isinstance(draft.sales_rules, dict) else {}
        ),
    )


def preview_catalog(view: CatalogView) -> DraftPreview:
    """Read-only customer preview of the live catalog. Never touches the draft."""
    if view.version is None:
        raise StoreAdminError("هنوز فروشگاهی در دیتابیس نیست.")
    version = view.version
    cats = sorted(view.categories, key=lambda c: (c.sort_order, c.id))
    pkgs = sorted(view.packages, key=lambda p: (p.sort_order, p.id))
    cat_by_id = {c.id: c for c in cats}
    pricing = version.pricing_document if isinstance(version.pricing_document, dict) else {}
    vol = pricing.get("volume_plan") or {}
    unl = pricing.get("unlimited_plan") or {}
    pricing_summary = {
        "currency": pricing.get("currency", "IRT"),
        "volume_options_gb": list(vol.get("volume_options_gb") or []),
        "duration_options_days": list(vol.get("duration_options_days") or []),
        "user_options": list(unl.get("user_options") or []),
        "duration_options_months": list(unl.get("duration_options_months") or []),
        "promo_codes": list((pricing.get("promo_codes") or {}).keys()),
        "tiers_count": len(vol.get("tiers") or []),
    }
    cat_rows = [
        {
            "id": c.id,
            "slug": c.slug,
            "title_fa": c.title_fa,
            "enabled": c.enabled,
            "sort_order": c.sort_order,
            "panel_group_ids": list(c.panel_group_ids or []),
            "sales_flags": dict(c.sales_flags or {}),
        }
        for c in cats
    ]
    pkg_rows = []
    for p in pkgs:
        cat = cat_by_id.get(p.category_id)
        pkg_rows.append(
            {
                "id": p.id,
                "slug": p.slug,
                "kind": p.kind.value if isinstance(p.kind, StorePackageKind) else str(p.kind),
                "title_fa": p.title_fa,
                "enabled": p.enabled,
                "category_slug": cat.slug if cat else "?",
                "category_title": cat.title_fa if cat else "?",
                "fixed_price_toman": p.fixed_price_toman,
                "pricing_ref": p.pricing_ref,
                "options": dict(p.options or {}),
                "forbid_extra_discount": p.forbid_extra_discount,
                "allow_new_sale": p.allow_new_sale,
                "allow_renew": p.allow_renew,
            }
        )
    return DraftPreview(
        draft_id=version.id,
        draft_version=version.version,
        categories=cat_rows,
        packages=pkg_rows,
        pricing_summary=pricing_summary,
        purchase_steps=list(version.purchase_steps or []),
        sales_rules=_locked_sales_rules(
            version.sales_rules if isinstance(version.sales_rules, dict) else {}
        ),
    )


def format_preview_fa(preview: DraftPreview) -> str:
    """Persian summary of what the customer currently sees."""
    lines: list[str] = [
        f"پیش‌نمایش مشتری — نسخهٔ فعال v{preview.draft_version}",
        "همین چیزی است که مشتری همین الان می‌بیند.",
        "",
        "📂 گروه‌های محصول:",
    ]
    if not preview.categories:
        lines.append("  (خالی)")
    for c in preview.categories:
        en = "✅" if c["enabled"] else "❌"
        flags = c.get("sales_flags") or {}
        ns = flags.get("allow_new_sale", True)
        rn = flags.get("allow_renew", True)
        lines.append(
            f"  {en} {c['title_fa']} — "
            f"فروش جدید:{'بله' if ns else 'خیر'} | تمدید:{'بله' if rn else 'خیر'}"
        )
        if c.get("panel_group_ids"):
            lines.append(f"     گروه‌های پنل: {c['panel_group_ids']}")
    lines.append("")
    lines.append("🎁 پلن‌ها:")
    if not preview.packages:
        lines.append("  (خالی)")
    for p in preview.packages:
        en = "✅" if p["enabled"] else "❌"
        kind_fa = "پلن آماده" if p["kind"] == "ready" else "خرید سفارشی"
        if p.get("fixed_price_toman") is not None:
            price = f"{p['fixed_price_toman']:,} تومان"
        else:
            price = "قیمت‌گذاری پویا"
        lines.append(f"  {en} [{kind_fa}] {p['title_fa']} — {p['category_title']} — {price}")
        if p["kind"] != "ready" and p.get("options"):
            opt = p["options"]
            bits = []
            label_map = {
                "volume_options_gb": "حجم‌ها",
                "duration_options_days": "مدت (روز)",
                "user_options": "کاربران",
                "duration_options_months": "مدت (ماه)",
            }
            for key, fa in label_map.items():
                if opt.get(key):
                    bits.append(f"{fa}={opt[key]}")
            if bits:
                lines.append(f"     گزینه‌ها: {'; '.join(bits)}")
    ps = preview.pricing_summary
    step_bits = []
    for s in preview.purchase_steps:
        if isinstance(s, dict):
            en = "" if s.get("enabled", True) else " (خاموش)"
            step_bits.append(f"{s.get('block')}{en}")
        else:
            step_bits.append(str(s))
    lines.extend(
        [
            "",
            "💰 خلاصه قیمت:",
            f"  ارز: {ps.get('currency')}",
            f"  حجم‌ها: {ps.get('volume_options_gb')}",
            f"  مدت حجمی (روز): {ps.get('duration_options_days')}",
            f"  کاربران نامحدود: {ps.get('user_options')}",
            f"  مدت نامحدود (ماه): {ps.get('duration_options_months')}",
            f"  کدهای تخفیف: {ps.get('promo_codes') or '—'}",
            "",
            "🧭 مراحل خرید: " + ", ".join(step_bits),
        ]
    )
    return "\n".join(lines)


async def build_published_preview(session: AsyncSession) -> DraftPreview | None:
    """Read-only preview of the published catalog (no draft created)."""
    from services.store_config import get_published_store_config

    pub = await get_published_store_config(session)
    if pub is None:
        return None
    cats = sorted(pub.categories, key=lambda c: (c.sort_order, c.id))
    pkgs = sorted(pub.packages, key=lambda p: (p.sort_order, p.id))
    cat_by_id = {c.id: c for c in cats}
    pricing = pub.pricing
    vol = pricing.get("volume_plan") or {}
    unl = pricing.get("unlimited_plan") or {}
    pricing_summary = {
        "currency": pricing.get("currency", "IRT"),
        "volume_options_gb": list(vol.get("volume_options_gb") or []),
        "duration_options_days": list(vol.get("duration_options_days") or []),
        "user_options": list(unl.get("user_options") or []),
        "duration_options_months": list(unl.get("duration_options_months") or []),
        "promo_codes": list((pricing.get("promo_codes") or {}).keys()),
        "tiers_count": len(vol.get("tiers") or []),
    }
    cat_rows = [
        {
            "id": c.id,
            "slug": c.slug,
            "title_fa": c.title_fa,
            "enabled": c.enabled,
            "sort_order": c.sort_order,
            "panel_group_ids": list(c.panel_group_ids or []),
            "sales_flags": dict(c.sales_flags or {}),
        }
        for c in cats
    ]
    pkg_rows = []
    for p in pkgs:
        cat = cat_by_id.get(p.category_id)
        pkg_rows.append(
            {
                "id": p.id,
                "slug": p.slug,
                "kind": p.kind.value if isinstance(p.kind, StorePackageKind) else str(p.kind),
                "title_fa": p.title_fa,
                "enabled": p.enabled,
                "category_slug": cat.slug if cat else "?",
                "category_title": cat.title_fa if cat else "?",
                "fixed_price_toman": p.fixed_price_toman,
                "pricing_ref": p.pricing_ref,
                "options": dict(p.options or {}),
                "forbid_extra_discount": p.forbid_extra_discount,
                "allow_new_sale": p.allow_new_sale,
                "allow_renew": p.allow_renew,
            }
        )
    return DraftPreview(
        draft_id=pub.version.id,
        draft_version=pub.version.version,
        categories=cat_rows,
        packages=pkg_rows,
        pricing_summary=pricing_summary,
        purchase_steps=list(pub.purchase_steps),
        sales_rules=_locked_sales_rules(pub.sales_rules),
    )


async def seed_store_if_empty(
    session: AsyncSession, path: str | Path, *, publish: bool = True
) -> StoreVersion | None:
    """Owner bootstrap: seed from pricing.json when no published version exists."""
    return await seed_store_from_pricing_json(session, path, publish=publish)


# Public aliases for STORE_STRATEGY (avoid importing underscored names).
clone_version_as_draft = _clone_version_as_draft
load_store_version = _load_version
locked_sales_rules = _locked_sales_rules

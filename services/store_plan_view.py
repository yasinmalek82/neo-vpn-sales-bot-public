"""What the plan editor shows: the plan's facts, and the customer's button for it.

The Mini App's plan editor draws a form and, beside it, **the exact label the
customer's purchase keyboard would show for the plan being typed**. The label is
built by ``services/store_catalog.shelf_plan_label``, the function
``bot/keyboards/purchase.py::ready_plans_keyboard`` calls for every plan button, fed
with an in-memory stand-in for the plan (a transient ``StorePackage`` that is never
added to a session) and a transient price row. Nothing here writes and nothing here
creates a draft: a preview is a read.

The warnings are the bot's own: the shelf has no tab for this duration
(``no_tab_warning``) and a bigger plan that is not cheaper per GB (``ladder_lines``,
asked about the ladder *as it would be* with this plan in it).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from bot.texts import texts
from database.models.store import StoreCategory, StorePackage, StorePackageKind, StorePackagePrice
from services.store_admin import CatalogView, StoreAdminError
from services.store_catalog import (
    flat_plan_dim_label,
    flat_plan_list_enabled,
    hwid_limit_label_fa,
    package_hwid_limit,
    shelf_plan_label,
)
from services.store_dashboard import ladder_lines, retail_cell
from services.store_plan_edit import (
    derive_plan_title,
    dim_keys,
    duration_in_days,
    no_tab_warning,
    plan_kind_of,
    plan_options,
    read_hwid,
    read_plan_number,
    read_plan_title,
    read_price,
    visible_plan_controls,
)
from services.store_pricing import DEFAULT_TIER, duration_from_package_options
from services.ux_format import strip_html_tags

PREVIEW_STAND_IN_SLUG = "preview"


def ready_dims_complete(pkg: StorePackage) -> bool:
    """Whether ``choose_ready_plan`` can honour this plan as priced.

    An incomplete ready plan is diverted into the custom گیگ/ماه builder and its
    fixed price is dropped, so the plan screens say so.
    """
    opts = dict(pkg.options or {})
    volume_ok = opts.get("volume_gb") is not None and opts.get("duration_days") is not None
    unlimited_ok = (
        opts.get("concurrent_users") is not None and opts.get("duration_months") is not None
    )
    return volume_ok or unlimited_ok


@dataclass(frozen=True)
class PlanPreview:
    """The customer's button for the plan as typed, and what to warn about."""

    label: str | None
    warnings: tuple[str, ...] = ()
    error: str | None = None
    hint: str | None = None


@dataclass(frozen=True)
class PlanEditor:
    """A plan (or a new one) as the editor's boxes start."""

    group_slug: str
    group_title: str
    group_kind: str  # volume | unlimited
    is_new: bool
    slug: str | None
    title: str
    controls: frozenset[str]
    enabled: bool
    new_sale: bool
    renew: bool
    extra_discount: bool  # the switch reads «تخفیف اضافه»: on when it is allowed
    size: int | None
    duration: int | None
    no_expiry: bool
    hwid: int | None
    hwid_label: str
    price: int | None
    size_label: str
    duration_label: str
    incomplete: str | None
    preview: PlanPreview


def _find_plan(view: CatalogView, cat: StoreCategory, slug: str | None) -> StorePackage | None:
    if slug is None:
        return None
    return next((p for p in view.packages_of(cat.id) if str(p.slug) == slug), None)


def _stand_in(
    view: CatalogView,
    cat: StoreCategory,
    saved: StorePackage | None,
    *,
    title: str,
    options: dict[str, Any],
) -> StorePackage:
    """A plan that exists only in memory, shaped like the one being typed."""
    sort_order = (
        int(saved.sort_order)
        if saved is not None
        else max((int(p.sort_order) for p in view.packages_of(cat.id)), default=0) + 10
    )
    return StorePackage(
        id=int(saved.id) if saved is not None else -1,
        store_version_id=int(cat.store_version_id),
        category_id=int(cat.id),
        slug=str(saved.slug) if saved is not None else PREVIEW_STAND_IN_SLUG,
        kind=StorePackageKind.ready,
        title_fa=title,
        enabled=True,
        allow_new_sale=True,
        allow_renew=True,
        forbid_extra_discount=False,
        sort_order=sort_order,
        options=options,
    )


def preview_plan(
    view: CatalogView,
    cat: StoreCategory,
    plan_slug: str | None,
    params: Mapping[str, str],
) -> PlanPreview:
    """The customer's button for the typed values; a box that is absent keeps the saved value.

    Every typed number goes through the readers the write uses (``read_plan_number``,
    ``read_hwid``, ``read_price``: ``check_dim`` and ``DIM_LIMITS``), so what the
    preview accepts the save accepts, and what it refuses it refuses with the same line.
    A box left blank is a hint, not an error: the owner has not typed it yet.
    """
    kind = plan_kind_of(cat)
    size_key, duration_key = dim_keys(kind)
    saved = _find_plan(view, cat, plan_slug)
    opts = dict(saved.options or {}) if saved is not None else {}
    size_example = "15" if kind == "volume" else "3"
    duration_example = "45" if kind == "volume" else "2"

    def typed(name: str) -> str | None:
        return params.get(name)

    try:
        size: int | None
        if typed("size") is not None:
            raw = str(typed("size")).strip()
            size = read_plan_number(size_key, raw, example=size_example) if raw else None
        else:
            size = opts.get(size_key) if saved is not None else None
        duration: int | None
        if typed("duration") is not None:
            if params.get("no_expiry") == "1":
                duration = None
                duration_known = True
            else:
                raw = str(typed("duration")).strip()
                duration = (
                    read_plan_number(duration_key, raw, example=duration_example) if raw else None
                )
                duration_known = bool(raw)
        else:
            duration = opts.get(duration_key) if saved is not None else None
            duration_known = saved is not None
        hwid: int | None = None
        if kind != "unlimited":
            if typed("hwid") is not None and str(typed("hwid")).strip():
                hwid = read_hwid(str(typed("hwid")))
            else:
                hwid = package_hwid_limit(saved) if saved is not None else 0
        price: int | None
        if typed("price") is not None:
            raw = str(typed("price")).strip()
            price = read_price(raw) if raw else None
        else:
            price = retail_cell(list(view.prices), saved) if saved is not None else None
        raw_title = str(typed("title") or "").strip() if typed("title") is not None else None
        title = read_plan_title(raw_title) if raw_title else None
    except StoreAdminError as exc:
        return PlanPreview(label=None, error=strip_html_tags(str(exc)))

    if size is None or not duration_known or price is None:
        return PlanPreview(label=None, hint=texts.admin_store_plan_preview_hint)

    shown_title = title or (
        str(saved.title_fa)
        if saved is not None and typed("title") is None
        else derive_plan_title(kind=kind, size=size, duration=duration)
    )
    options = plan_options(kind, size=size, duration=duration, hwid=hwid)
    stand_in = _stand_in(view, cat, saved, title=shown_title, options=options)
    label = shelf_plan_label(stand_in, price, flat_mode=flat_plan_list_enabled(cat))

    warnings: list[str] = []
    tab = no_tab_warning(cat, kind=kind, duration=duration)
    if tab is not None:
        warnings.append(tab)
    # The ladder as it would be: every other plan as saved, this one as typed.
    days = duration_in_days(kind, duration)
    others = [p for p in view.packages_of(cat.id) if saved is None or p.slug != saved.slug]
    rows = [
        r
        for r in view.prices
        if saved is None or str(getattr(r, "package_slug", "")) != str(saved.slug)
    ]
    rows.append(
        StorePackagePrice(
            store_version_id=int(cat.store_version_id),
            package_slug=stand_in.slug,
            duration_days=days if kind == "unlimited" else duration_from_package_options(options),
            tier=DEFAULT_TIER,
            price_toman=price,
        )
    )
    dims = flat_plan_dim_label(stand_in)
    for line in ladder_lines(cat, [*others, stand_in], rows):
        if dims and dims in line:
            warnings.append(line)
    return PlanPreview(label=label, warnings=tuple(warnings))


def build_plan_editor(
    view: CatalogView, cat: StoreCategory, plan_slug: str | None
) -> PlanEditor | None:
    """The editor model for a saved plan, or for a new one in ``cat`` (``plan_slug=None``)."""
    kind = plan_kind_of(cat)
    pkg = _find_plan(view, cat, plan_slug)
    if plan_slug is not None and pkg is None:
        return None
    unlimited = kind == "unlimited"
    size_key, duration_key = dim_keys(kind)
    opts = dict(pkg.options or {}) if pkg is not None else {}
    size = opts.get(size_key) if pkg is not None else None
    duration = opts.get(duration_key) if pkg is not None else None
    price = retail_cell(list(view.prices), pkg) if pkg is not None else None
    hwid = 0 if pkg is None else (None if unlimited else package_hwid_limit(pkg))
    return PlanEditor(
        group_slug=str(cat.slug),
        group_title=str(cat.title_fa),
        group_kind=kind,
        is_new=pkg is None,
        slug=str(pkg.slug) if pkg is not None else None,
        title=str(pkg.title_fa) if pkg is not None else "",
        controls=(visible_plan_controls(pkg) if pkg is not None else frozenset()),
        enabled=bool(pkg.enabled) if pkg is not None else True,
        new_sale=bool(pkg.allow_new_sale) if pkg is not None else True,
        renew=bool(pkg.allow_renew) if pkg is not None else True,
        extra_discount=not bool(pkg.forbid_extra_discount) if pkg is not None else True,
        size=int(size) if size is not None else None,
        duration=int(duration) if duration is not None else None,
        no_expiry=pkg is not None and duration is None,
        hwid=hwid,
        hwid_label=hwid_limit_label_fa(hwid or 0),
        price=price,
        size_label="تعداد کاربر همزمان" if unlimited else "حجم (گیگ)",
        duration_label="مدت (ماه)" if unlimited else "مدت (روز)",
        incomplete=(
            texts.admin_store_pkg_incomplete
            if pkg is not None and not ready_dims_complete(pkg)
            else None
        ),
        preview=preview_plan(view, cat, plan_slug, {}),
    )

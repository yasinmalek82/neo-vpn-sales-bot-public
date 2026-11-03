"""How the store reads on a screen: one composition for the bot and the panel.

The group dashboard's status lines, its ⚠️ warnings, the hub's status line and
the marks on a group's name were written inside ``bot/handlers/store_admin.py``.
The Mini App shows the same screens, and a second copy of a warning in a
template is how two screens come to disagree about whether a group is for
sale. So the composition lives here; the bot HTML-escapes through ``quote`` and
the panel's Jinja escapes on output.

Nothing here writes, and nothing here creates a draft: a browse screen reads
``get_catalog_view`` and only that.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from bot.texts import texts
from database.models.store import StoreCategory, StorePackage, StorePackageKind
from services.store_admin import CatalogView, category_off_sale
from services.store_catalog import (
    CUSTOM_PURCHASE_EVERYONE,
    CUSTOM_PURCHASE_OFF,
    CUSTOM_PURCHASE_RESELLERS,
    category_trial_shape,
    custom_purchase_mode,
    flat_plan_dim_label,
    flat_plan_list_enabled,
    hwid_limit_label_fa,
    is_unlimited_like,
    is_volume_like,
    ladder_warnings,
    package_hwid_limit,
    plan_order_mode,
    plans_without_tab,
    sort_packages_for_display,
)
from services.store_pricing import DEFAULT_TIER, duration_from_package_options, get_price
from services.ux_format import format_toman, to_persian_digits

Quote = Callable[[str], str]


def _same(text: str) -> str:
    return text


CUSTOM_STATE_FA = {
    CUSTOM_PURCHASE_OFF: texts.admin_store_custom_off,
    CUSTOM_PURCHASE_RESELLERS: texts.admin_store_custom_resellers,
    CUSTOM_PURCHASE_EVERYONE: texts.admin_store_custom_everyone,
}


def parent_of(view: CatalogView, cat: StoreCategory) -> StoreCategory | None:
    return view.category(int(cat.parent_id)) if cat.parent_id is not None else None


def panel_group_label(
    cat: StoreCategory, parent: StoreCategory | None, *, web: bool = False
) -> str:
    """The card's «گروه پنل» value: own ids, the parent's, or a warning.

    ``web`` swaps the long warning for its short twin: the page shows the full
    sentence once, as the alert under the facts.

    A group with none is not for sale, and the card says so rather than naming
    a default that no longer exists.
    """
    own = [int(item) for item in (cat.panel_group_ids or [])]
    if own:
        return "، ".join(str(item) for item in own)
    inherited = [int(item) for item in ((parent.panel_group_ids if parent else None) or [])]
    if inherited:
        return texts.admin_store_cat_groups_inherited.format(
            ids="، ".join(str(item) for item in inherited)
        )
    return texts.web_store_cat_groups_unset if web else texts.admin_store_cat_groups_unset


def retail_cell(prices: list[Any], pkg: StorePackage) -> int | None:
    """The plan's retail price: the economy cell at the plan's own duration."""
    return get_price(
        prices,
        slug=str(pkg.slug),
        duration_days=duration_from_package_options(dict(pkg.options or {})),
        tier=DEFAULT_TIER,
    )


def ladder_lines(cat: StoreCategory, pkgs: list[StorePackage], prices: list[Any]) -> list[str]:
    """«نردبان قیمت» warnings: only where the flat volume ladder exists."""
    if not (flat_plan_list_enabled(cat) and is_volume_like(cat)):
        return []
    priced_pairs: list[tuple[Any, int]] = []
    for pkg in sort_packages_for_display(list(pkgs), cat):
        cell = retail_cell(prices, pkg)
        if cell is not None:
            priced_pairs.append((pkg, int(cell)))
    return [
        texts.admin_store_ladder_warning.format(
            small=small,
            big=big,
            small_per_gb=format_toman(small_per, persian_digits=True),
            big_per_gb=format_toman(big_per, persian_digits=True),
        )
        for small, big, small_per, big_per in ladder_warnings(priced_pairs)
    ]


def plan_order_label(cat: StoreCategory) -> str:
    """One row: current mode plus the alternate («ترتیب: … ↔ …»)."""
    if plan_order_mode(cat) == "manual":
        mode_text = texts.admin_store_order_manual
        switch = texts.admin_store_order_switch_auto
    elif is_unlimited_like(cat):
        mode_text = texts.admin_store_order_auto_users
        switch = texts.admin_store_order_switch_manual
    else:
        mode_text = texts.admin_store_order_auto
        switch = texts.admin_store_order_switch_manual
    return f"ترتیب: {mode_text} ↔ {switch}"


@dataclass(frozen=True)
class PlanOrderView:
    """«↕️ چیدمان پلن‌ها»: the mode toggle's row text, and whether ⬆️⬇️ show (manual only)."""

    manual: bool
    label: str


def plan_order_view(cat: StoreCategory) -> PlanOrderView:
    return PlanOrderView(manual=plan_order_mode(cat) == "manual", label=plan_order_label(cat))


def group_marks(view: CatalogView, cat: StoreCategory) -> str:
    """«⏸ » when off sale, «⚠️ » when it has no panel group, in front of a name."""
    marks = ""
    if category_off_sale(cat):
        marks += "⏸ "
    if view.unsellable(cat):
        marks += "⚠️ "
    return marks


def hub_status(view: CatalogView) -> str:
    """«N گروه، M پلن» and, when a group cannot sell, the ⚠️ count."""
    plans = sum(1 for p in view.packages if p.kind == StorePackageKind.ready)
    warn = sum(1 for c in view.categories if not view.children_of(c.id) and view.unsellable(c))
    status = texts.admin_store_hub_status.format(
        groups=to_persian_digits(len(view.categories)), plans=to_persian_digits(plans)
    )
    if warn:
        status = f"{status}{texts.admin_store_hub_warn.format(n=to_persian_digits(warn))}"
    return status


def hub_body(view: CatalogView, *, is_owner: bool) -> str:
    """The hub's text: title, status line, the «goes live at once» sentence, read-only note."""
    body = texts.admin_store_hub.format(status=hub_status(view))
    if not is_owner:
        body = f"{body}\n{texts.admin_store_hub_readonly}"
    return body


@dataclass(frozen=True)
class GroupFacts:
    """A group's status block (``lines``) and its ⚠️ lines (``warnings``)."""

    title: str
    lines: tuple[str, ...]
    warnings: tuple[str, ...]


def group_facts(
    view: CatalogView, cat: StoreCategory, *, quote: Quote = _same, web: bool = False
) -> GroupFacts:
    """Sale, panel group, trial, shelf, [custom], children or plan count, then warnings.

    ``quote`` escapes the values that come from the owner's own typing (the
    group's panel label and plan titles); the bot passes ``html.escape``. ``web`` makes
    the panel line carry the short «تنظیم نشده», because the page shows the full
    sentence once, in the warning under it.
    """
    flags = cat.sales_flags or {}
    kids = view.children_of(cat.id)
    pkgs = view.packages_of(cat.id)
    ready = [p for p in pkgs if p.kind == StorePackageKind.ready]
    trial = category_trial_shape(cat)
    lines = [
        texts.admin_store_dash_sale.format(state="خاموش" if category_off_sale(cat) else "روشن"),
        texts.admin_store_dash_panel.format(
            groups=quote(panel_group_label(cat, parent_of(view, cat), web=web))
        ),
        texts.admin_store_dash_trial.format(
            trial=(
                "خاموش"
                if trial is None
                else f"{to_persian_digits(trial[0])} گیگ • {to_persian_digits(trial[1])} روز"
            )
        ),
        texts.admin_store_dash_shelf.format(
            shelf="فهرست ساده" if flags.get("flat_plan_list") else "تب مدت"
        ),
    ]
    if not is_unlimited_like(cat):
        lines.append(
            texts.admin_store_dash_custom.format(state=CUSTOM_STATE_FA[custom_purchase_mode(cat)])
        )
    if kids:
        lines.append(texts.admin_store_dash_children.format(n=to_persian_digits(len(kids))))
    else:
        lines.append(texts.admin_store_dash_plans.format(n=to_persian_digits(len(ready))))
    warnings: list[str] = []
    if view.unsellable(cat):
        warnings.append(texts.admin_store_dash_warn_no_panel)
    if not kids and not pkgs:
        warnings.append(texts.admin_store_dash_warn_no_plans)
    hidden = plans_without_tab(cat, pkgs, list(view.prices))
    if hidden:
        warnings.append(
            texts.admin_store_dash_warn_no_tab.format(
                plans="، ".join(f"«{quote(str(p.title_fa))}»" for p in hidden)
            )
        )
    warnings.extend(ladder_lines(cat, pkgs, list(view.prices)))
    return GroupFacts(title=cat.title_fa, lines=tuple(lines), warnings=tuple(warnings))


@dataclass(frozen=True)
class PlanEntry:
    """One plan of a group as the panel lists it."""

    slug: str
    title: str
    dims: str | None
    price_toman: int | None
    devices: str | None
    on_sale: bool
    editable: bool


def plan_entries(view: CatalogView, cat: StoreCategory) -> list[PlanEntry]:
    """The group's plans in shelf order. Only a ready plan has a fixed price to edit."""
    prices = list(view.prices)
    out: list[PlanEntry] = []
    for pkg in view.packages_of(cat.id):
        ready = pkg.kind == StorePackageKind.ready
        has_volume = (pkg.options or {}).get("volume_gb") is not None or (pkg.options or {}).get(
            "data_limit_gb"
        ) is not None
        out.append(
            PlanEntry(
                slug=str(pkg.slug),
                title=str(pkg.title_fa),
                dims=flat_plan_dim_label(pkg),
                price_toman=retail_cell(prices, pkg),
                devices=(
                    hwid_limit_label_fa(package_hwid_limit(pkg)) if ready and has_volume else None
                ),
                on_sale=bool(pkg.enabled and pkg.allow_new_sale),
                editable=ready,
            )
        )
    return out


@dataclass(frozen=True)
class GroupNode:
    """A group in the list, with its child groups (two levels at most)."""

    slug: str
    title: str
    marks: str
    plan_count: int
    children: tuple[GroupNode, ...]


def group_tree(view: CatalogView) -> list[GroupNode]:
    """Top-level groups in the bot's hub order, each with its children."""

    def node(cat: StoreCategory) -> GroupNode:
        kids = view.children_of(cat.id)
        return GroupNode(
            slug=str(cat.slug),
            title=str(cat.title_fa),
            marks=group_marks(view, cat),
            plan_count=sum(1 for p in view.packages_of(cat.id) if p.kind == StorePackageKind.ready),
            children=tuple(node(kid) for kid in kids),
        )

    tops = sorted(
        (c for c in view.categories if c.parent_id is None), key=lambda c: (c.sort_order, c.id)
    )
    return [node(cat) for cat in tops]


def find_group(view: CatalogView, slug: str) -> StoreCategory | None:
    """A group by its slug, the identity that survives a publish."""
    return next((c for c in view.categories if str(c.slug) == slug), None)


def first_group_slug(tree: list[GroupNode]) -> str | None:
    """Where the panel opens: the first group that holds plans, else the first group."""
    for top in tree:
        if not top.children:
            return top.slug
        return top.children[0].slug
    return None


def node_for(tree: list[GroupNode], slug: str) -> GroupNode | None:
    for top in tree:
        if top.slug == slug:
            return top
        for kid in top.children:
            if kid.slug == slug:
                return kid
    return None

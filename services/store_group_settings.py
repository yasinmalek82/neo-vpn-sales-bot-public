"""A group's four settings sections: which controls show, and what each write does.

The bot's group dashboard ends in four sections (🛒 فروش, 🎟 تست, 🖥 پنل, ✏️ گروه),
each setting in exactly one. The Mini App shows the same four. Two things are
shared so the screens cannot drift:

* **When a control shows** (``visible_controls``): hide until relevant, a control
  that would save a flag nothing reads is not drawn. The bot's keyboards and the
  panel's template both ask this one function.
* **What a write does** (``toggle_category_flag``, ``save_category_trial`` and
  the rest): validate, write the draft through ``services/store_admin.py``, and
  publish with the Persian change-log note «تغییرات فروشگاه» reads back to the
  owner. The bot's handlers and the panel's routes both call these.

Nothing here knows Telegram or HTTP. A parse failure is a ``StoreAdminError``
carrying the bot's own Persian line, so a caller prints ``str(exc)``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.texts import texts
from database.models.store import StoreCategory
from services.group_sync import accounts_of_category, target_groups_for_category
from services.panel_settings import published_category_groups
from services.store_admin import (
    CatalogView,
    StoreAdminError,
    add_category_duration,
    commit_and_activate,
    duration_label_fa,
    get_draft_category,
    get_or_create_draft,
    remove_category_duration,
    set_category_flat_plan_list,
    set_category_premium_panel_groups,
    set_category_tiers_enabled,
    set_category_trial,
    set_category_volume_hwid_limit,
    update_draft_category,
)
from services.store_catalog import (
    CUSTOM_PURCHASE_OFF,
    category_kind,
    category_reseller_trial,
    category_trial_shape,
    category_volume_hwid_limit,
    custom_purchase_mode,
    flat_plan_list_enabled,
    hwid_limit_label_fa,
    is_unlimited_like,
    next_custom_purchase_mode,
)
from services.store_dashboard import CUSTOM_STATE_FA, panel_group_label, parent_of
from services.store_edit import (
    INT64_MAX,
    TEXT_STALE,
    StaleStoreVersion,
    guarded_store_write,
    parse_toman,
)
from services.store_pricing import category_durations
from services.ux_format import parse_positive_int, to_persian_digits

_T = TypeVar("_T")

# --------------------------------------------------------------------------- #
# When a control shows
# --------------------------------------------------------------------------- #

# 🛒 فروش
SALE_VISIBLE = "visible"  # «نمایش به مشتری»: hides the group, so it stops the sale
SALE_NEW_SALE = "new_sale"  # «فروش جدید»: the stop-sale switch
SALE_RENEW = "renew"
SALE_FLAT = "flat"
SALE_DURATIONS = "durations"
SALE_TIERS = "tiers"
SALE_CUSTOM = "custom"
SALE_CUSTOM_HWID = "custom_hwid"
# 🎟 تست
TRIAL_SIZE = "trial"
TRIAL_RESELLER = "reseller_trial"
# 🖥 پنل
PANEL_GROUPS = "panel_groups"
PANEL_PREMIUM = "premium_groups"
# ✏️ گروه
GROUP_TITLE = "title"
GROUP_SORT = "sort"
GROUP_KIND = "kind"
GROUP_SUBGROUP = "subgroup"
GROUP_DELETE = "delete"
# The group dashboard's own buttons (they sit above the four sections)
PLAN_ADD = "plan_add"  # «➕ پلن جدید»
PLAN_ORDER = "plan_order"  # «↕️ چیدمان پلن‌ها»

# The two switches that stop selling a group. They stay in the bot (the plan's
# «stays in the bot» list: stop-sale of a group); the panel shows their state
# on the dashboard line «فروش» and draws no control for them.
STOP_SALE_CONTROLS = frozenset({SALE_VISIBLE, SALE_NEW_SALE})


def flag_kind(cat: Any) -> str:
    """The kind as the sale keyboard has always read it: the flag, lowercased, or empty."""
    flags = cat.sales_flags or {}
    return str(flags.get("kind") or flags.get("plan_type") or "").lower()


def visible_controls(
    cat: Any,
    *,
    has_plans: bool = False,
    has_children: bool = False,
    plan_count: int | None = None,
) -> frozenset[str]:
    """The settings controls that would do something for this group.

    Tiers and durations under the flat list, custom purchase on an unlimited
    group, the custom device limit without custom purchase, «تست نماینده» without
    a trial, the premium panel group without tiers, «نوع» once a group has plans
    or children, a sub-group under a group that holds plans or is itself a child,
    «پلن جدید» unless the group holds child groups, «چیدمان پلن‌ها» from two plans.
    ``plan_count`` is the number of plans (default: one when ``has_plans``).
    ``tests/unit/test_store_admin_layout.py`` pins each rule through the bot's
    keyboards and ``tests/unit/test_web_panel_store_settings.py`` through the page.
    """
    flags = cat.sales_flags or {}
    flat_on = bool(flags.get("flat_plan_list"))
    custom_mode = custom_purchase_mode(cat)
    out = {SALE_VISIBLE, SALE_NEW_SALE, SALE_RENEW, SALE_FLAT}
    if not flat_on:
        out.update({SALE_DURATIONS, SALE_TIERS})
    if not is_unlimited_like(cat):
        # An unlimited group never offers custom (`custom_purchase_offered`): the
        # setting would save and nothing would read it.
        out.add(SALE_CUSTOM)
    if custom_mode != CUSTOM_PURCHASE_OFF and flag_kind(cat) == "volume":
        out.add(SALE_CUSTOM_HWID)
    out.add(TRIAL_SIZE)
    if category_trial_shape(cat) is not None:
        out.add(TRIAL_RESELLER)  # a switch on a group with no trial saves nothing
    out.add(PANEL_GROUPS)
    if flags.get("tiers_enabled") and not flat_on:
        out.add(PANEL_PREMIUM)  # tiers are not offered (and not read) under the flat list
    out.update({GROUP_TITLE, GROUP_SORT, GROUP_DELETE})
    plans = plan_count if plan_count is not None else (1 if has_plans else 0)
    if not has_children:
        out.add(PLAN_ADD)  # a group holds child groups or plans, never both
        if plans >= 2:
            out.add(PLAN_ORDER)
    if not has_plans and not has_children:
        out.add(GROUP_KIND)  # it would relabel products already priced for the other kind
    if not has_plans and getattr(cat, "parent_id", None) is None:
        out.add(GROUP_SUBGROUP)
    return frozenset(out)


def sale_hint(cat: Any) -> str | None:
    """The sentence under the sale section's title, or None."""
    if flat_plan_list_enabled(cat):
        return texts.admin_store_flat_hint
    if not (cat.sales_flags or {}).get("tiers_enabled"):
        return texts.admin_store_cat_settings_hint
    return None


def kind_labels(cat: Any) -> tuple[str, str]:
    """«حجمی» → «نامحدود» for the kind button: the current kind and the one it becomes."""
    kind = flag_kind(cat)
    current = "حجمی" if kind == "volume" else ("نامحدود" if kind == "unlimited" else "نامشخص")
    other = "نامحدود" if kind == "volume" else "حجمی"
    return current, other


# --------------------------------------------------------------------------- #
# Parsing what an owner typed
# --------------------------------------------------------------------------- #


def parse_trial_input(raw: str) -> tuple[int, int] | None:
    """«1 1», «۱ ۱», «1،1» → (1, 1); a lone 0 → (0, 0) = off; anything else None."""
    parts = raw.replace("،", " ").replace(",", " ").split()
    values = [parse_toman(part) for part in parts]
    if values == [0]:
        return 0, 0
    if len(values) != 2 or any(v is None for v in values):
        return None
    gb, days = values
    assert gb is not None and days is not None
    return gb, days


def parse_trial_fields(gb_raw: str, days_raw: str) -> tuple[int, int] | None:
    """The panel's two boxes. Off is «0» in the first, whatever the second says."""
    gb = parse_toman(gb_raw)
    if gb == 0:
        return 0, 0
    days = parse_toman(days_raw)
    if gb is None or days is None:
        return None
    return gb, days


def require_number(raw: str) -> int:
    """A whole number ≥ 0 (a device limit: 0 is نامحدود), or the invalid-input line."""
    value = parse_toman(raw)
    if value is None:
        raise StoreAdminError(texts.admin_store_invalid_input)
    return value


def require_trial(gb_raw: str, days_raw: str) -> tuple[int, int]:
    parsed = parse_trial_fields(gb_raw, days_raw)
    if parsed is None:
        raise StoreAdminError(texts.admin_store_invalid_input)
    return parsed


def parse_sort_order(raw: str) -> int:
    """The display order as a whole number; anything else is the invalid-input line.

    ``int()`` keeps the bot's reading («+5», «-3», Persian digits). The 64-bit
    bound is the column's, not a product rule.
    """
    try:
        value = int(raw.strip())
    except ValueError:
        raise StoreAdminError(texts.admin_store_invalid_input) from None
    if abs(value) > INT64_MAX:
        raise StoreAdminError(texts.admin_store_invalid_input)
    return value


# --------------------------------------------------------------------------- #
# What a write does. Each one ends in commit_and_activate with the Persian note.
# --------------------------------------------------------------------------- #

NOTE_FLAGS = "تنظیمات گروه عوض شد"
NOTE_FLAT = "فهرست ساده گروه عوض شد"
NOTE_TIERS = "تفکیک ویژه/اقتصادی عوض شد"
NOTE_HWID = "کاربر همزمان خرید سفارشی عوض شد"
NOTE_GROUP_EDITED = "گروه محصول ویرایش شد"
NOTE_PANEL_GROUPS = "گروه پنل ویرایش شد"
NOTE_PREMIUM_GROUPS = "گروه‌های پنل ویژه ویرایش شد"

FLAG_ENABLED = "enabled"
FLAG_NEW_SALE = "allow_new_sale"
FLAG_RENEW = "allow_renew"
FLAG_CUSTOM = "custom_purchase_mode"
FLAG_RESELLER_TRIAL = "reseller_trial"
FLAG_KIND = "kind"


async def toggle_category_flag(
    session: AsyncSession, category_id: int, field: str
) -> StoreCategory:
    """Flip one of the group's flags and publish. The three-state custom mode cycles."""
    cat = await get_draft_category(session, category_id)
    flags = dict(cat.sales_flags or {})
    if field == FLAG_ENABLED:
        await update_draft_category(session, cat.id, enabled=not cat.enabled)
    elif field == FLAG_NEW_SALE:
        await update_draft_category(
            session, cat.id, allow_new_sale=not bool(flags.get("allow_new_sale", True))
        )
    elif field == FLAG_RENEW:
        await update_draft_category(
            session, cat.id, allow_renew=not bool(flags.get("allow_renew", True))
        )
    elif field == FLAG_CUSTOM:
        await update_draft_category(
            session,
            cat.id,
            custom_purchase_mode=next_custom_purchase_mode(custom_purchase_mode(cat)),
        )
    elif field == FLAG_RESELLER_TRIAL:
        await update_draft_category(
            session, cat.id, reseller_trial=not bool(flags.get("reseller_trial"))
        )
    elif field == FLAG_KIND:
        nxt = "unlimited" if (category_kind(cat) or "volume") == "volume" else "volume"
        await update_draft_category(session, cat.id, kind=nxt)
    await commit_and_activate(session, notes=NOTE_FLAGS)
    return cat


async def toggle_flat_plan_list(session: AsyncSession, category_id: int) -> StoreCategory:
    cat = await get_draft_category(session, category_id)
    cat = await set_category_flat_plan_list(
        session, cat.id, enabled=not flat_plan_list_enabled(cat)
    )
    await commit_and_activate(session, notes=NOTE_FLAT)
    return cat


async def toggle_tiers(session: AsyncSession, category_id: int) -> StoreCategory:
    cat = await get_draft_category(session, category_id)
    want = not bool((cat.sales_flags or {}).get("tiers_enabled"))
    cat = await set_category_tiers_enabled(session, int(category_id), enabled=want)
    await commit_and_activate(session, notes=NOTE_TIERS)
    return cat


async def save_category_trial(
    session: AsyncSession, category_id: int, *, gb: int, days: int
) -> StoreCategory:
    """The group's free trial; ``gb == 0`` switches it off."""
    cat = await set_category_trial(session, int(category_id), data_limit_gb=gb, duration_days=days)
    await commit_and_activate(session, notes=f"تست رایگان گروه «{cat.title_fa}» عوض شد")
    return cat


async def rename_category(session: AsyncSession, category_id: int, raw: str) -> StoreCategory:
    cat = await update_draft_category(session, int(category_id), title_fa=raw)
    await commit_and_activate(session, notes=NOTE_GROUP_EDITED)
    return cat


async def save_sort_order(session: AsyncSession, category_id: int, raw: str) -> StoreCategory:
    cat = await update_draft_category(session, int(category_id), sort_order=parse_sort_order(raw))
    await commit_and_activate(session, notes=NOTE_GROUP_EDITED)
    return cat


async def save_hwid_limit(
    session: AsyncSession, category_id: int, *, hwid_limit: int
) -> StoreCategory:
    """The device limit of a custom volume purchase; 0 is نامحدود and stays 0."""
    cat = await set_category_volume_hwid_limit(session, int(category_id), hwid_limit=hwid_limit)
    await commit_and_activate(session, notes=NOTE_HWID)
    return cat


def require_months(raw: str) -> int:
    """Whole months typed for a new duration tab; the service checks the range."""
    months = parse_positive_int(raw)
    if months is None:
        raise StoreAdminError(texts.admin_store_dur_invalid)
    return months


async def add_duration(
    session: AsyncSession, category_id: int, *, months: int | None
) -> StoreCategory:
    """``months=None`` adds «بدون انقضا»."""
    cat = await add_category_duration(session, int(category_id), months=months)
    label = duration_label_fa(None if months is None else months * 30)
    await commit_and_activate(session, notes=f"مدت {label} به «{cat.title_fa}» اضافه شد")
    return cat


async def remove_duration(
    session: AsyncSession, category_id: int, *, duration_days: int | None
) -> StoreCategory:
    cat = await remove_category_duration(session, int(category_id), duration_days=duration_days)
    label = duration_label_fa(duration_days)
    await commit_and_activate(session, notes=f"مدت {label} از «{cat.title_fa}» برداشته شد")
    return cat


async def save_premium_groups(
    session: AsyncSession, category_id: int, ids: list[int]
) -> tuple[StoreCategory, bool]:
    """Premium panel groups. The flag is True when emptying them switched tiers off."""
    cat, disabled = await set_category_premium_panel_groups(session, int(category_id), ids)
    await commit_and_activate(session, notes=NOTE_PREMIUM_GROUPS)
    return cat, disabled


async def save_panel_groups(
    session: AsyncSession, settings: Settings, category_id: int, ids: list[int]
) -> list[int]:
    """The group's own panel groups. Returns the groups a purchase got *before*.

    The caller passes that to ``services_left_on_old_groups`` after the save: a
    change of target leaves existing services on the old group until they are
    moved (a panel write, which only the bot does).
    """
    groups_before = await target_groups_for_category(session, settings, category_id)
    await update_draft_category(session, int(category_id), panel_group_ids=ids)
    await commit_and_activate(session, notes=NOTE_PANEL_GROUPS)
    return groups_before


async def services_left_on_old_groups(
    session: AsyncSession, settings: Settings, category_id: int, groups_before: list[int]
) -> int:
    """How many live services sit on the old panel groups after a change of target."""
    groups_after = await target_groups_for_category(session, settings, category_id)
    if groups_after and groups_after != groups_before:
        return len(await accounts_of_category(session, category_id))
    return 0


async def panel_groups_stranded(
    session: AsyncSession, settings: Settings, category_id: int, ids: list[int]
) -> int:
    """How many live services a change to ``ids`` would leave on the old groups. Writes nothing.

    The same question ``services_left_on_old_groups`` answers after the bot's own
    change, asked *before* the write: the groups a purchase gets now, the groups it
    would get with ``ids`` on this group (its own, else its parent's: the rule
    ``published_category_groups`` states for rows already in memory), and the
    accounts of the group when the two differ and the new target is not empty.
    The panel refuses a save where this is above zero; the bot saves and then asks
    whether to move them.
    """
    before = await target_groups_for_category(session, settings, category_id)
    draft = await get_or_create_draft(session)
    cat = next((c for c in draft.categories if int(c.id) == int(category_id)), None)
    if cat is None:
        return 0
    stand_in = SimpleNamespace(panel_group_ids=list(ids), parent_id=cat.parent_id)
    after = published_category_groups(draft.categories, stand_in)
    if after and after != before:
        return len(await accounts_of_category(session, category_id))
    return 0


# --------------------------------------------------------------------------- #
# The panel's way in
# --------------------------------------------------------------------------- #

TEXT_GROUP_MISSING = "گروه محصول یافت نشد؛ شاید همین الان حذف شده."


async def edit_group_setting(
    session: AsyncSession,
    settings: Settings,
    telegram_id: int | None,
    *,
    expected_version: int | None,
    group_slug: str,
    control: str,
    apply: Callable[[AsyncSession, StoreCategory], Awaitable[_T]],
) -> _T:
    """A panel write to one setting of one group, named by its slug.

    Owner only and current version only (``guarded_store_write``), and only for
    a control the group shows right now: a form for a control that is hidden is
    a page that is out of date, and is refused the way a stale version is. The
    stop-sale switches are never written from here.
    """
    if control in STOP_SALE_CONTROLS:
        raise ValueError(f"{control} stays in the bot")

    async def action(s: AsyncSession) -> _T:
        draft = await get_or_create_draft(s)
        cat = next((c for c in draft.categories if str(c.slug) == group_slug), None)
        if cat is None:
            raise StoreAdminError(TEXT_GROUP_MISSING)
        plans_here = sum(1 for p in draft.packages if p.category_id == cat.id)
        shown = visible_controls(
            cat,
            has_plans=plans_here > 0,
            has_children=any(c.parent_id == cat.id for c in draft.categories),
            plan_count=plans_here,
        )
        if control not in shown:
            raise StaleStoreVersion(TEXT_STALE)
        return await apply(s, cat)

    return await guarded_store_write(
        session, settings, telegram_id, expected_version=expected_version, action=action
    )


# --------------------------------------------------------------------------- #
# What the panel draws
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DurationTab:
    days: int | None
    key: str  # «none» for بدون انقضا, else the day count: the remove route's path part
    label: str


@dataclass(frozen=True)
class GroupSettings:
    """Current values of a group's settings, as the sections show them."""

    controls: frozenset[str]
    title: str
    sort_order: int
    renew: bool
    flat: bool
    tiers: bool
    custom_label: str
    hwid_label: str
    hwid_limit: int
    durations: tuple[DurationTab, ...]
    sale_hint: str | None
    trial_gb: int
    trial_days: int
    trial_label: str
    reseller_trial: bool
    panel_label: str
    panel_ids: str
    premium_ids: str
    kind_now: str
    kind_other: str


def duration_key(days: int | None) -> str:
    return "none" if days is None else str(int(days))


def parse_duration_key(key: str) -> tuple[bool, int | None]:
    """The remove route's path part → ``(valid, days)``."""
    if key == "none":
        return True, None
    if key.isascii() and key.isdecimal() and 0 < int(key) <= INT64_MAX:
        return True, int(key)
    return False, None


def current_panel_group_ids(cat: StoreCategory, control: str) -> list[int]:
    """The panel group ids a group has now: its own (``PANEL_GROUPS``) or the premium ones.

    What the typed box is filled with and what the panel-group picker pre-checks.
    """
    if control == PANEL_PREMIUM:
        raw = (cat.sales_flags or {}).get("premium_panel_group_ids")
    else:
        raw = cat.panel_group_ids
    return [int(i) for i in (raw or [])]


def group_settings(view: CatalogView, cat: StoreCategory) -> GroupSettings:
    flags = cat.sales_flags or {}
    trial = category_trial_shape(cat)
    kind_now, kind_other = kind_labels(cat)
    return GroupSettings(
        controls=visible_controls(
            cat,
            has_plans=bool(view.packages_of(cat.id)),
            has_children=bool(view.children_of(cat.id)),
            plan_count=len(view.packages_of(cat.id)),
        ),
        title=str(cat.title_fa),
        sort_order=int(cat.sort_order),
        renew=bool(flags.get("allow_renew", True)),
        flat=flat_plan_list_enabled(cat),
        tiers=bool(flags.get("tiers_enabled")),
        custom_label=CUSTOM_STATE_FA[custom_purchase_mode(cat)],
        hwid_label=hwid_limit_label_fa(category_volume_hwid_limit(cat)),
        hwid_limit=category_volume_hwid_limit(cat),
        durations=tuple(
            DurationTab(days=d, key=duration_key(d), label=duration_label_fa(d))
            for d in category_durations(cat)
        ),
        sale_hint=sale_hint(cat),
        trial_gb=trial[0] if trial else 0,
        trial_days=trial[1] if trial else 0,
        trial_label=(
            "خاموش"
            if trial is None
            else f"{to_persian_digits(trial[0])} گیگ • {to_persian_digits(trial[1])} روز"
        ),
        reseller_trial=category_reseller_trial(cat),
        panel_label=panel_group_label(cat, parent_of(view, cat), web=True),
        panel_ids=", ".join(str(i) for i in current_panel_group_ids(cat, PANEL_GROUPS)),
        premium_ids=", ".join(str(i) for i in current_panel_group_ids(cat, PANEL_PREMIUM)),
        kind_now=kind_now,
        kind_other=kind_other,
    )

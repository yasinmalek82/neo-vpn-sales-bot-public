"""Plan and group writes shared by the bot's store screens and the Mini App.

The bot's plan wizard, plan detail screen, plan-order screen and group creation and
deletion kept their parsing, defaults and change-log notes inside
``bot/handlers/store_admin.py``. The Mini App needs the same writes, and a second
copy of a note or of a range is how the two would disagree (and how a row in
«تغییرات فروشگاه» would read differently depending on where it was made). So the
logic lives here and both call it:

* **Reading what an owner typed** (``read_plan_number``, ``read_hwid``, ``read_price``,
  ``read_group_title``): the bot's own refusal lines, ``check_dim`` and ``DIM_LIMITS``
  for every dimension.
* **What a write does** (``create_package``, ``set_package_dims``, ``toggle_package_flag``,
  ``remove_package``, ``move_package``, ``create_group`` …): validate, write the draft
  through ``services/store_admin.py`` and publish with the Persian note.
* **Which controls a plan shows** (``visible_plan_controls``): the bot's plan keyboard and
  the panel's editor both ask it.
* **The panel's way in** (``edit_plan``): owner only, current store version only, the group
  and the plan by slug, and only a control the plan shows.

Nothing here knows Telegram or HTTP. A refusal is a ``StoreAdminError`` carrying the
bot's own Persian line, so a caller prints ``str(exc)``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.texts import texts
from database.models.store import StoreCategory, StorePackage
from services.store_admin import (
    CatalogView,
    StoreAdminError,
    check_dim,
    commit_and_activate,
    create_draft_category,
    create_draft_package,
    delete_draft_category,
    delete_draft_package,
    get_catalog_view,
    get_draft_category,
    get_draft_package,
    get_or_create_draft,
    move_draft_package,
    set_category_plan_order_mode,
    set_package_hwid_limit,
    update_draft_package,
)
from services.store_catalog import (
    category_kind,
    duration_has_tab,
    plan_duration_label_fa,
    plan_order_mode,
)
from services.store_dashboard import retail_cell
from services.store_edit import (
    PACKAGE_EDIT_NOTE,
    TEXT_STALE,
    StaleStoreVersion,
    guarded_store_write,
    parse_toman,
    price_text,
)
from services.store_group_settings import TEXT_GROUP_MISSING
from services.ux_format import to_persian_digits

_T = TypeVar("_T")

TEXT_PLAN_MISSING = "پلن یافت نشد؛ شاید همین الان حذف شده."

# --------------------------------------------------------------------------- #
# Notes: «تغییرات فروشگاه» reads these back to the owner, so Persian
# --------------------------------------------------------------------------- #

NOTE_PLAN_FLAGS = "تنظیمات پلن عوض شد"
NOTE_PLAN_DIMS = "حجم و مدت پلن عوض شد"
NOTE_PLAN_HWID = "کاربر همزمان پلن عوض شد"
NOTE_GROUP_CREATED = "گروه محصول ساخته شد"
NOTE_SUBGROUP_CREATED = "زیرگروه ساخته شد"


def note_plan_created(title: str) -> str:
    return f"پلن «{title}» ساخته شد"


def note_plan_deleted(title: str) -> str:
    return f"پلن «{title}» حذف شد"


def note_group_deleted(title: str) -> str:
    return f"گروه «{title}» حذف شد"


def note_order_mode(title: str, mode: str) -> str:
    return f"ترتیب پلن‌های «{title}» " + ("دستی شد" if mode == "manual" else "خودکار شد")


def note_plan_moved(plan_title: str, group_title: str) -> str:
    return f"جابه‌جایی «{plan_title}» در «{group_title}»"


# --------------------------------------------------------------------------- #
# Reading what an owner typed
# --------------------------------------------------------------------------- #

PLAN_TITLE_MAX = 128


def parse_positive(raw: str) -> int | None:
    """One positive integer, Persian digits and separators accepted, else ``None``.

    Returns None rather than raising so the raw Python ``ValueError`` text never
    reaches the Persian UI.
    """
    value = parse_toman(raw)
    if value is None or value <= 0:
        return None
    return value


def read_plan_number(key: str, raw: str, *, example: str) -> int:
    """A plan's size or duration: a positive whole number inside ``DIM_LIMITS[key]``.

    Not a number is «فقط یک عدد بفرست»; a number outside the range is the range line
    ``check_dim`` writes. The wizard asks this at each question and the write asks
    ``check_dim`` again, so a value accepted here is never refused at the confirm.
    """
    value = parse_positive(raw)
    if value is None:
        raise StoreAdminError(texts.admin_store_number_invalid.format(example=example))
    check_dim(key, value)
    return value


def read_hwid(raw: str) -> int:
    """The device limit: a whole number from 0, and **0 is نامحدود** (never read as 1)."""
    value = parse_toman(raw)
    if value is None:
        raise StoreAdminError(texts.admin_store_invalid_input)
    check_dim("hwid_limit", value)
    return value


def read_price(raw: str) -> int:
    """A new plan's price in toman: a positive whole number."""
    value = parse_positive(raw)
    if value is None:
        raise StoreAdminError(texts.admin_store_number_invalid.format(example="150000"))
    return value


def read_group_title(raw: str) -> str:
    """A group's title: not empty, at most 128 characters, else the invalid-input line."""
    title = raw.strip()
    if not title or len(title) > PLAN_TITLE_MAX:
        raise StoreAdminError(texts.admin_store_invalid_input)
    return title


def read_plan_title(raw: str) -> str:
    """A pending plan's new title (the wizard's rename): same bounds as a group's."""
    return read_group_title(raw)


# --------------------------------------------------------------------------- #
# The plan's shape
# --------------------------------------------------------------------------- #


def plan_kind_of(cat: StoreCategory | None) -> str:
    """``volume`` or ``unlimited``: what the group sells, which decides a plan's dimensions."""
    return (category_kind(cat) if cat is not None else None) or "volume"


def dim_keys(kind: str) -> tuple[str, str]:
    """``(size key, duration key)`` in a plan's ``options`` for this kind of group."""
    if kind == "unlimited":
        return "concurrent_users", "duration_months"
    return "volume_gb", "duration_days"


def derive_plan_title(*, kind: str, size: int, duration: int | None) -> str:
    """«۵۰ گیگ / ۳۰ روز» — the owner can rename it."""
    if kind == "unlimited":
        return f"{to_persian_digits(size)} کاربره / {to_persian_digits(duration or 0)} ماه"
    if duration is None:
        return f"{to_persian_digits(size)} گیگ بدون انقضا"
    return f"{to_persian_digits(size)} گیگ / {to_persian_digits(duration)} روز"


def duration_in_days(kind: str, duration: int | None) -> int | None:
    """The plan's duration as the duration tabs count it (months are 30 days)."""
    if kind == "unlimited":
        return int(duration or 0) * 30
    return None if duration is None else int(duration)


def no_tab_warning(cat: StoreCategory | None, *, kind: str, duration: int | None) -> str | None:
    """The bot's warning when the shelf has no tab for this plan's duration, or ``None``.

    Duration tabs are the owner's own setting, so a plan outside them is warned
    about and not given a tab (``.claude/rules/store-admin-ui.md``).
    """
    days = duration_in_days(kind, duration)
    if cat is None or duration_has_tab(cat, days):
        return None
    return texts.admin_store_new_pkg_warn_no_tab.format(
        duration=plan_duration_label_fa(duration_days=days)
    )


def plan_options(
    kind: str, *, size: int, duration: int | None, hwid: int | None = None
) -> dict[str, Any]:
    """The ``options`` a ready plan carries; what ``choose_ready_plan`` needs to honour its price."""
    size_key, duration_key = dim_keys(kind)
    opts: dict[str, Any] = {size_key: size, duration_key: duration}
    if kind != "unlimited":
        opts["hwid_limit"] = 0 if hwid is None else int(hwid)
    return opts


# --------------------------------------------------------------------------- #
# What a plan shows
# --------------------------------------------------------------------------- #

PLAN_FLAGS = "flags"  # the four on/off switches
PLAN_TITLE = "title"
PLAN_PRICE = "price"
PLAN_DIMS = "dims"
PLAN_HWID = "hwid"
PLAN_DELETE = "delete"

PLAN_FLAG_FIELDS = ("enabled", "allow_new_sale", "allow_renew", "forbid_extra_discount")


def visible_plan_controls(pkg: Any) -> frozenset[str]:
    """The controls that would do something for this plan.

    A ready plan has a price and dimensions; the device limit is only for a volume
    plan. The bot's plan keyboard and the panel's plan editor both ask this.
    """
    out = {PLAN_FLAGS, PLAN_TITLE, PLAN_DELETE}
    kind_val = pkg.kind.value if hasattr(pkg.kind, "value") else str(pkg.kind)
    if kind_val == "ready":
        out.update({PLAN_PRICE, PLAN_DIMS})
        opts = pkg.options or {}
        if opts.get("volume_gb") is not None or opts.get("data_limit_gb") is not None:
            out.add(PLAN_HWID)
    return frozenset(out)


# --------------------------------------------------------------------------- #
# What a write does. Each one ends in commit_and_activate with the Persian note.
# --------------------------------------------------------------------------- #


async def create_package(
    session: AsyncSession,
    *,
    category_id: int,
    kind: str,
    size: int,
    duration: int | None,
    price: int,
    hwid: int | None = None,
    title: str | None = None,
) -> StorePackage:
    """A new ready plan with its dimensions in ``options``, live at once.

    ``duration`` is days for a volume group and months for an unlimited one
    (``None``: بدون انقضا). A blank ``title`` is derived from the dimensions.
    """
    final_title = (title or "").strip() or derive_plan_title(
        kind=kind, size=size, duration=duration
    )
    size_key, duration_key = dim_keys(kind)
    dims: dict[str, Any] = {size_key: size, duration_key: duration}
    if kind != "unlimited":
        dims["hwid_limit"] = 0 if hwid is None else int(hwid)
    pkg = await create_draft_package(
        session,
        category_id=int(category_id),
        title_fa=final_title,
        fixed_price_toman=int(price),
        **dims,
    )
    await commit_and_activate(session, notes=note_plan_created(final_title))
    return pkg


async def set_package_title(session: AsyncSession, package_id: int, title: str) -> StorePackage:
    pkg = await update_draft_package(session, int(package_id), title_fa=title)
    await commit_and_activate(session, notes=PACKAGE_EDIT_NOTE)
    return pkg


async def set_package_dims(
    session: AsyncSession, package_id: int, *, kind: str, size: int, duration: int | None
) -> StorePackage:
    """A plan's size and duration; ``duration=None`` clears the expiry."""
    size_key, duration_key = dim_keys(kind)
    dims: dict[str, Any] = {size_key: size, duration_key: duration}
    pkg = await update_draft_package(
        session, int(package_id), clear_duration=duration is None, **dims
    )
    await commit_and_activate(session, notes=NOTE_PLAN_DIMS)
    return pkg


async def save_package_hwid(
    session: AsyncSession, package_id: int, *, hwid_limit: int
) -> StorePackage:
    """The device limit of a ready volume plan; 0 is نامحدود and stays 0."""
    pkg = await set_package_hwid_limit(session, int(package_id), hwid_limit=hwid_limit)
    await commit_and_activate(session, notes=NOTE_PLAN_HWID)
    return pkg


async def toggle_package_flag(session: AsyncSession, package_id: int, field: str) -> StorePackage:
    """Flip one of the plan's four switches and publish."""
    if field not in PLAN_FLAG_FIELDS:
        raise ValueError(f"{field} is not a plan switch")
    pkg = await get_draft_package(session, int(package_id))
    changes: dict[str, Any] = {field: not getattr(pkg, field)}
    await update_draft_package(session, pkg.id, **changes)
    await commit_and_activate(session, notes=NOTE_PLAN_FLAGS)
    return pkg


async def remove_package(session: AsyncSession, package_id: int) -> tuple[str, int | None]:
    """Delete a plan from the live catalog. Returns its title and its group's id."""
    view = await get_catalog_view(session)
    pkg = view.package(int(package_id))
    title = str(pkg.title_fa) if pkg else "—"
    category_id = int(pkg.category_id) if pkg else None
    await delete_draft_package(session, int(package_id))
    await commit_and_activate(session, notes=note_plan_deleted(title))
    return title, category_id


def package_delete_confirm(pkg: StorePackage, prices: list[Any], *, quote: Any = None) -> str:
    """The bot's «حذف پلن» sentence with the plan's current price (``quote`` escapes the title)."""
    cell = retail_cell(prices, pkg)
    price = price_text(cell) if cell is not None else "قیمت‌گذاری پویا"
    title = str(pkg.title_fa)
    return texts.admin_store_pkg_del_confirm.format(
        title=quote(title) if quote else title, price=price
    )


async def toggle_plan_order(session: AsyncSession, category_id: int) -> StoreCategory:
    """Switch the group's plan order between automatic and manual and publish."""
    cat = await get_draft_category(session, int(category_id))
    new_mode = "auto" if plan_order_mode(cat) == "manual" else "manual"
    cat = await set_category_plan_order_mode(session, cat.id, mode=new_mode)
    await commit_and_activate(session, notes=note_order_mode(str(cat.title_fa), new_mode))
    return cat


@dataclass(frozen=True)
class MoveResult:
    """``moved`` or ``at_edge``; ``flipped`` when the first move switched the group to manual."""

    status: str
    flipped: bool
    category_id: int


async def move_package(session: AsyncSession, package_id: int, *, direction: str) -> MoveResult:
    """Swap a plan with its neighbour (the first move freezes the order into manual).

    Moving the top plan up changes nothing and publishes nothing, unless the tap
    already flipped the group to manual: that freeze is persisted.
    """
    status, flipped, pkg, cat = await move_draft_package(
        session, int(package_id), direction=direction
    )
    if status == "at_edge":
        if flipped:
            await commit_and_activate(session, notes=f"ترتیب پلن‌های «{cat.title_fa}» دستی شد")
    else:
        await commit_and_activate(
            session, notes=note_plan_moved(str(pkg.title_fa), str(cat.title_fa))
        )
    return MoveResult(status=status, flipped=flipped, category_id=int(cat.id))


async def create_group(session: AsyncSession, raw_title: str, kind: str) -> StoreCategory:
    """A new top-level group of ``kind`` (``volume`` or ``unlimited``), live at once."""
    title = raw_title.strip()
    if not title:
        raise StoreAdminError(texts.admin_store_invalid_input)
    cat = await create_draft_category(session, title_fa=title, kind=kind)
    await commit_and_activate(session, notes=NOTE_GROUP_CREATED)
    return cat


async def create_subgroup(
    session: AsyncSession, parent_id: int, raw_title: str
) -> tuple[StoreCategory, StoreCategory]:
    """A child group under ``parent_id``, of the parent's kind. Returns ``(child, parent)``.

    The parent is read afresh: an id from a screen drawn before the last publish
    would otherwise orphan the child. Two levels at most, and a group holding
    plans takes no child; ``create_draft_category`` refuses both.
    """
    title = read_group_title(raw_title)
    view = await get_catalog_view(session)
    parent = view.category(int(parent_id))
    if parent is None:
        raise StoreAdminError(texts.admin_store_lost_context)
    cat = await create_draft_category(
        session, title_fa=title, kind=plan_kind_of(parent), parent_id=int(parent.id)
    )
    await commit_and_activate(session, notes=NOTE_SUBGROUP_CREATED)
    return cat, parent


def group_delete_scope(view: CatalogView, cat: StoreCategory) -> str:
    """What deleting this group takes with it, as the bot's confirm says it."""
    children = view.children_of(cat.id)
    if children:
        child_plans = sum(len(view.packages_of(c.id)) for c in children)
        return texts.admin_store_cat_del_scope_children.format(c=len(children), n=child_plans)
    plans = view.packages_of(cat.id)
    if plans:
        return texts.admin_store_cat_del_scope_plans.format(n=len(plans))
    return texts.admin_store_cat_del_scope_plain


def group_delete_confirm(view: CatalogView, cat: StoreCategory, *, quote: Any = None) -> str:
    title = str(cat.title_fa)
    return texts.admin_store_cat_del_confirm.format(
        title=quote(title) if quote else title, scope=group_delete_scope(view, cat)
    )


async def remove_group(session: AsyncSession, category_id: int) -> str:
    """Delete a group with its child groups and their plans. Returns its title.

    The last top-level group cannot go (the store would publish with none).
    Past orders keep their own price, so nothing else is touched.
    """
    view = await get_catalog_view(session)
    cat = view.category(int(category_id))
    title = str(cat.title_fa) if cat else "—"
    await delete_draft_category(session, int(category_id))
    await commit_and_activate(session, notes=note_group_deleted(title))
    return title


# --------------------------------------------------------------------------- #
# A whole form at once (the panel's boxes), through the same readers
# --------------------------------------------------------------------------- #

SIZE_EXAMPLE = {"volume": "15", "unlimited": "3"}
DURATION_EXAMPLE = {"volume": "45", "unlimited": "2"}


def read_size_and_duration(kind: str, form: Mapping[str, Any]) -> tuple[int, int | None]:
    """The size and duration boxes of a plan, as the bot's two questions read them.

    «بدون انقضا» is for a volume plan only (the bot's duration keyboard offers it
    nowhere else); an unlimited-users plan always has months.
    """
    size_key, duration_key = dim_keys(kind)
    size = read_plan_number(size_key, str(form.get("size", "")), example=SIZE_EXAMPLE[kind])
    if str(form.get("no_expiry", "")) == "1":
        if kind == "unlimited":
            raise StoreAdminError(texts.admin_store_invalid_input)
        return size, None
    duration = read_plan_number(
        duration_key, str(form.get("duration", "")), example=DURATION_EXAMPLE[kind]
    )
    return size, duration


async def create_package_from_form(
    session: AsyncSession, cat: StoreCategory, form: Mapping[str, Any]
) -> StorePackage:
    """The panel's «ساخت پلن»: every box read by the bot's readers, then ``create_package``."""
    kind = plan_kind_of(cat)
    size, duration = read_size_and_duration(kind, form)
    hwid = read_hwid(str(form.get("hwid", ""))) if kind != "unlimited" else None
    price = read_price(str(form.get("price", "")))
    raw_title = str(form.get("title", "")).strip()
    title = read_plan_title(raw_title) if raw_title else None
    return await create_package(
        session,
        category_id=int(cat.id),
        kind=kind,
        size=size,
        duration=duration,
        hwid=hwid,
        price=price,
        title=title,
    )


async def set_package_dims_from_form(
    session: AsyncSession, pkg: StorePackage, form: Mapping[str, Any]
) -> StorePackage:
    """The panel's «حجم و مدت» form for a saved plan (its group decides the kind)."""
    cat = await get_draft_category(session, int(pkg.category_id))
    kind = plan_kind_of(cat)
    size, duration = read_size_and_duration(kind, form)
    return await set_package_dims(session, int(pkg.id), kind=kind, size=size, duration=duration)


# --------------------------------------------------------------------------- #
# The panel's way in
# --------------------------------------------------------------------------- #


async def edit_plan(
    session: AsyncSession,
    settings: Settings,
    telegram_id: int | None,
    *,
    expected_version: int | None,
    group_slug: str,
    plan_slug: str,
    control: str,
    apply: Callable[[AsyncSession, StorePackage], Awaitable[_T]],
) -> _T:
    """A panel write to one control of one plan, named by group and plan slug.

    Owner only and current version only (``guarded_store_write``); the plan must be in
    that group and the control one the plan shows right now, else it is refused the way
    a stale form is.
    """

    async def action(s: AsyncSession) -> _T:
        draft = await get_or_create_draft(s)
        cat = next((c for c in draft.categories if str(c.slug) == group_slug), None)
        if cat is None:
            raise StoreAdminError(TEXT_GROUP_MISSING)
        pkg = next(
            (
                p
                for p in draft.packages
                if str(p.slug) == plan_slug and int(p.category_id) == int(cat.id)
            ),
            None,
        )
        if pkg is None:
            raise StoreAdminError(TEXT_PLAN_MISSING)
        if control not in visible_plan_controls(pkg):
            raise StaleStoreVersion(TEXT_STALE)
        return await apply(s, pkg)

    return await guarded_store_write(
        session, settings, telegram_id, expected_version=expected_version, action=action
    )

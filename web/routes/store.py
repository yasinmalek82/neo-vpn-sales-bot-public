"""«🛍 مدیریت فروشگاه»: the groups, a group's dashboard, its prices and its settings.

Reads are the bot's reads (``get_catalog_view`` through ``services/store_dashboard``).
Every write is a ``POST`` into one service function the bot's own control calls
(``services/store_edit.edit_package_price`` for a price, ``services/store_group_settings``
for the four sections 🛒 فروش, 🎟 تست, 🖥 پنل, ✏️ گروه). A page carries the published
store version it was drawn from and the plan and group by slug; the service refuses
a write against any other version. A GET never writes, and a route here holds no
price, limit or validation rule.

The stop-sale switches («فروش جدید», «نمایش به مشتری») have no route here: they
stay in the bot.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession

from bot.texts import texts
from database.models.store import StoreCategory, StorePackage
from services.panel_group_list import panel_group_choices, picker_rows
from services.store_admin import (
    StoreAdminError,
    StoreOwnerRequired,
    count_prices_for_duration,
    duration_label_fa,
    get_catalog_view,
    get_or_create_draft,
    parse_panel_group_ids,
)
from services.store_changes import (
    ask_undo,
    change_rows,
    confirm_undo,
    undo_confirm_text,
    undone_text,
)
from services.store_config import is_store_owner
from services.store_dashboard import (
    find_group,
    first_group_slug,
    group_facts,
    group_tree,
    hub_body,
    plan_entries,
    plan_order_view,
)
from services.store_edit import (
    TEXT_STALE,
    PriceJumpNeedsConfirm,
    StaleStoreVersion,
    edit_package_price,
    guarded_store_write,
    published_store_version,
)
from services.store_group_settings import (
    FLAG_CUSTOM,
    FLAG_KIND,
    FLAG_RENEW,
    FLAG_RESELLER_TRIAL,
    GROUP_DELETE,
    GROUP_KIND,
    GROUP_SORT,
    GROUP_SUBGROUP,
    GROUP_TITLE,
    PANEL_GROUPS,
    PANEL_PREMIUM,
    PLAN_ADD,
    PLAN_ORDER,
    SALE_CUSTOM,
    SALE_CUSTOM_HWID,
    SALE_DURATIONS,
    SALE_FLAT,
    SALE_RENEW,
    SALE_TIERS,
    TRIAL_RESELLER,
    TRIAL_SIZE,
    add_duration,
    current_panel_group_ids,
    edit_group_setting,
    group_settings,
    panel_groups_stranded,
    parse_duration_key,
    remove_duration,
    rename_category,
    require_months,
    require_number,
    require_trial,
    save_category_trial,
    save_hwid_limit,
    save_panel_groups,
    save_premium_groups,
    save_sort_order,
    toggle_category_flag,
    toggle_flat_plan_list,
    toggle_tiers,
)
from services.store_plan_edit import (
    PLAN_DELETE,
    PLAN_DIMS,
    PLAN_FLAG_FIELDS,
    PLAN_FLAGS,
    PLAN_HWID,
    PLAN_TITLE,
    TEXT_PLAN_MISSING,
    create_group,
    create_package_from_form,
    create_subgroup,
    edit_plan,
    group_delete_confirm,
    move_package,
    package_delete_confirm,
    read_hwid,
    remove_group,
    remove_package,
    save_package_hwid,
    set_package_dims_from_form,
    set_package_title,
    toggle_package_flag,
    toggle_plan_order,
)
from services.store_plan_view import build_plan_editor, preview_plan
from services.store_strategy import list_store_changes
from services.ux_format import strip_html_tags, to_persian_digits
from web.keys import NUMERIC_ID_PATTERN, PANEL_PREFIX, SESSION_FACTORY, SETTINGS
from web.templating import render

STORE_URL = f"{PANEL_PREFIX}/store"
SETTINGS_URL = f"{STORE_URL}/g/{{group_slug}}/settings"
CHANGES_URL = f"{STORE_URL}/changes"

SECTION_SALE = "sale"
SECTION_TRIAL = "trial"
SECTION_PANEL = "panel"
SECTION_GROUP = "group"
SECTION_ORDER = "order"


async def _store_page(
    request: web.Request,
    group_slug: str | None,
    *,
    notice: str | None = None,
    notice_is_error: bool = False,
    notice_section: str | None = None,
    status: int = 200,
) -> web.Response:
    owner = is_store_owner(request.app[SETTINGS], request["telegram_id"])
    async with request.app[SESSION_FACTORY]() as session:
        view = await get_catalog_view(session)
        version = await published_store_version(session)
    if not view.exists or not view.categories:
        return render(request, "store.html", empty=texts.admin_store_no_catalog, status=status)
    tree = group_tree(view)
    cat = find_group(view, group_slug or first_group_slug(tree) or "")
    if group_slug and cat is None:
        return render(
            request,
            "store_refused.html",
            status=404,
            message="گروه محصول یافت نشد.",
            reload_url=STORE_URL,
        )
    return render(
        request,
        "store.html",
        status=status,
        empty=None,
        hub_text=hub_body(view, is_owner=owner).partition("\n")[2].lstrip(),
        tree=tree,
        selected=cat.slug if cat else None,
        facts=group_facts(view, cat, web=True) if cat else None,
        plans=plan_entries(view, cat) if cat else [],
        order=plan_order_view(cat) if cat else None,
        gs=group_settings(view, cat) if cat and owner else None,
        premium_title=texts.admin_store_tier_groups,
        can_edit=owner,
        version=version,
        notice=notice,
        notice_is_error=notice_is_error,
        notice_section=notice_section,
        store_url=STORE_URL,
    )


async def store_home(request: web.Request) -> web.Response:
    return await _store_page(request, None)


async def store_group(request: web.Request) -> web.Response:
    return await _store_page(request, request.match_info["group_slug"])


def _refused(request: web.Request, status: int, message: str, group_slug: str) -> web.Response:
    return render(
        request,
        "store_refused.html",
        status=status,
        message=message,
        reload_url=f"{STORE_URL}/g/{group_slug}",
    )


def _form_version(form: Any) -> int | None:
    try:
        return int(str(form.get("version", "")))
    except ValueError:
        return None


async def _refusal_page(
    request: web.Request, refusal: StoreAdminError, group_slug: str, section: str | None
) -> web.Response:
    """The one mapping from a refused write to a page: owner 403, stale 409, input 400."""
    if isinstance(refusal, StoreOwnerRequired):
        return _refused(request, 403, texts.admin_store_owner_only, group_slug)
    if isinstance(refusal, StaleStoreVersion):
        return _refused(request, 409, str(refusal), group_slug)
    message = strip_html_tags(str(refusal)) or texts.admin_store_invalid_input
    return await _store_page(
        request,
        group_slug,
        notice=message,
        notice_is_error=True,
        notice_section=section,
        status=400,
    )


async def store_edit_price(request: web.Request) -> web.Response:
    group_slug = request.match_info["group_slug"]
    plan_slug = request.match_info["plan_slug"]
    form = await request.post()
    raw_price = str(form.get("price", ""))
    from_editor = str(form.get("origin", "")) == "editor"
    refusal: StoreAdminError | None = None
    async with request.app[SESSION_FACTORY]() as session:
        try:
            await edit_package_price(
                session,
                request.app[SETTINGS],
                request["telegram_id"],
                expected_version=_form_version(form),
                package_slug=plan_slug,
                raw_price=raw_price,
                confirmed=str(form.get("confirm", "")) == "1",
            )
        except PriceJumpNeedsConfirm as ask:
            # 10x or a tenth of the current price: nothing was written. Ask once,
            # with the old and the new price; the second request carries confirm=1.
            return render(
                request,
                "store_confirm.html",
                message=str(ask),
                version=_form_version(form),
                post_url=request.path,
                fields={"price": raw_price, **({"origin": "editor"} if from_editor else {})},
                confirm_label="✅ بله، همین قیمت",
                confirm_class="",
                cancel_url=(
                    f"{STORE_URL}/g/{group_slug}/plans/{plan_slug}"
                    if from_editor
                    else f"{STORE_URL}/g/{group_slug}"
                ),
            )
        except StoreAdminError as exc:  # owner, stale and invalid input all derive from it
            refusal = exc
    if from_editor:
        return await _editor_result(request, group_slug, plan_slug, "price", refusal)
    if refusal is not None:
        return await _refusal_page(request, refusal, group_slug, None)
    return await _store_page(request, group_slug, notice=texts.admin_store_saved)


# --------------------------------------------------------------------------- #
# Group settings: the bot's four sections
# --------------------------------------------------------------------------- #

SAVED = f"✅ {texts.admin_store_saved}"

# A form's `apply` gets the draft group, the posted form and a list it may add
# follow-up lines to (shown under the saved line).
Apply = Callable[[AsyncSession, StoreCategory, Any, list[str]], Awaitable[Any]]


async def _write(request: web.Request, *, section: str, control: str, apply: Apply) -> web.Response:
    """Run one settings write through the shared guard and redraw the section.

    ``edit_group_setting`` is the guard: owner only, current store version only,
    the group by slug, and only a control the group shows right now.
    """
    group_slug = request.match_info["group_slug"]
    form = await request.post()
    follow_up: list[str] = []
    refusal: StoreAdminError | None = None
    async with request.app[SESSION_FACTORY]() as session:
        try:
            await edit_group_setting(
                session,
                request.app[SETTINGS],
                request["telegram_id"],
                expected_version=_form_version(form),
                group_slug=group_slug,
                control=control,
                apply=lambda s, cat: apply(s, cat, form, follow_up),
            )
        except StoreAdminError as exc:
            refusal = exc
    if refusal is not None:
        return await _refusal_page(request, refusal, group_slug, section)
    return await _store_page(
        request, group_slug, notice="\n".join([SAVED, *follow_up]), notice_section=section
    )


def _flag(field: str) -> Apply:
    async def apply(s: AsyncSession, cat: StoreCategory, _form: Any, _more: list[str]) -> Any:
        return await toggle_category_flag(s, int(cat.id), field)

    return apply


async def _toggle_flat(s: AsyncSession, cat: StoreCategory, _f: Any, _m: list[str]) -> Any:
    return await toggle_flat_plan_list(s, int(cat.id))


async def _toggle_tiers(s: AsyncSession, cat: StoreCategory, _f: Any, _m: list[str]) -> Any:
    return await toggle_tiers(s, int(cat.id))


# URL part -> (control, section, write). «new-sale» and «visible» are not here on purpose.
TOGGLES: dict[str, tuple[str, str, Apply]] = {
    "renew": (SALE_RENEW, SECTION_SALE, _flag(FLAG_RENEW)),
    "flat": (SALE_FLAT, SECTION_SALE, _toggle_flat),
    "tiers": (SALE_TIERS, SECTION_SALE, _toggle_tiers),
    "custom": (SALE_CUSTOM, SECTION_SALE, _flag(FLAG_CUSTOM)),
    "reseller-trial": (TRIAL_RESELLER, SECTION_TRIAL, _flag(FLAG_RESELLER_TRIAL)),
    "kind": (GROUP_KIND, SECTION_GROUP, _flag(FLAG_KIND)),
}


async def settings_toggle(request: web.Request) -> web.Response:
    entry = TOGGLES.get(request.match_info["setting"])
    if entry is None:
        return _refused(request, 404, "این تنظیم در پنل نیست.", request.match_info["group_slug"])
    control, section, apply = entry
    return await _write(request, section=section, control=control, apply=apply)


async def settings_title(request: web.Request) -> web.Response:
    async def apply(s: AsyncSession, cat: StoreCategory, form: Any, _m: list[str]) -> Any:
        return await rename_category(s, int(cat.id), str(form.get("title", "")))

    return await _write(request, section=SECTION_GROUP, control=GROUP_TITLE, apply=apply)


async def settings_sort(request: web.Request) -> web.Response:
    async def apply(s: AsyncSession, cat: StoreCategory, form: Any, _m: list[str]) -> Any:
        return await save_sort_order(s, int(cat.id), str(form.get("sort", "")))

    return await _write(request, section=SECTION_GROUP, control=GROUP_SORT, apply=apply)


async def settings_trial(request: web.Request) -> web.Response:
    async def apply(s: AsyncSession, cat: StoreCategory, form: Any, _m: list[str]) -> Any:
        gb, days = require_trial(str(form.get("gb", "")), str(form.get("days", "")))
        return await save_category_trial(s, int(cat.id), gb=gb, days=days)

    return await _write(request, section=SECTION_TRIAL, control=TRIAL_SIZE, apply=apply)


async def settings_hwid(request: web.Request) -> web.Response:
    async def apply(s: AsyncSession, cat: StoreCategory, form: Any, _m: list[str]) -> Any:
        limit = require_number(str(form.get("hwid", "")))
        return await save_hwid_limit(s, int(cat.id), hwid_limit=limit)

    return await _write(request, section=SECTION_SALE, control=SALE_CUSTOM_HWID, apply=apply)


def _ids_text(form: Any) -> str:
    """The ids of a panel-group form: the typed box (``11, 12``) or one checkbox per group.

    Both send the field ``ids``; the checkboxes send it once each, joined here with a
    comma so ``parse_panel_group_ids`` is the one parser and refuses what it always did.
    """
    return ",".join(str(v) for v in form.getall("ids", []))


async def settings_panel_groups(request: web.Request) -> web.Response:
    """Saved only when no live service would be left on the old group.

    Moving existing services is a panel write and the bot offers it right after its
    own change. Until the owner decides how the page should offer it, a change that
    would strand services is refused here, with the count, and made from the bot.
    """

    async def apply(s: AsyncSession, cat: StoreCategory, form: Any, _m: list[str]) -> Any:
        ids = parse_panel_group_ids(_ids_text(form))
        cat_id = int(cat.id)
        left = await panel_groups_stranded(s, request.app[SETTINGS], cat_id, ids)
        if left:
            raise StoreAdminError(
                texts.admin_store_group_strand_refused.format(count=to_persian_digits(left))
            )
        await save_panel_groups(s, request.app[SETTINGS], cat_id, ids)

    return await _write(request, section=SECTION_PANEL, control=PANEL_GROUPS, apply=apply)


async def settings_premium_groups(request: web.Request) -> web.Response:
    async def apply(s: AsyncSession, cat: StoreCategory, form: Any, more: list[str]) -> Any:
        ids = parse_panel_group_ids(_ids_text(form))
        _cat, disabled = await save_premium_groups(s, int(cat.id), ids)
        if disabled:
            more.append(texts.admin_store_tier_disabled_no_groups)

    return await _write(request, section=SECTION_PANEL, control=PANEL_PREMIUM, apply=apply)


async def _panel_group_picker(
    request: web.Request, *, control: str, post_tail: str
) -> web.Response:
    """The panel's groups as checkboxes for one group's panel-group field. A read.

    The page itself never asks the panel: this is the one request that does, and only
    when the owner taps «نمایش گروه‌های پنل». It writes nothing. The form it draws posts to
    the same route the typed box does, with the same fields, so the server cannot tell
    the two apart. Panel not answering is one line and the typed box stays.
    """
    group_slug = request.match_info["group_slug"]
    settings = request.app[SETTINGS]
    if not is_store_owner(settings, request["telegram_id"]):
        return _refused(request, 403, texts.admin_store_owner_only, group_slug)
    async with request.app[SESSION_FACTORY]() as session:
        view = await get_catalog_view(session)
        current_version = await published_store_version(session)
        cat = find_group(view, group_slug)
        if cat is None:
            return render(
                request,
                "store_refused.html",
                status=404,
                message="گروه محصول یافت نشد.",
                reload_url=STORE_URL,
            )
        if control not in group_settings(view, cat).controls:
            return _refused(request, 409, TEXT_STALE, group_slug)
        selected = current_panel_group_ids(cat, control)
        # panel_group_choices lets go of the read snapshot before it awaits the panel
        choices = await panel_group_choices(session, settings)
    version = _form_version(request.query)
    return render(
        request,
        "store_panel_picker.html",
        unreachable=choices is None,
        unreachable_line=texts.web_store_pick_unreachable,
        rows=[] if choices is None else picker_rows(choices, selected),
        selected=selected,
        version=current_version if version is None else version,
        post_url=f"{STORE_URL}/g/{quote(group_slug)}/settings/{post_tail}",
    )


async def settings_panel_groups_pick(request: web.Request) -> web.Response:
    return await _panel_group_picker(request, control=PANEL_GROUPS, post_tail="panel-groups")


async def settings_premium_groups_pick(request: web.Request) -> web.Response:
    return await _panel_group_picker(request, control=PANEL_PREMIUM, post_tail="premium-groups")


async def settings_duration_add(request: web.Request) -> web.Response:
    async def apply(s: AsyncSession, cat: StoreCategory, form: Any, _m: list[str]) -> Any:
        if str(form.get("unlimited", "")) == "1":
            return await add_duration(s, int(cat.id), months=None)
        return await add_duration(
            s, int(cat.id), months=require_months(str(form.get("months", "")))
        )

    return await _write(request, section=SECTION_SALE, control=SALE_DURATIONS, apply=apply)


async def settings_duration_remove(request: web.Request) -> web.Response:
    """Two steps, as the bot's: the first only counts and asks, the second removes."""
    group_slug = request.match_info["group_slug"]
    valid, days = parse_duration_key(request.match_info["duration"])
    if not valid:
        return await _refusal_page(
            request, StoreAdminError("این مدت در لیست نیست."), group_slug, SECTION_SALE
        )
    form = await request.post()
    if str(form.get("confirm", "")) == "1":

        async def apply(s: AsyncSession, cat: StoreCategory, _f: Any, _m: list[str]) -> Any:
            return await remove_duration(s, int(cat.id), duration_days=days)

        return await _write(request, section=SECTION_SALE, control=SALE_DURATIONS, apply=apply)

    asked: dict[str, Any] = {}
    refusal: StoreAdminError | None = None

    async def preview(s: AsyncSession, cat: StoreCategory) -> None:
        asked["title"] = str(cat.title_fa)
        asked["count"] = await count_prices_for_duration(s, int(cat.id), days)
        await s.rollback()  # asking changes nothing: not even a draft is left behind

    async with request.app[SESSION_FACTORY]() as session:
        try:
            await edit_group_setting(
                session,
                request.app[SETTINGS],
                request["telegram_id"],
                expected_version=_form_version(form),
                group_slug=group_slug,
                control=SALE_DURATIONS,
                apply=preview,
            )
        except StoreAdminError as exc:
            refusal = exc
    if refusal is not None:
        return await _refusal_page(request, refusal, group_slug, SECTION_SALE)
    label = duration_label_fa(days)
    return render(
        request,
        "store_confirm.html",
        message=texts.admin_store_dur_del_confirm.format(
            label=label, title=asked["title"], count=asked["count"]
        ),
        version=_form_version(form),
        post_url=request.path,
        cancel_url=f"{STORE_URL}/g/{group_slug}",
    )


# --------------------------------------------------------------------------- #
# The plan editor and the customer's button
# --------------------------------------------------------------------------- #


async def _plan_page(
    request: web.Request,
    group_slug: str,
    plan_slug: str | None,
    *,
    notice: str | None = None,
    notice_is_error: bool = False,
    notice_section: str | None = None,
    status: int = 200,
) -> web.Response:
    """The editor of a saved plan (``plan_slug``) or of a new one in the group (``None``)."""
    owner = is_store_owner(request.app[SETTINGS], request["telegram_id"])
    async with request.app[SESSION_FACTORY]() as session:
        view = await get_catalog_view(session)
        version = await published_store_version(session)
    cat = find_group(view, group_slug)
    if cat is None:
        return render(
            request,
            "store_refused.html",
            status=404,
            message="گروه محصول یافت نشد.",
            reload_url=STORE_URL,
        )
    editor = build_plan_editor(view, cat, plan_slug)
    if editor is None:
        return _refused(request, 404, TEXT_PLAN_MISSING, group_slug)
    return render(
        request,
        "store_plan.html",
        status=status,
        editor=editor,
        version=version,
        can_edit=owner,
        store_url=STORE_URL,
        notice=notice,
        notice_is_error=notice_is_error,
        notice_section=notice_section,
    )


async def plan_editor(request: web.Request) -> web.Response:
    return await _plan_page(
        request, request.match_info["group_slug"], request.match_info["plan_slug"]
    )


async def plan_new(request: web.Request) -> web.Response:
    group_slug = request.match_info["group_slug"]
    if not is_store_owner(request.app[SETTINGS], request["telegram_id"]):
        return _refused(request, 403, texts.admin_store_owner_only, group_slug)
    async with request.app[SESSION_FACTORY]() as session:
        view = await get_catalog_view(session)
    cat = find_group(view, group_slug)
    if cat is not None and view.children_of(cat.id):
        # a group holds child groups or plans, never both: the same refusal as a stale form
        return _refused(request, 409, TEXT_STALE, group_slug)
    return await _plan_page(request, group_slug, None)


async def plan_preview(request: web.Request) -> web.Response:
    """The customer's button for the typed values. A read: it writes nothing and makes no draft."""
    group_slug = request.match_info["group_slug"]
    async with request.app[SESSION_FACTORY]() as session:
        view = await get_catalog_view(session)
    cat = find_group(view, group_slug)
    if cat is None:
        return render(
            request,
            "store_refused.html",
            status=404,
            message="گروه محصول یافت نشد.",
            reload_url=STORE_URL,
        )
    preview = preview_plan(view, cat, request.query.get("plan") or None, request.query)
    return render(request, "store_plan_preview.html", preview=preview)


async def _editor_result(
    request: web.Request,
    group_slug: str,
    plan_slug: str,
    section: str,
    refusal: StoreAdminError | None,
    *,
    notice: str | None = None,
) -> web.Response:
    """After a plan write from the editor: the editor again, saved line or the refusal in its section."""
    if refusal is None:
        return await _plan_page(
            request, group_slug, plan_slug, notice=notice or SAVED, notice_section=section
        )
    if isinstance(refusal, StoreOwnerRequired | StaleStoreVersion):
        return await _refusal_page(request, refusal, group_slug, section)
    return await _plan_page(
        request,
        group_slug,
        plan_slug,
        notice=strip_html_tags(str(refusal)) or texts.admin_store_invalid_input,
        notice_is_error=True,
        notice_section=section,
        status=400,
    )


PlanApply = Callable[[AsyncSession, StorePackage, Any], Awaitable[Any]]


async def _plan_write(
    request: web.Request, *, section: str, control: str, apply: PlanApply
) -> web.Response:
    """One write to one plan through ``edit_plan`` (owner, version, group and plan by slug)."""
    group_slug = request.match_info["group_slug"]
    plan_slug = request.match_info["plan_slug"]
    form = await request.post()
    refusal: StoreAdminError | None = None
    async with request.app[SESSION_FACTORY]() as session:
        try:
            await edit_plan(
                session,
                request.app[SETTINGS],
                request["telegram_id"],
                expected_version=_form_version(form),
                group_slug=group_slug,
                plan_slug=plan_slug,
                control=control,
                apply=lambda s, pkg: apply(s, pkg, form),
            )
        except StoreAdminError as exc:
            refusal = exc
    if str(form.get("origin", "")) == "editor":
        return await _editor_result(request, group_slug, plan_slug, section, refusal)
    if refusal is not None:
        return await _refusal_page(request, refusal, group_slug, None)
    return await _store_page(request, group_slug, notice=SAVED)


# URL part -> the plan switch it flips
PLAN_TOGGLES = {
    "enabled": "enabled",
    "new-sale": "allow_new_sale",
    "renew": "allow_renew",
    "extra-discount": "forbid_extra_discount",
}


async def plan_toggle(request: web.Request) -> web.Response:
    field = PLAN_TOGGLES.get(request.match_info["setting"])
    if field is None or field not in PLAN_FLAG_FIELDS:
        return _refused(request, 404, "این تنظیم در پنل نیست.", request.match_info["group_slug"])

    async def apply(s: AsyncSession, pkg: StorePackage, _form: Any) -> Any:
        return await toggle_package_flag(s, int(pkg.id), field)

    return await _plan_write(request, section="flags", control=PLAN_FLAGS, apply=apply)


async def plan_title(request: web.Request) -> web.Response:
    async def apply(s: AsyncSession, pkg: StorePackage, form: Any) -> Any:
        return await set_package_title(s, int(pkg.id), str(form.get("title", "")))

    return await _plan_write(request, section="title", control=PLAN_TITLE, apply=apply)


async def plan_dims(request: web.Request) -> web.Response:
    async def apply(s: AsyncSession, pkg: StorePackage, form: Any) -> Any:
        return await set_package_dims_from_form(s, pkg, form)

    return await _plan_write(request, section="dims", control=PLAN_DIMS, apply=apply)


async def plan_hwid(request: web.Request) -> web.Response:
    async def apply(s: AsyncSession, pkg: StorePackage, form: Any) -> Any:
        limit = read_hwid(str(form.get("hwid", "")))
        return await save_package_hwid(s, int(pkg.id), hwid_limit=limit)

    return await _plan_write(request, section="hwid", control=PLAN_HWID, apply=apply)


async def plan_delete(request: web.Request) -> web.Response:
    """Two steps, as the bot's: the first asks (and writes nothing), the second deletes."""
    group_slug = request.match_info["group_slug"]
    plan_slug = request.match_info["plan_slug"]
    form = await request.post()
    if str(form.get("confirm", "")) == "1":
        deleted: dict[str, str] = {}
        refusal: StoreAdminError | None = None

        async def do(s: AsyncSession, pkg: StorePackage, _f: Any) -> Any:
            deleted["title"], _cat = await remove_package(s, int(pkg.id))

        async with request.app[SESSION_FACTORY]() as session:
            try:
                await edit_plan(
                    session,
                    request.app[SETTINGS],
                    request["telegram_id"],
                    expected_version=_form_version(form),
                    group_slug=group_slug,
                    plan_slug=plan_slug,
                    control=PLAN_DELETE,
                    apply=lambda s, pkg: do(s, pkg, form),
                )
            except StoreAdminError as exc:
                refusal = exc
        if refusal is not None:
            return await _refusal_page(request, refusal, group_slug, None)
        return await _store_page(
            request,
            group_slug,
            notice=texts.admin_store_pkg_deleted.format(title=deleted["title"]),
        )

    asked: dict[str, str] = {}
    refusal = None

    async def preview(s: AsyncSession, pkg: StorePackage) -> None:
        view = await get_catalog_view(s)
        asked["text"] = package_delete_confirm(pkg, list(view.prices))
        await s.rollback()  # asking changes nothing: not even a draft is left behind

    async with request.app[SESSION_FACTORY]() as session:
        try:
            await edit_plan(
                session,
                request.app[SETTINGS],
                request["telegram_id"],
                expected_version=_form_version(form),
                group_slug=group_slug,
                plan_slug=plan_slug,
                control=PLAN_DELETE,
                apply=preview,
            )
        except StoreAdminError as exc:
            refusal = exc
    if refusal is not None:
        return await _refusal_page(request, refusal, group_slug, None)
    return render(
        request,
        "store_confirm.html",
        message=asked["text"],
        version=_form_version(form),
        post_url=request.path,
        cancel_url=f"{STORE_URL}/g/{group_slug}/plans/{plan_slug}",
    )


async def plan_create(request: web.Request) -> web.Response:
    """«ساخت پلن»: every box read by the bot's readers, then the bot's create call."""
    group_slug = request.match_info["group_slug"]
    form = await request.post()
    created: dict[str, str] = {}
    refusal: StoreAdminError | None = None

    async def apply(s: AsyncSession, cat: StoreCategory) -> Any:
        pkg = await create_package_from_form(s, cat, form)
        created["title"] = str(pkg.title_fa)

    async with request.app[SESSION_FACTORY]() as session:
        try:
            await edit_group_setting(
                session,
                request.app[SETTINGS],
                request["telegram_id"],
                expected_version=_form_version(form),
                group_slug=group_slug,
                control=PLAN_ADD,
                apply=apply,
            )
        except StoreAdminError as exc:
            refusal = exc
    if refusal is None:
        return await _store_page(request, group_slug, notice=texts.admin_store_plan_live)
    if isinstance(refusal, StoreOwnerRequired | StaleStoreVersion):
        return await _refusal_page(request, refusal, group_slug, None)
    return await _plan_page(
        request,
        group_slug,
        None,
        notice=strip_html_tags(str(refusal)) or texts.admin_store_invalid_input,
        notice_is_error=True,
        notice_section="new",
        status=400,
    )


# ---- plan order ------------------------------------------------------------


async def settings_order(request: web.Request) -> web.Response:
    """«ترتیب: خودکار ↔ دستی»: the mode switch."""

    async def apply(s: AsyncSession, cat: StoreCategory, _f: Any, _m: list[str]) -> Any:
        return await toggle_plan_order(s, int(cat.id))

    return await _write(request, section=SECTION_ORDER, control=PLAN_ORDER, apply=apply)


async def plan_move(request: web.Request) -> web.Response:
    """⬆️ / ⬇️: one step. The first move on an automatic group freezes it to manual."""
    direction = request.match_info["direction"]
    if direction not in ("up", "down"):
        return _refused(request, 404, "این تنظیم در پنل نیست.", request.match_info["group_slug"])
    plan_slug = request.match_info["plan_slug"]

    async def apply(s: AsyncSession, cat: StoreCategory, _f: Any, more: list[str]) -> Any:
        draft = await get_or_create_draft(s)
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
        moved = await move_package(s, int(pkg.id), direction=direction)
        if moved.status == "at_edge" and not moved.flipped:
            raise StoreAdminError(texts.admin_store_order_at_edge)  # nothing to save
        if moved.flipped:
            more.append(texts.admin_store_order_first_manual)

    return await _write(request, section=SECTION_ORDER, control=PLAN_ORDER, apply=apply)


# ---- groups ----------------------------------------------------------------


async def settings_subgroup(request: web.Request) -> web.Response:
    async def apply(s: AsyncSession, cat: StoreCategory, form: Any, more: list[str]) -> Any:
        child, parent = await create_subgroup(s, int(cat.id), str(form.get("title", "")))
        more.append(
            texts.admin_store_sub_cat_created.format(title=child.title_fa, parent=parent.title_fa)
        )

    return await _write(request, section=SECTION_GROUP, control=GROUP_SUBGROUP, apply=apply)


async def settings_delete_group(request: web.Request) -> web.Response:
    """Two steps, as the bot's: the first states what goes with the group, the second deletes."""
    group_slug = request.match_info["group_slug"]
    form = await request.post()
    done: dict[str, str] = {}
    asked: dict[str, str] = {}
    refusal: StoreAdminError | None = None
    confirmed = str(form.get("confirm", "")) == "1"

    async def apply(s: AsyncSession, cat: StoreCategory) -> None:
        if confirmed:
            done["title"] = await remove_group(s, int(cat.id))
            return
        asked["text"] = group_delete_confirm(await get_catalog_view(s), cat)
        await s.rollback()  # asking changes nothing: not even a draft is left behind

    async with request.app[SESSION_FACTORY]() as session:
        try:
            await edit_group_setting(
                session,
                request.app[SETTINGS],
                request["telegram_id"],
                expected_version=_form_version(form),
                group_slug=group_slug,
                control=GROUP_DELETE,
                apply=apply,
            )
        except StoreAdminError as exc:
            refusal = exc
    if refusal is not None:
        return await _refusal_page(request, refusal, group_slug, SECTION_GROUP)
    if confirmed:
        return await _store_page(
            request, None, notice=texts.admin_store_cat_deleted.format(title=done["title"])
        )
    return render(
        request,
        "store_confirm.html",
        message=asked["text"],
        version=_form_version(form),
        post_url=request.path,
        cancel_url=f"{STORE_URL}/g/{group_slug}",
    )


async def group_create(request: web.Request) -> web.Response:
    """«➕ گروه جدید»: a top-level group, its title and its kind (حجمی or نامحدود)."""
    form = await request.post()
    created: dict[str, str] = {}
    refusal: StoreAdminError | None = None

    async def action(s: AsyncSession) -> None:
        cat = await create_group(s, str(form.get("title", "")), str(form.get("kind", "")))
        created["slug"], created["title"] = str(cat.slug), str(cat.title_fa)

    async with request.app[SESSION_FACTORY]() as session:
        try:
            await guarded_store_write(
                session,
                request.app[SETTINGS],
                request["telegram_id"],
                expected_version=_form_version(form),
                action=action,
            )
        except StoreAdminError as exc:
            refusal = exc
    if refusal is None:
        return await _store_page(
            request,
            created["slug"],
            notice=texts.admin_store_cat_created.format(title=created["title"]),
        )
    if isinstance(refusal, StoreOwnerRequired):
        return render(
            request,
            "store_refused.html",
            status=403,
            message=texts.admin_store_owner_only,
            reload_url=STORE_URL,
        )
    if isinstance(refusal, StaleStoreVersion):
        return render(
            request, "store_refused.html", status=409, message=str(refusal), reload_url=STORE_URL
        )
    return await _store_page(
        request,
        None,
        notice=str(refusal) or texts.admin_store_invalid_input,
        notice_is_error=True,
        status=400,
    )


# --------------------------------------------------------------------------- #
# «↩️ تغییرات»: the change log and its undo
# --------------------------------------------------------------------------- #


async def _changes_page(
    request: web.Request,
    *,
    notice: str | None = None,
    notice_is_error: bool = False,
    status: int = 200,
) -> web.Response:
    """The bot's «تغییرات فروشگاه»: the same entries, order and notes (any admin may read)."""
    owner = is_store_owner(request.app[SETTINGS], request["telegram_id"])
    async with request.app[SESSION_FACTORY]() as session:
        entries = await list_store_changes(session)
        version = await published_store_version(session)
    return render(
        request,
        "store_changes.html",
        status=status,
        rows=change_rows(entries),
        empty=texts.admin_store_changes_empty,
        hint=texts.admin_store_changes_hint,
        read_only=texts.admin_store_hub_readonly,
        can_undo=owner,
        version=version,
        notice=notice,
        notice_is_error=notice_is_error,
        store_url=STORE_URL,
        changes_url=CHANGES_URL,
    )


async def store_changes(request: web.Request) -> web.Response:
    return await _changes_page(request)


def _changes_refused(request: web.Request, status: int, message: str) -> web.Response:
    return render(
        request, "store_refused.html", status=status, message=message, reload_url=CHANGES_URL
    )


async def store_change_undo(request: web.Request) -> web.Response:
    """Two steps, as the bot's: the first states what goes back (and how many later
    changes with it) and writes nothing, the second (``confirm=1``) undoes.

    The entry is the version row the log lists (``version_id``): it keeps its id when
    later edits land, and an entry that is gone is refused as stale.
    """
    version_id = int(request.match_info["version_id"])
    form = await request.post()
    confirmed = str(form.get("confirm", "")) == "1"
    settings, tg_id, expected = request.app[SETTINGS], request["telegram_id"], _form_version(form)
    refusal: StoreAdminError | None = None
    description = ""
    asked = ""
    async with request.app[SESSION_FACTORY]() as session:
        try:
            if confirmed:
                description = await confirm_undo(
                    session, settings, tg_id, expected_version=expected, version_id=version_id
                )
            else:
                entry = await ask_undo(
                    session, settings, tg_id, expected_version=expected, version_id=version_id
                )
                asked = undo_confirm_text(entry)
        except StoreAdminError as exc:
            refusal = exc
    if isinstance(refusal, StoreOwnerRequired):
        return _changes_refused(request, 403, texts.admin_store_owner_only)
    if isinstance(refusal, StaleStoreVersion):
        return _changes_refused(request, 409, str(refusal))
    if refusal is not None:
        return await _changes_page(
            request,
            notice=strip_html_tags(str(refusal)) or texts.admin_store_invalid_input,
            notice_is_error=True,
            status=400,
        )
    if confirmed:
        return await _changes_page(
            request, notice=f"{texts.admin_store_undone}\n{undone_text(description)}"
        )
    return render(
        request,
        "store_confirm.html",
        message=asked,
        version=expected,
        post_url=request.path,
        confirm_label=texts.admin_store_undo_button,
        confirm_class="primary",
        cancel_url=CHANGES_URL,
    )


routes = [
    web.get(STORE_URL, store_home),
    web.get(CHANGES_URL, store_changes),
    web.post(f"{CHANGES_URL}/{{version_id:{NUMERIC_ID_PATTERN}}}/undo", store_change_undo),
    web.get(f"{STORE_URL}/g/{{group_slug}}", store_group),
    web.post(f"{STORE_URL}/g/{{group_slug}}/plans/{{plan_slug}}/price", store_edit_price),
    web.post(f"{SETTINGS_URL}/toggle/{{setting}}", settings_toggle),
    web.post(f"{SETTINGS_URL}/title", settings_title),
    web.post(f"{SETTINGS_URL}/sort", settings_sort),
    web.post(f"{SETTINGS_URL}/trial", settings_trial),
    web.post(f"{SETTINGS_URL}/hwid", settings_hwid),
    web.post(f"{SETTINGS_URL}/panel-groups", settings_panel_groups),
    web.post(f"{SETTINGS_URL}/premium-groups", settings_premium_groups),
    web.get(f"{SETTINGS_URL}/panel-groups/pick", settings_panel_groups_pick),
    web.get(f"{SETTINGS_URL}/premium-groups/pick", settings_premium_groups_pick),
    web.post(f"{SETTINGS_URL}/durations/add", settings_duration_add),
    web.post(f"{SETTINGS_URL}/durations/{{duration}}/remove", settings_duration_remove),
    web.post(f"{SETTINGS_URL}/order", settings_order),
    web.post(f"{SETTINGS_URL}/subgroups", settings_subgroup),
    web.post(f"{SETTINGS_URL}/delete", settings_delete_group),
    web.post(f"{STORE_URL}/groups", group_create),
    web.get(f"{STORE_URL}/g/{{group_slug}}/new-plan", plan_new),
    web.get(f"{STORE_URL}/g/{{group_slug}}/plan-preview", plan_preview),
    web.post(f"{STORE_URL}/g/{{group_slug}}/plans", plan_create),
    web.get(f"{STORE_URL}/g/{{group_slug}}/plans/{{plan_slug}}", plan_editor),
    web.post(f"{STORE_URL}/g/{{group_slug}}/plans/{{plan_slug}}/toggle/{{setting}}", plan_toggle),
    web.post(f"{STORE_URL}/g/{{group_slug}}/plans/{{plan_slug}}/title", plan_title),
    web.post(f"{STORE_URL}/g/{{group_slug}}/plans/{{plan_slug}}/dims", plan_dims),
    web.post(f"{STORE_URL}/g/{{group_slug}}/plans/{{plan_slug}}/hwid", plan_hwid),
    web.post(f"{STORE_URL}/g/{{group_slug}}/plans/{{plan_slug}}/delete", plan_delete),
    web.post(f"{STORE_URL}/g/{{group_slug}}/plans/{{plan_slug}}/move/{{direction}}", plan_move),
]

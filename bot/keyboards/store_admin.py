"""Inline keyboards for STORE_ADMIN / STORE_STRATEGY (مدیریت فروشگاه)."""

from __future__ import annotations

from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.keyboards.admin import AdminCB
from bot.keyboards.purchase import encode_duration_cb
from bot.keyboards.style import DANGER, PRIMARY, SUCCESS, styled
from bot.texts import texts
from services.panel_group_list import picker_rows
from services.store_admin import duration_label_fa
from services.store_catalog import (
    CUSTOM_PURCHASE_EVERYONE,
    CUSTOM_PURCHASE_OFF,
    CUSTOM_PURCHASE_RESELLERS,
    category_reseller_trial,
    category_trial_shape,
    category_volume_hwid_limit,
    compact_toman_fa,
    custom_purchase_mode,
    flat_plan_dim_label,
    hwid_limit_label_fa,
)
from services.store_group_settings import (
    GROUP_KIND,
    GROUP_SUBGROUP,
    PANEL_PREMIUM,
    PLAN_ADD,
    PLAN_ORDER,
    SALE_CUSTOM,
    SALE_CUSTOM_HWID,
    SALE_DURATIONS,
    SALE_TIERS,
    TRIAL_RESELLER,
    kind_labels,
    visible_controls,
)
from services.store_plan_edit import (
    PLAN_DIMS,
    PLAN_HWID,
    PLAN_PRICE,
    visible_plan_controls,
)
from services.store_pricing import category_durations
from services.ux_format import to_persian_digits


class StoreCB:
    """Callback prefixes — keep under Telegram 64-byte limit."""

    HUB = AdminCB.STORE  # adm:store
    CATS = "adm:st:cats"  # retired: the hub lists the groups itself
    CAT = "adm:st:cat:"  # + id
    CAT_EN = "adm:st:cen:"
    CAT_NS = "adm:st:cns:"
    CAT_RN = "adm:st:crn:"
    CAT_TITLE = "adm:st:ct:"
    CAT_SORT = "adm:st:cs:"
    CAT_GROUPS = "adm:st:cg:"
    CAT_TL = "adm:st:ctl:"  # retired — was time_limit_filter_enabled
    CAT_FLAT = "adm:st:cfl:"  # + id — toggle flat_plan_list
    CAT_CUSTOM = "adm:st:ccu:"  # cycle custom_purchase_mode (گیگ/ماه builder)
    CAT_HWID = "adm:st:chw:"  # + id — edit volume_hwid_limit for custom volume
    CAT_KIND = "adm:st:ck:"  # cycle kind volume|unlimited
    CAT_ADD = "adm:st:cadd"  # create free-form group (prompt title)
    CAT_ADD_KIND = "adm:st:caddk:"  # + volume|unlimited after title
    CAT_SUB_ADD = "adm:st:csub:"  # + parent id — create a child group
    CAT_SET = "adm:st:cset:"  # retired: the dashboard replaced settings
    CAT_DEL = "adm:st:cdel:"  # preview only — + id
    CAT_DEL_OK = "adm:st:cdelok:"  # applies — + id
    CAT_DURS = "adm:st:cdur:"  # + category id — duration list
    CAT_DUR_ADD = "adm:st:cda:"  # + category id — prompt months
    CAT_DUR_NONE = "adm:st:cdn:"  # + category id — add بدون انقضا
    CAT_DUR_DEL = "adm:st:cdd:"  # + cat_id + ":" + encoded duration — confirm
    CAT_DUR_DEL_OK = "adm:st:cddok:"  # + cat_id + ":" + encoded — apply
    CAT_TIERS = "adm:st:ctr:"  # + category id — toggle tiers_enabled
    CAT_TIER_GROUPS = "adm:st:ctp:"  # + category id — premium panel groups
    CAT_TRIAL = "adm:st:cfr:"  # + id — this group's free trial (GB and days)
    CAT_GROUP_MOVE = "adm:st:cgm:"  # + id + ":" + old ids joined by "-" — move from old to new
    CAT_GROUP_KEEP = "adm:st:cgn:"  # + id — leave them, only new services use it
    GROUP_PICK = "adm:st:gpk:"  # + cat id + ":" + panel group id — toggle in the picker
    GROUP_PICK_SAVE = "adm:st:gps:"  # + cat id — save the picker's selection
    CAT_RTRIAL = "adm:st:crs:"  # + id — toggle: resellers may give this trial away
    # The four sections of a group's dashboard. «sx» because "adm:st:st…" is the
    # retired purchase-steps prefix and would be swallowed by its handler.
    SEC_SALE = "adm:st:sxs:"  # + id — 🛒 فروش
    SEC_TRIAL = "adm:st:sxt:"  # + id — 🎟 تست
    SEC_PANEL = "adm:st:sxp:"  # + id — 🖥 پنل
    SEC_GROUP = "adm:st:sxg:"  # + id — ✏️ گروه
    SEC_ORDER = "adm:st:sxo:"  # + id — ↕️ چیدمان پلن‌ها
    READY = "adm:st:ready"  # retired: «همهٔ پلن‌ها»
    CUSTOM = "adm:st:cust"  # retired: global «خرید سفارشی» list
    PKGS_PICK = "adm:st:pkp"  # retired: group picker
    PKGS = "adm:st:pkgs:"  # retired: plans are on the group dashboard
    PKG = "adm:st:pkg:"
    PKG_EN = "adm:st:pen:"
    PKG_NS = "adm:st:pns:"
    PKG_RN = "adm:st:prn:"
    PKG_FD = "adm:st:pfd:"
    PKG_TITLE = "adm:st:pt:"
    PKG_PRICE = "adm:st:pp:"
    PKG_PRICE_OK = "adm:st:ppok"  # confirm a price 10x / a tenth of the current one
    PKG_TL = "adm:st:ptl:"  # toggle has_time_limit on ready plan
    PKG_ADD = "adm:st:padd:"  # + category id — create a priced plan
    # The pending plan lives in FSM state until it is confirmed, so none of
    # these carry an id — there is no row to point at yet.
    PKG_ACTIVATE = "adm:st:pkgok"  # write it and make it live
    PKG_DISCARD = "adm:st:pkgno"  # drop it, nothing was written
    PKG_KEEP_TITLE = "adm:st:pkeep"  # rename before confirming
    PKG_DIMS = "adm:st:pdim:"  # + id — edit حجم/مدت of a ready plan
    PKG_HWID = "adm:st:phw:"  # + id — edit کاربر همزمان of a ready volume plan
    PKG_USERS = "adm:st:pus:"  # + value (0 = نامحدود)
    PKG_USERS_OTHER = "adm:st:puso"
    # Building a plan by tapping, not by typing a format.
    PKG_SIZE = "adm:st:psz:"  # + value
    PKG_SIZE_OTHER = "adm:st:pszo"
    PKG_DUR = "adm:st:pdu:"  # + value, or 0 for «بدون انقضا»
    PKG_DUR_OTHER = "adm:st:pduo"
    PKG_DEL = "adm:st:pdel:"  # preview only — + id
    PKG_DEL_OK = "adm:st:pdelok:"  # applies — + id
    PKG_ORDER = "adm:st:pord:"  # toggle plan_order_mode + category id (on SEC_ORDER)
    PKG_UP = "adm:st:pup:"  # move plan up + package id
    PKG_DN = "adm:st:pdn:"  # move plan down + package id
    PREVIEW = "adm:st:prev"
    SEED = "adm:st:seed"
    # STORE_STRATEGY
    STRAT = "adm:st:strat"
    DISC = "adm:st:disc"
    DISC_BEST = "adm:st:dbest"  # set best_of
    DISC_SUM = "adm:st:dsum"  # set sum (legacy)
    # RENEW_PRICE / RENEW_CF were removed. Checkout never read them, and
    # customer confirmation is not optional.
    PROMO = "adm:st:prm"
    PROMO_ADD = "adm:st:prma"
    PROMO_DEL = "adm:st:prmd:"  # preview only — + code
    PROMO_DEL_OK = "adm:st:prmdok:"  # applies — + code
    PROMO_TG = "adm:st:prmt:"  # toggle active + code
    PROMO_Q = "adm:st:prmq:"  # a wizard button + y | n | s (yes, no, skip)
    PAY = "adm:st:pay"  # the payment card screen
    PAY_EDIT = "adm:st:pye:"  # + field name — prompt for one value
    PAY_CARD_OK = "adm:st:pyc"  # save the card number held in FSM state
    PAY_CLEAR = "adm:st:pyr"  # preview: back to .env
    PAY_CLEAR_OK = "adm:st:pyrk"  # applies it
    STEPS = "adm:st:steps"
    STEP_UP = "adm:st:stu:"  # + index
    STEP_DN = "adm:st:std:"
    STEP_TG = "adm:st:stt:"
    STEP_RM = "adm:st:str:"
    STEP_RM_OK = "adm:st:strmok:"  # applies the step removal
    STEP_ADD = "adm:st:sta"
    STEP_ADDB = "adm:st:stab:"  # + block id
    HIST = "adm:st:hist"
    ADV = "adm:st:adv"  # retired: «پیشرفته» is gone
    HIST_REST = "adm:st:hr:"  # + version id
    HIST_OK = "adm:st:hrok:"  # confirm restore


def _back_hub() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="⬅️ بازگشت به فروشگاه", callback_data=StoreCB.HUB)]


def _back_home() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="🏠 خانه ادمین", callback_data=AdminCB.HOME)]


def _toggle_label(label: str, on: bool) -> str:
    """«✅ فروش جدید» / «❌ فروش جدید»: the mark is the state now, a tap flips it.

    The owner asked for the marks back (2026-10-03): the words «روشن — خاموش
    کن» were harder to tell apart at a glance than ✅ and ❌.
    """
    return f"{'✅' if on else '❌'} {label}"


_CUSTOM_MODE_LABELS = {
    CUSTOM_PURCHASE_OFF: "❌ خرید سفارشی",
    CUSTOM_PURCHASE_RESELLERS: "🏪 خرید سفارشی: فقط نماینده‌ها",
    CUSTOM_PURCHASE_EVERYONE: "✅ خرید سفارشی: همه",
}


def _custom_mode_label(mode: str) -> str:
    """The three-state sibling of ``_toggle_label``: the mark and the words are the
    state now, a tap moves to the next one (off, resellers only, everyone)."""
    return _CUSTOM_MODE_LABELS[mode]


def store_hub_keyboard(
    *,
    is_owner: bool,
    has_categories: bool,
    groups: list[tuple[int, str]] | None = None,
) -> InlineKeyboardMarkup:
    """The shop home: top-level groups first, then the shop-wide tools.

    ``groups`` are ``(category id, label)`` with their state already in the
    label («⏸» off sale, «⚠️» no panel group). «پیشرفته», «همهٔ پلن‌ها» and
    «گروه‌های محصول» are gone: the groups are buttons here, and the tools that
    hid under پیشرفته sit one row below.

    Seeding is offered only on a shop with no groups at all — and then it is
    the one thing the screen exists for, so it takes the accent.
    """
    rows: list[list[InlineKeyboardButton]] = []
    seed = is_owner and not has_categories
    if seed:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🌱 بارگذاری اولیه از فایل قیمت",
                    callback_data=StoreCB.SEED,
                    style=styled(PRIMARY),
                )
            ]
        )
    row: list[InlineKeyboardButton] = []
    for cat_id, label in groups or []:
        row.append(
            InlineKeyboardButton(text=_clip(label, 30), callback_data=f"{StoreCB.CAT}{cat_id}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if is_owner:
        rows.append(
            [
                InlineKeyboardButton(
                    text="➕ گروه جدید",
                    callback_data=StoreCB.CAT_ADD,
                    style=None if seed else styled(PRIMARY),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="🏷 تخفیف‌ها", callback_data=StoreCB.DISC),
            InlineKeyboardButton(text="💳 پرداخت", callback_data=StoreCB.PAY),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text="👁 پیش‌نمایش مشتری", callback_data=StoreCB.PREVIEW),
            InlineKeyboardButton(text="↩️ تغییرات", callback_data=StoreCB.HIST),
        ]
    )
    rows.append(_back_home())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _clip(label: str, limit: int) -> str:
    return label if len(label) <= limit else label[: limit - 1] + "…"


def store_discount_keyboard(
    *,
    is_owner: bool,
    discount_combine: str,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if is_owner:
        best_mark = "✅" if discount_combine == "best_of_promo_vs_extra" else "▫️"
        sum_mark = "✅" if discount_combine == "sum" else "▫️"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{best_mark} بهترین تخفیف",
                    callback_data=StoreCB.DISC_BEST,
                ),
                InlineKeyboardButton(
                    text=f"{sum_mark} جمع تخفیف‌ها",
                    callback_data=StoreCB.DISC_SUM,
                ),
            ]
        )
        rows.append(
            [InlineKeyboardButton(text="🏷 مدیریت کدهای تخفیف", callback_data=StoreCB.PROMO)]
        )
    else:
        rows.append(
            [InlineKeyboardButton(text="🏷 کدهای تخفیف (مشاهده)", callback_data=StoreCB.PROMO)]
        )
    rows.append(_back_hub())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _percent_label(value: Any) -> str:
    """«15», not «15.0»: the percent is stored as a float. A real fraction stays."""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def store_promo_keyboard(promos: list[dict[str, Any]], *, is_owner: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for p in promos:
        code = str(p["code"])
        mark = "✅" if p.get("active", True) else "❌"
        label = f"{mark} {code} ({_percent_label(p.get('percent'))}%)"
        if len(label) > 40:
            label = label[:37] + "…"
        if is_owner:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=label,
                        callback_data=f"{StoreCB.PROMO_TG}{code}",
                    ),
                    InlineKeyboardButton(
                        text="🗑",
                        callback_data=f"{StoreCB.PROMO_DEL}{code}",
                    ),
                ]
            )
        else:
            rows.append([InlineKeyboardButton(text=label, callback_data=StoreCB.PROMO)])
    if is_owner:
        rows.append(
            [InlineKeyboardButton(text="➕ افزودن کد تخفیف", callback_data=StoreCB.PROMO_ADD)]
        )
    rows.append([InlineKeyboardButton(text="⬅️ تخفیف‌ها", callback_data=StoreCB.DISC)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def store_promo_first_keyboard() -> InlineKeyboardMarkup:
    """Wizard step: only for the first purchase? Two answers, a skip, a way out."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.admin_promo_yes, callback_data=f"{StoreCB.PROMO_Q}y"
                ),
                InlineKeyboardButton(
                    text=texts.admin_promo_no, callback_data=f"{StoreCB.PROMO_Q}n"
                ),
            ],
            [
                InlineKeyboardButton(
                    text=texts.admin_promo_skip, callback_data=f"{StoreCB.PROMO_Q}s"
                )
            ],
            [InlineKeyboardButton(text="لغو", callback_data=StoreCB.HUB)],
        ]
    )


def store_promo_skip_keyboard() -> InlineKeyboardMarkup:
    """Wizard step that wants a number: «رد کردن» keeps the unlimited default."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.admin_promo_skip, callback_data=f"{StoreCB.PROMO_Q}s"
                )
            ],
            [InlineKeyboardButton(text="لغو", callback_data=StoreCB.HUB)],
        ]
    )


def store_changes_keyboard(entries: list[Any], *, is_owner: bool) -> InlineKeyboardMarkup:
    """The change log. Each row is one edit; tapping it offers to undo.

    It used to read «v12 — بایگانی», which told the owner nothing about what
    they had actually done. The row now carries the change itself, and the
    version number is gone from the label entirely.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for entry in entries:
        label = entry.description
        if entry.is_current:
            label = f"● {label}"
        if len(label) > 55:
            label = label[:52] + "…"
        if is_owner and entry.can_undo:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"↩️ {label}",
                        callback_data=f"{StoreCB.HIST_REST}{entry.version_id}",
                    )
                ]
            )
        else:
            rows.append([InlineKeyboardButton(text=label, callback_data=StoreCB.HIST)])
    rows.append(_back_hub())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def store_restore_confirm_keyboard(version_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.admin_store_undo_button,
                    callback_data=f"{StoreCB.HIST_OK}{version_id}",
                ),
                InlineKeyboardButton(text="لغو", callback_data=StoreCB.HIST),
            ]
        ]
    )


def _dash_back(cat_id: int, label: str = "⬅️ گروه") -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=label, callback_data=f"{StoreCB.CAT}{cat_id}")]


def plan_button_label(pkg: Any, price_toman: int | None) -> str:
    """One plan on the group dashboard: dims, price, and «⏸» when off sale.

    «۱۰۰ گیگ – ۳ ماهه | ۴۳۰ هزار». Built from ``options`` (never ``title_fa``),
    en dash and «هزار», no «تومان». A custom template has no dimensions, so it
    keeps its title behind «🧩». At most 40 characters: the owner checks a
    button uncut on a phone.
    """
    off = (not pkg.enabled) or (not pkg.allow_new_sale)
    dims = flat_plan_dim_label(pkg)
    if dims is None:
        label = f"🧩 {pkg.title_fa}"
    else:
        price = compact_toman_fa(int(price_toman)) if price_toman else "بدون قیمت"
        shelf_dims = dims.replace(" • ", " \u2013 ", 1)
        label = f"{shelf_dims} | {price}"
    return _clip(f"⏸ {label}" if off else label, 40)


def store_category_dashboard_keyboard(
    cat: Any,
    *,
    is_owner: bool,
    children: list[Any] | None = None,
    plan_buttons: list[tuple[int, str]] | None = None,
) -> InlineKeyboardMarkup:
    """The group screen: its plans (or child groups) are the buttons.

    A group holds child groups or plans, never both, so the customer screen
    never mixes them; ``➕ پلن جدید`` / ``➕ زیرگروه جدید`` follow the same rule.
    The four sections carry every setting, each in exactly one place.
    """
    rows: list[list[InlineKeyboardButton]] = []
    kids = list(children or [])
    plans = list(plan_buttons or [])
    if kids:
        row: list[InlineKeyboardButton] = []
        for child in kids:
            mark = "" if child.enabled else "⏸ "
            row.append(
                InlineKeyboardButton(
                    text=_clip(f"{mark}{child.title_fa}", 30),
                    callback_data=f"{StoreCB.CAT}{child.id}",
                )
            )
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
    else:
        for pkg_id, label in plans:
            rows.append([InlineKeyboardButton(text=label, callback_data=f"{StoreCB.PKG}{pkg_id}")])
    if is_owner:
        # Which of the group's own buttons show is `visible_controls`, the function the
        # Mini App's group page asks too; the bot builds the buttons, not the condition.
        shown = visible_controls(
            cat, has_plans=bool(plans), has_children=bool(kids), plan_count=len(plans)
        )
        if PLAN_ADD in shown:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="➕ پلن جدید",
                        callback_data=f"{StoreCB.PKG_ADD}{cat.id}",
                        style=styled(PRIMARY),
                    )
                ]
            )
        # One level of nesting only: a child group cannot have children.
        if GROUP_SUBGROUP in shown:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="➕ زیرگروه جدید",
                        callback_data=f"{StoreCB.CAT_SUB_ADD}{cat.id}",
                    )
                ]
            )
        if PLAN_ORDER in shown:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="↕️ چیدمان پلن‌ها",
                        callback_data=f"{StoreCB.SEC_ORDER}{cat.id}",
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(text="🛒 فروش", callback_data=f"{StoreCB.SEC_SALE}{cat.id}"),
                InlineKeyboardButton(text="🎟 تست", callback_data=f"{StoreCB.SEC_TRIAL}{cat.id}"),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(text="🖥 پنل", callback_data=f"{StoreCB.SEC_PANEL}{cat.id}"),
                InlineKeyboardButton(text="✏️ گروه", callback_data=f"{StoreCB.SEC_GROUP}{cat.id}"),
            ]
        )
    parent_id = getattr(cat, "parent_id", None)
    if parent_id:
        rows.append(_dash_back(int(parent_id), "⬅️ گروه والد"))
    else:
        rows.append(_back_hub())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def store_cat_sale_keyboard(cat: Any) -> InlineKeyboardMarkup:
    """🛒 فروش — what customers can do in this group and how the shelf looks.

    Under the flat list the duration tabs and the tier split do not exist, so
    their controls are hidden rather than left saving flags nothing reads. The
    custom device limit only means something while custom purchase is on, and
    only for a volume group.
    """
    flags = cat.sales_flags or {}
    cid = cat.id
    flat_on = bool(flags.get("flat_plan_list"))
    custom_mode = custom_purchase_mode(cat)
    controls = visible_controls(cat)
    rows: list[list[InlineKeyboardButton]] = []

    def toggle(label: str, on: bool, prefix: str) -> None:
        rows.append(
            [InlineKeyboardButton(text=_toggle_label(label, on), callback_data=f"{prefix}{cid}")]
        )

    toggle("نمایش به مشتری", bool(cat.enabled), StoreCB.CAT_EN)
    toggle("فروش جدید", bool(flags.get("allow_new_sale", True)), StoreCB.CAT_NS)
    toggle("تمدید", bool(flags.get("allow_renew", True)), StoreCB.CAT_RN)
    toggle("فهرست ساده", flat_on, StoreCB.CAT_FLAT)
    if SALE_DURATIONS in controls:
        rows.append(
            [InlineKeyboardButton(text="⏱ مدت‌ها", callback_data=f"{StoreCB.CAT_DURS}{cid}")]
        )
    if SALE_TIERS in controls:
        toggle("تفکیک ویژه/اقتصادی", bool(flags.get("tiers_enabled")), StoreCB.CAT_TIERS)
    if SALE_CUSTOM in controls:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_custom_mode_label(custom_mode),
                    callback_data=f"{StoreCB.CAT_CUSTOM}{cid}",
                )
            ]
        )
    if SALE_CUSTOM_HWID in controls:
        hwid_fa = hwid_limit_label_fa(category_volume_hwid_limit(cat))
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"👥 کاربر همزمان خرید سفارشی: {hwid_fa}",
                    callback_data=f"{StoreCB.CAT_HWID}{cid}",
                )
            ]
        )
    rows.append(_dash_back(cid))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def store_cat_trial_keyboard(cat: Any) -> InlineKeyboardMarkup:
    """🎟 تست — the trial's size, and the reseller switch only once it exists."""
    cid = cat.id
    trial = category_trial_shape(cat)
    trial_fa = (
        "خاموش"
        if trial is None
        else f"{to_persian_digits(trial[0])} گیگ • {to_persian_digits(trial[1])} روز"
    )
    rows = [
        [
            InlineKeyboardButton(
                text=f"🎟 تست رایگان: {trial_fa} — تغییر",
                callback_data=f"{StoreCB.CAT_TRIAL}{cid}",
            )
        ]
    ]
    # A reseller switch on a group with no trial would save a flag nothing reads.
    if TRIAL_RESELLER in visible_controls(cat):
        rows.append(
            [
                InlineKeyboardButton(
                    text=_toggle_label("تست نماینده", category_reseller_trial(cat)),
                    callback_data=f"{StoreCB.CAT_RTRIAL}{cid}",
                )
            ]
        )
    rows.append(_dash_back(cid))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def store_cat_panel_keyboard(cat: Any) -> InlineKeyboardMarkup:
    """🖥 پنل — the panel group; the premium one only while tiers exist."""
    cid = cat.id
    rows = [[InlineKeyboardButton(text="🔗 گروه پنل", callback_data=f"{StoreCB.CAT_GROUPS}{cid}")]]
    # Tiers are not offered (and not read) under the flat list.
    if PANEL_PREMIUM in visible_controls(cat):
        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.admin_store_tier_groups,
                    callback_data=f"{StoreCB.CAT_TIER_GROUPS}{cid}",
                )
            ]
        )
    rows.append(_dash_back(cid))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def store_panel_group_picker_keyboard(
    cat_id: int,
    groups: list[tuple[int, str, bool]],
    selected: list[int],
) -> InlineKeyboardMarkup:
    """The panel's groups as toggles; several may be picked.

    ``groups`` is ``(id, name, is_disabled)`` from the panel. A selected id the
    panel no longer lists still gets a row, so it can be un-picked rather than
    saved again unseen.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for gid, name, disabled in picker_rows(groups, selected):
        mark = "✅" if gid in selected else "▫️"
        off = " ⛔" if disabled else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {name} • #{gid}{off}",
                    callback_data=f"{StoreCB.GROUP_PICK}{cat_id}:{gid}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=f"💾 ذخیره ({len(selected)} گروه)",
                callback_data=f"{StoreCB.GROUP_PICK_SAVE}{cat_id}",
                style=styled(PRIMARY),
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="⬅️ پنل", callback_data=f"{StoreCB.SEC_PANEL}{cat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def store_cat_group_keyboard(
    cat: Any, *, has_plans: bool = False, has_children: bool = False
) -> InlineKeyboardMarkup:
    """✏️ گروه — name, position, kind, sub-groups and delete.

    The kind toggle needs an empty group: with plans or children it would
    relabel products already priced for the other kind.
    """
    cid = cat.id
    controls = visible_controls(cat, has_plans=has_plans, has_children=has_children)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="✏️ عنوان", callback_data=f"{StoreCB.CAT_TITLE}{cid}"),
            InlineKeyboardButton(text="🔢 ترتیب نمایش", callback_data=f"{StoreCB.CAT_SORT}{cid}"),
        ]
    ]
    if GROUP_KIND in controls:
        kind_fa, other = kind_labels(cat)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"نوع: {kind_fa} — تبدیل به {other}",
                    callback_data=f"{StoreCB.CAT_KIND}{cid}",
                )
            ]
        )
    if GROUP_SUBGROUP in controls:
        rows.append(
            [
                InlineKeyboardButton(
                    text="➕ زیرگروه جدید", callback_data=f"{StoreCB.CAT_SUB_ADD}{cid}"
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🗑 حذف این گروه",
                callback_data=f"{StoreCB.CAT_DEL}{cid}",
                style=styled(DANGER),
            )
        ]
    )
    rows.append(_dash_back(cid))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def store_plan_order_keyboard(
    cat: Any, plans: list[tuple[int, str]], *, mode_label: str, manual: bool
) -> InlineKeyboardMarkup:
    """↕️ چیدمان پلن‌ها — the only place a plan is moved.

    ⬆️⬇️ exist only in manual mode: in auto mode the order is computed, and an
    arrow would flip the mode as a side effect of a tap that looks like a move.
    """
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=mode_label, callback_data=f"{StoreCB.PKG_ORDER}{cat.id}")]
    ]
    if manual:
        for pkg_id, label in plans:
            rows.append(
                [
                    InlineKeyboardButton(text="⬆️", callback_data=f"{StoreCB.PKG_UP}{pkg_id}"),
                    InlineKeyboardButton(text=label, callback_data=f"{StoreCB.PKG}{pkg_id}"),
                    InlineKeyboardButton(text="⬇️", callback_data=f"{StoreCB.PKG_DN}{pkg_id}"),
                ]
            )
    rows.append(_dash_back(cat.id))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pkg_cb(pkg_id: int, *, origin: str) -> str:
    """Plan row callback carrying the list it was opened from.

    ``store_package_detail_keyboard`` has always accepted ``back_cb`` but no
    caller passed one, so entering a plan from «پلن‌های آماده» left no way back
    to it — the back button pointed at a category list instead.
    """
    return f"{StoreCB.PKG}{pkg_id}:{origin}"


def parse_pkg_cb(data: str) -> tuple[int | None, str | None]:
    """``adm:st:pkg:<id>[:<origin>]`` → (package_id, back_callback)."""
    rest = data[len(StoreCB.PKG) :] if data.startswith(StoreCB.PKG) else data
    parts = rest.split(":")
    if not parts or not parts[0].isdigit():
        return None, None
    pkg_id = int(parts[0])
    if len(parts) == 2:
        return pkg_id, {"r": StoreCB.READY, "c": StoreCB.CUSTOM}.get(parts[1])
    return pkg_id, None


def _pkg_origin_suffix(back_cb: str | None) -> str:
    origin = {StoreCB.READY: "r", StoreCB.CUSTOM: "c"}.get(back_cb or "", "")
    return f":{origin}" if origin else ""


def store_package_detail_keyboard(
    pkg: Any, *, is_owner: bool, back_cb: str | None = None
) -> InlineKeyboardMarkup:
    """One plan: toggles as state + action, edits, then delete alone at the end."""
    rows: list[list[InlineKeyboardButton]] = []
    sfx = _pkg_origin_suffix(back_cb)
    if is_owner:

        def toggle(label: str, on: bool, prefix: str) -> None:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=_toggle_label(label, on),
                        callback_data=f"{prefix}{pkg.id}{sfx}",
                    )
                ]
            )

        toggle("نمایش به مشتری", bool(pkg.enabled), StoreCB.PKG_EN)
        toggle("فروش جدید", bool(pkg.allow_new_sale), StoreCB.PKG_NS)
        toggle("تمدید", bool(pkg.allow_renew), StoreCB.PKG_RN)
        toggle("تخفیف اضافه", not pkg.forbid_extra_discount, StoreCB.PKG_FD)
        controls = visible_plan_controls(pkg)
        edits = [
            InlineKeyboardButton(text="✏️ عنوان", callback_data=f"{StoreCB.PKG_TITLE}{pkg.id}")
        ]
        if PLAN_PRICE in controls:
            edits.append(
                InlineKeyboardButton(text="💰 قیمت", callback_data=f"{StoreCB.PKG_PRICE}{pkg.id}")
            )
        if PLAN_DIMS in controls:
            edits.append(
                InlineKeyboardButton(
                    text="📐 حجم و مدت", callback_data=f"{StoreCB.PKG_DIMS}{pkg.id}"
                )
            )
        # Volume ready plans: device limit is its own edit, not part of dims.
        if PLAN_HWID in controls:
            edits.append(
                InlineKeyboardButton(
                    text="👥 کاربر همزمان", callback_data=f"{StoreCB.PKG_HWID}{pkg.id}"
                )
            )
        for i in range(0, len(edits), 2):
            rows.append(edits[i : i + 2])
        rows.append(
            [
                InlineKeyboardButton(
                    text="🗑 حذف این پلن",
                    callback_data=f"{StoreCB.PKG_DEL}{pkg.id}",
                    style=styled(DANGER),
                )
            ]
        )
    rows.append(_dash_back(int(pkg.category_id)) if back_cb is None else _back_hub())
    return InlineKeyboardMarkup(inline_keyboard=rows)


VOLUME_PRESETS_GB = (10, 20, 30, 50, 100, 200)
DURATION_PRESETS_DAYS = (30, 60, 90, 180, 365)
USER_PRESETS = (1, 2, 3, 4, 5)
HWID_PRESETS = (0, 1, 2, 3, 5)
DURATION_PRESETS_MONTHS = (1, 3, 6, 12)


def _preset_rows(
    values: tuple[int, ...], prefix: str, label: str
) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for value in values:
        row.append(
            InlineKeyboardButton(
                text=label.format(n=to_persian_digits(value)),
                callback_data=f"{prefix}{value}",
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def store_new_pkg_size_keyboard(*, kind: str) -> InlineKeyboardMarkup:
    """First step of building a plan — tap a size instead of typing a format.

    A single free-text «گیگ روز» prompt fought the operator's instinct, which is
    to describe the plan («۱۰ گیگ | یک ماهه»). Worse, a bare number typed there
    was read as a size, so a price could silently become a 60000 GB plan.
    """
    if kind == "unlimited":
        rows = _preset_rows(USER_PRESETS, StoreCB.PKG_SIZE, "{n} کاربره")
    else:
        rows = _preset_rows(VOLUME_PRESETS_GB, StoreCB.PKG_SIZE, "{n} گیگ")
    rows.append([InlineKeyboardButton(text="✏️ مقدار دیگر", callback_data=StoreCB.PKG_SIZE_OTHER)])
    rows.append([InlineKeyboardButton(text="انصراف", callback_data=StoreCB.HUB)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def store_new_pkg_duration_keyboard(*, kind: str) -> InlineKeyboardMarkup:
    if kind == "unlimited":
        rows = _preset_rows(DURATION_PRESETS_MONTHS, StoreCB.PKG_DUR, "{n} ماه")
    else:
        rows = _preset_rows(DURATION_PRESETS_DAYS, StoreCB.PKG_DUR, "{n} روز")
        # Only a volume plan can sensibly have no expiry.
        rows.append(
            [InlineKeyboardButton(text="♾ بدون انقضا", callback_data=f"{StoreCB.PKG_DUR}0")]
        )
    rows.append([InlineKeyboardButton(text="✏️ مقدار دیگر", callback_data=StoreCB.PKG_DUR_OTHER)])
    rows.append([InlineKeyboardButton(text="انصراف", callback_data=StoreCB.HUB)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def store_new_pkg_users_keyboard() -> InlineKeyboardMarkup:
    """Device-limit presets for a volume plan (0 = نامحدود)."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for value in HWID_PRESETS:
        label = "نامحدود" if value == 0 else to_persian_digits(value)
        row.append(InlineKeyboardButton(text=label, callback_data=f"{StoreCB.PKG_USERS}{value}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✏️ مقدار دیگر", callback_data=StoreCB.PKG_USERS_OTHER)])
    rows.append([InlineKeyboardButton(text="انصراف", callback_data=StoreCB.HUB)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def store_new_pkg_confirm_keyboard() -> InlineKeyboardMarkup:
    """The one confirmation between building a plan and customers seeing it.

    The owner asked for exactly this and nothing more: look at what you made,
    say yes, it is live. No separate «انتشار» trip afterwards.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ تأیید و فعال‌سازی",
                    style=styled(SUCCESS),
                    callback_data=StoreCB.PKG_ACTIVATE,
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ تغییر عنوان",
                    callback_data=StoreCB.PKG_KEEP_TITLE,
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ انصراف",
                    callback_data=StoreCB.PKG_DISCARD,
                    style=styled(DANGER),
                )
            ],
        ]
    )


def store_category_durations_keyboard(cat: Any) -> InlineKeyboardMarkup:
    """List category durations with per-row delete; add + back."""
    rows: list[list[InlineKeyboardButton]] = []
    for d in category_durations(cat):
        enc = encode_duration_cb(d)
        label = duration_label_fa(d)
        rows.append(
            [
                InlineKeyboardButton(text=label, callback_data=f"{StoreCB.CAT_DURS}{cat.id}"),
                InlineKeyboardButton(
                    text="🗑",
                    callback_data=f"{StoreCB.CAT_DUR_DEL}{cat.id}:{enc}",
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=texts.admin_store_dur_add,
                callback_data=f"{StoreCB.CAT_DUR_ADD}{cat.id}",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="⬅️ فروش", callback_data=f"{StoreCB.SEC_SALE}{cat.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def store_duration_add_keyboard(category_id: int) -> InlineKeyboardMarkup:
    """Prompt keyboard: بدون انقضا + cancel back to durations list."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.admin_store_dur_none_btn,
                    callback_data=f"{StoreCB.CAT_DUR_NONE}{category_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ انصراف",
                    callback_data=f"{StoreCB.CAT_DURS}{category_id}",
                )
            ],
        ]
    )


def store_duration_delete_confirm_keyboard(
    category_id: int, duration_days: int | None
) -> InlineKeyboardMarkup:
    enc = encode_duration_cb(duration_days)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 بله، حذف کن",
                    callback_data=f"{StoreCB.CAT_DUR_DEL_OK}{category_id}:{enc}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ بازگشت",
                    callback_data=f"{StoreCB.CAT_DURS}{category_id}",
                )
            ],
        ]
    )


def store_category_delete_confirm_keyboard(category_id: int) -> InlineKeyboardMarkup:
    """Second step of a group deletion — the preview step changes nothing."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 بله، حذف کن",
                    callback_data=f"{StoreCB.CAT_DEL_OK}{category_id}",
                ),
                InlineKeyboardButton(text="انصراف", callback_data=f"{StoreCB.CAT}{category_id}"),
            ]
        ]
    )


def store_package_delete_confirm_keyboard(package_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 بله، حذف کن",
                    callback_data=f"{StoreCB.PKG_DEL_OK}{package_id}",
                ),
                InlineKeyboardButton(text="انصراف", callback_data=f"{StoreCB.PKG}{package_id}"),
            ]
        ]
    )


def store_price_jump_keyboard(package_id: int) -> InlineKeyboardMarkup:
    """The one extra step for a price 10 times the current one or a tenth of it."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ بله، همین قیمت",
                    callback_data=StoreCB.PKG_PRICE_OK,
                    style=styled(PRIMARY),
                )
            ],
            [InlineKeyboardButton(text="انصراف", callback_data=f"{StoreCB.PKG}{package_id}")],
        ]
    )


def store_cancel_input_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="لغو", callback_data=StoreCB.HUB)],
        ]
    )


def store_pay_keyboard(*, can_edit: bool, has_override: bool) -> InlineKeyboardMarkup:
    """Four fields, two to a row; «back to .env» only when there is an override."""
    rows: list[list[InlineKeyboardButton]] = []
    if can_edit:

        def _b(label: str, field: str) -> InlineKeyboardButton:
            return InlineKeyboardButton(text=label, callback_data=f"{StoreCB.PAY_EDIT}{field}")

        rows.append([_b("🔢 شماره کارت", "card_number"), _b("👤 صاحب کارت", "card_holder_name")])
        rows.append([_b("🏛 بانک", "bank_name"), _b("📝 توضیح", "instructions")])
        if has_override:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="♻️ برگشت به .env",
                        callback_data=StoreCB.PAY_CLEAR,
                        style=styled(DANGER),
                    )
                ]
            )
    rows.extend([_back_hub(), _back_home()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def store_pay_card_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ تأیید و ذخیره",
                    callback_data=StoreCB.PAY_CARD_OK,
                    style=styled(PRIMARY),
                )
            ],
            [InlineKeyboardButton(text="⬅️ انصراف", callback_data=StoreCB.PAY)],
        ]
    )


def store_pay_clear_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="♻️ بله، برگشت به .env",
                    callback_data=StoreCB.PAY_CLEAR_OK,
                    style=styled(DANGER),
                )
            ],
            [InlineKeyboardButton(text="⬅️ انصراف", callback_data=StoreCB.PAY)],
        ]
    )


def store_pay_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="لغو", callback_data=StoreCB.PAY)]]
    )


def store_back_hub_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[_back_hub(), _back_home()])


def store_category_kind_pick_keyboard() -> InlineKeyboardMarkup:
    """Used when creating a new product group — pick volume vs unlimited kind."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📦 حجمی (volume)",
                    callback_data=f"{StoreCB.CAT_ADD_KIND}volume",
                )
            ],
            [
                InlineKeyboardButton(
                    text="♾️ نامحدود (unlimited)",
                    callback_data=f"{StoreCB.CAT_ADD_KIND}unlimited",
                )
            ],
            [InlineKeyboardButton(text="⬅️ انصراف", callback_data=StoreCB.HUB)],
        ]
    )


def store_group_move_keyboard(category_id: int, old_groups: list[int]) -> InlineKeyboardMarkup:
    """The owner's question after changing a group's panel group.

    The old groups ride in the button: only a service still on them moves, and
    after the save nothing else remembers what they were.
    """
    old = "-".join(str(int(g)) for g in old_groups)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ منتقل کن",
                    callback_data=f"{StoreCB.CAT_GROUP_MOVE}{category_id}:{old}",
                    style=styled(PRIMARY),
                )
            ],
            [
                InlineKeyboardButton(
                    text="نه، فقط سرویس‌های جدید",
                    callback_data=f"{StoreCB.CAT_GROUP_KEEP}{category_id}",
                )
            ],
        ]
    )


def store_group_move_done_keyboard(category_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ پنل",
                    callback_data=f"{StoreCB.SEC_PANEL}{category_id}",
                )
            ],
            _back_hub(),
        ]
    )


def store_confirm_keyboard(
    *, confirm_cb: str, back_to: str, back_label: str
) -> InlineKeyboardMarkup:
    """Two-step guard. 🗑 sat next to a harmless toggle in the same row."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 بله، حذف کن", callback_data=confirm_cb)],
            [InlineKeyboardButton(text=f"⬅️ {back_label}", callback_data=back_to)],
        ]
    )

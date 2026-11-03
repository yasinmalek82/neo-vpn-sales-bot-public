"""SIMPLE catalog helpers: product-group kind + volume time-limit filter.

Layer-1 groups are free-form ``StoreCategory`` rows. Volume-like groups may
optionally enable ``sales_flags.time_limit_filter_enabled`` so the customer
sees «با محدودیت زمانی» / «بدون محدودیت زمانی» before ready plans.

``volume_time_limit`` may appear in ``purchase_steps`` / PURCHASE_STEP_CATALOG
for documentation in the steps editor, but the **category flag wins**: the
customer step is shown iff the entered group is volume-like AND
``time_limit_filter_enabled`` is true — regardless of the global step list.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from database.models.store import StoreCategory, StorePackage, StorePackageKind
from services.store_pricing import (
    DEFAULT_TIER,
    available_durations,
    category_durations,
    category_tiers,
    duration_from_package_options,
    get_price,
)
from services.ux_format import format_toman, parse_positive_int, to_persian_digits

CATEGORY_KIND_VOLUME = "volume"
CATEGORY_KIND_UNLIMITED = "unlimited"
ALLOWED_CATEGORY_KINDS = frozenset({CATEGORY_KIND_VOLUME, CATEGORY_KIND_UNLIMITED})

# Persian / slug heuristics when sales_flags.kind is absent (legacy seeds).
_VOLUME_HINTS = ("volume", "حجم", "حجمی")
_UNLIMITED_HINTS = ("unlimited", "نامحدود")


def category_kind(cat: StoreCategory | dict[str, Any] | None) -> str | None:
    """Return ``volume`` | ``unlimited`` | None.

    Prefer explicit ``sales_flags.kind``, then legacy ``plan_type``, then
    slug/title heuristics.
    """
    if cat is None:
        return None
    if isinstance(cat, dict):
        flags = cat.get("sales_flags") or {}
        slug = str(cat.get("slug") or "")
        title = str(cat.get("title_fa") or "")
    else:
        flags = dict(cat.sales_flags or {})
        # getattr: a stand-in group in a keyboard test has flags and nothing else.
        slug = str(getattr(cat, "slug", "") or "")
        title = str(getattr(cat, "title_fa", "") or "")
    if not isinstance(flags, dict):
        flags = {}

    explicit = flags.get("kind") or flags.get("plan_type")
    if isinstance(explicit, str) and explicit.strip().lower() in ALLOWED_CATEGORY_KINDS:
        return explicit.strip().lower()

    blob = f"{slug} {title}".lower()
    if any(h in blob for h in _VOLUME_HINTS):
        return CATEGORY_KIND_VOLUME
    if any(h in blob for h in _UNLIMITED_HINTS):
        return CATEGORY_KIND_UNLIMITED
    return None


def is_volume_like(cat: StoreCategory | dict[str, Any] | None) -> bool:
    return category_kind(cat) == CATEGORY_KIND_VOLUME


def is_unlimited_like(cat: StoreCategory | dict[str, Any] | None) -> bool:
    return category_kind(cat) == CATEGORY_KIND_UNLIMITED


def time_limit_filter_enabled(cat: StoreCategory | dict[str, Any] | None) -> bool:
    """True only for volume-like groups with the flag ON. Unlimited → always False."""
    if not is_volume_like(cat):
        return False
    if isinstance(cat, dict):
        flags = cat.get("sales_flags") or {}
    else:
        flags = dict((cat.sales_flags if cat is not None else None) or {})
    if not isinstance(flags, dict):
        return False
    return bool(flags.get("time_limit_filter_enabled"))


CUSTOM_PURCHASE_OFF = "off"
CUSTOM_PURCHASE_RESELLERS = "resellers"
CUSTOM_PURCHASE_EVERYONE = "everyone"
# Order of the owner's cycling button: off -> resellers only -> everyone -> off.
CUSTOM_PURCHASE_MODES = (CUSTOM_PURCHASE_OFF, CUSTOM_PURCHASE_RESELLERS, CUSTOM_PURCHASE_EVERYONE)

# What a buyer may type at the custom builder's volume question. This is the
# customer's limit; ``services/store_admin.py::DIM_LIMITS`` is the owner's, for
# the size of a plan he sells, and is a different number on purpose.
CUSTOM_VOLUME_GB_MIN = 1
CUSTOM_VOLUME_GB_MAX = 1000


def custom_purchase_mode(cat: StoreCategory | dict[str, Any] | None) -> str:
    """Who may use the گیگ/ماه builder in this group: off, resellers, or everyone.

    Stored as ``sales_flags.custom_purchase_mode``. A group saved before the
    three states carries only the old ``allow_custom_purchase`` bool: true read
    as everyone (the old default was on, so a missing flag does too), false as
    off. The new key wins when both exist.
    """
    if cat is None:
        return CUSTOM_PURCHASE_EVERYONE
    flags = cat.get("sales_flags") if isinstance(cat, dict) else cat.sales_flags
    return _custom_mode_of_flags(dict(flags or {}))


def _custom_mode_of_flags(flags: dict[str, Any]) -> str:
    mode = flags.get("custom_purchase_mode")
    if mode in CUSTOM_PURCHASE_MODES:
        return str(mode)
    legacy_on = bool(flags.get("allow_custom_purchase", True))
    return CUSTOM_PURCHASE_EVERYONE if legacy_on else CUSTOM_PURCHASE_OFF


def next_custom_purchase_mode(mode: str) -> str:
    """The owner's button: off -> resellers only -> everyone -> off."""
    index = CUSTOM_PURCHASE_MODES.index(mode) if mode in CUSTOM_PURCHASE_MODES else 0
    return CUSTOM_PURCHASE_MODES[(index + 1) % len(CUSTOM_PURCHASE_MODES)]


def parse_custom_volume_gb(raw: str) -> int | None:
    """A typed volume: a whole number of gigabytes in the limits, else ``None``.

    Persian and Latin digits both read. ``None`` for zero, negatives, decimals,
    text and anything over the limit, so the caller refuses with the range and
    never sees a raw ``ValueError``.
    """
    try:
        value = parse_positive_int(raw or "")
    except ValueError:  # str.isdigit() is true for «²», int() refuses it
        return None
    if value is None or not CUSTOM_VOLUME_GB_MIN <= value <= CUSTOM_VOLUME_GB_MAX:
        return None
    return value


def custom_purchase_available(cat: StoreCategory | dict[str, Any] | None, pricing: Any) -> bool:
    """Whether the گیگ/ماه builder can really be opened for this group.

    The builder asks its questions from the pricing document's custom plan. A
    store built in the admin panel has none, so a group on the customer's shelf
    with only the toggle behind it was a dead end and the first tap raised
    ``KeyError: 'volume_plan'``. Who is *offered* the builder is
    :func:`custom_purchase_offered`, which builds on this.
    """
    doc = pricing if isinstance(pricing, dict) else {}
    if category_kind(cat) == CATEGORY_KIND_UNLIMITED:
        plan, needed = doc.get("unlimited_plan"), ("user_options", "duration_options_months")
    else:
        plan, needed = doc.get("volume_plan"), ("volume_options_gb", "duration_options_days")
    return isinstance(plan, dict) and all(plan.get(key) for key in needed)


def custom_purchase_offered(
    cat: StoreCategory | dict[str, Any] | None,
    pricing: Any,
    *,
    is_reseller: bool,
    has_per_gb_rate: bool,
) -> bool:
    """Is the custom builder offered to THIS buyer in THIS group?

    The shelf, the «🧩 خرید سفارشی» button, the direct entry of a group with no
    ready plans and every handler of the builder ask this and nothing else, so
    a stale or forged callback cannot open it for someone who is not offered it.

    - **never in an unlimited group**, whatever its stored state: that product
      is sold by users and months, and a per-gigabyte rate (a shop's, or the
      retail curve) has nothing to price there. A stored value is not
      rewritten; it is simply not read;
    - the builder must be openable (:func:`custom_purchase_available`);
    - state off: nobody; resellers only: a reseller; everyone: any buyer;
    - a reseller also needs a per-gigabyte rate. Without one the custom price
      would fall through to retail (``quote_custom_volume``), and a shop must
      never be charged retail by surprise.
    """
    if is_unlimited_like(cat):
        return False
    if not custom_purchase_available(cat, pricing):
        return False
    mode = custom_purchase_mode(cat)
    if mode == CUSTOM_PURCHASE_OFF:
        return False
    if is_reseller:
        return has_per_gb_rate
    return mode == CUSTOM_PURCHASE_EVERYONE


def flat_plan_list_enabled(cat: StoreCategory | dict[str, Any] | None) -> bool:
    """One ascending ladder of ready plans; no duration/tier shelf tabs."""
    if cat is None:
        return False
    if isinstance(cat, dict):
        flags = cat.get("sales_flags") or {}
    else:
        flags = dict((cat.sales_flags if cat is not None else None) or {})
    if not isinstance(flags, dict):
        return False
    return bool(flags.get("flat_plan_list"))


def plan_duration_label_fa(
    *,
    duration_days: int | None = None,
    duration_months: int | None = None,
) -> str:
    """Shelf/admin duration fragment: «۲ ماهه» / «۴۵ روزه» / «بدون انقضا»."""
    label = "بدون انقضا"
    if duration_months is not None:
        try:
            months = int(duration_months)
        except (TypeError, ValueError):
            months = 0
        if months > 0:
            label = f"{to_persian_digits(months)} ماهه"
    elif duration_days is not None:
        try:
            days = int(duration_days)
        except (TypeError, ValueError):
            days = 0
        if days > 0 and days % 30 == 0:
            label = f"{to_persian_digits(days // 30)} ماهه"
        elif days > 0:
            label = f"{to_persian_digits(days)} روزه"
    return label


def flat_plan_dim_label(pkg: StorePackage | dict[str, Any] | Any) -> str | None:
    """Dimension-only label for a ready plan (no price). ``None`` if dims missing."""
    if isinstance(pkg, dict):
        opts = pkg.get("options") or {}
    else:
        opts = dict(getattr(pkg, "options", None) or {})
    if not isinstance(opts, dict):
        opts = {}

    gb = opts.get("volume_gb")
    if gb is None:
        gb = opts.get("data_limit_gb")
    if gb is not None:
        try:
            gb_i = int(gb)
        except (TypeError, ValueError):
            return None
        days = opts.get("duration_days")
        days_i: int | None
        try:
            days_i = int(days) if days is not None else None
        except (TypeError, ValueError):
            days_i = None
        dur = plan_duration_label_fa(duration_days=days_i)
        return f"{to_persian_digits(gb_i)} گیگ • {dur}"

    users = opts.get("concurrent_users")
    if users is None:
        users = opts.get("user_count")
    if users is not None:
        try:
            users_i = int(users)
        except (TypeError, ValueError):
            return None
        months = opts.get("duration_months")
        months_i: int | None
        try:
            months_i = int(months) if months is not None else None
        except (TypeError, ValueError):
            months_i = None
        if months_i is None:
            days = opts.get("duration_days")
            try:
                days_i = int(days) if days is not None else None
            except (TypeError, ValueError):
                days_i = None
            dur = plan_duration_label_fa(duration_days=days_i)
        else:
            dur = plan_duration_label_fa(duration_months=months_i)
        return f"{to_persian_digits(users_i)} کاربره • {dur}"
    return None


def compact_toman_fa(price: int) -> str:
    """Shelf total: ``N هزار`` when divisible by 1000; else full ``format_toman``."""
    value = int(price)
    if value >= 1000 and value % 1000 == 0:
        thousands = value // 1000
        return f"{format_toman(thousands, persian_digits=True)} هزار"
    return format_toman(value, persian_digits=True)


def flat_plan_button_label(pkg: StorePackage | dict[str, Any] | Any, price_toman: int) -> str:
    """Flat shelf button: dims – dur | price (گیگی …) for volume. No «تومان»."""
    dim = flat_plan_dim_label(pkg)
    price_fa = compact_toman_fa(int(price_toman))
    if dim is None:
        title = str(pkg.get("title_fa") if isinstance(pkg, dict) else getattr(pkg, "title_fa", ""))
        return f"{title} • {price_fa}"

    # dim uses « • » between size and duration; shelf buttons use « – » and « | ».
    dim_shelf = dim.replace(" • ", " \u2013 ", 1)

    if isinstance(pkg, dict):
        opts = pkg.get("options") or {}
    else:
        opts = dict(getattr(pkg, "options", None) or {})
    if not isinstance(opts, dict):
        opts = {}
    gb = opts.get("volume_gb")
    if gb is None:
        gb = opts.get("data_limit_gb")
    if gb is not None:
        try:
            gb_i = int(gb)
        except (TypeError, ValueError):
            gb_i = 0
        if gb_i > 0:
            per_fa = format_toman(int(price_toman) // gb_i, persian_digits=True)
            return f"{dim_shelf} | {price_fa} (گیگی {per_fa})"
    return f"{dim_shelf} | {price_fa}"


def shelf_plan_label(
    pkg: StorePackage | dict[str, Any] | Any, price_toman: int | None, *, flat_mode: bool
) -> str:
    """The text of one ready plan's button on the customer's shelf.

    ``ready_plans_keyboard`` calls this for every plan button, and the Mini App's
    plan preview calls it with an in-memory stand-in for the plan being typed, so
    the owner sees the label the customer will see and not a second copy of it.
    The flat list builds it from the plan's dimensions; the duration tabs from its
    title. Longer than 60 characters is cut with «…», as Telegram's row would.
    """
    if flat_mode and price_toman is not None:
        label = flat_plan_button_label(pkg, int(price_toman))
    else:
        title = pkg.get("title_fa") if isinstance(pkg, dict) else getattr(pkg, "title_fa", "")
        label = str(title)
        if price_toman is not None:
            label = f"{label} — {format_toman(int(price_toman), persian_digits=True)}ت"
    if len(label) > 60:
        label = label[:57] + "…"
    return label


def ladder_warnings(
    plans_with_prices: Sequence[tuple[Any, int]],
) -> list[tuple[str, str, int, int]]:
    """Per-GB non-monotonic pairs on a volume ladder.

    ``plans_with_prices`` must already be sorted ascending by GB. Returns
    ``(small_label, big_label, small_per_gb, big_per_gb)`` for each consecutive
    pair where the bigger plan is *not* cheaper-or-equal per GB. Plans without
    a positive GB or price are skipped. Unlimited plans yield ``[]``.
    """
    volume_rows: list[tuple[str, int, int]] = []
    for pkg, price in plans_with_prices:
        if price is None or int(price) <= 0:
            continue
        if isinstance(pkg, dict):
            opts = pkg.get("options") or {}
        else:
            opts = dict(getattr(pkg, "options", None) or {})
        if not isinstance(opts, dict):
            continue
        gb_raw = opts.get("volume_gb")
        if gb_raw is None:
            gb_raw = opts.get("data_limit_gb")
        if gb_raw is None:
            continue
        try:
            gb = int(gb_raw)
        except (TypeError, ValueError):
            continue
        if gb <= 0:
            continue
        label = flat_plan_dim_label(pkg)
        if label is None:
            continue
        volume_rows.append((label, gb, int(price)))

    out: list[tuple[str, str, int, int]] = []
    for i in range(len(volume_rows) - 1):
        small_label, small_gb, small_price = volume_rows[i]
        big_label, big_gb, big_price = volume_rows[i + 1]
        if big_gb <= small_gb:
            continue
        small_per = small_price // small_gb
        big_per = big_price // big_gb
        if big_per > small_per:
            out.append((small_label, big_label, small_per, big_per))
    return out


def duration_has_tab(cat: StoreCategory | dict[str, Any] | None, duration_days: int | None) -> bool:
    """Whether the customer's shelf has a place for a plan of this duration.

    The flat list shows every plan at its own duration. Under «تب مدت» the tabs
    are the owner's own list (``sales_flags.durations``, «⏱ مدت‌ها»), and a
    plan outside it is simply not shown.
    """
    return flat_plan_list_enabled(cat) or duration_days in category_durations(cat)


def plans_without_tab(
    cat: StoreCategory, packages: Sequence[Any], prices: Sequence[Any]
) -> list[Any]:
    """Live ready plans no duration tab of the group shows (no price cell under any).

    Mirrors the shelf: a tab lists a plan only when it has a price cell for
    that tab's duration. Live, «۵ گیگ بدون انقضا» sat in a group whose only
    tab was «۱ ماهه»; the dashboard counted it and the customer never saw it.
    """
    if flat_plan_list_enabled(cat):
        return []
    hidden = []
    for pkg in packages:
        if pkg.kind != StorePackageKind.ready or not pkg.enabled:
            continue
        slug = str(pkg.slug)
        if not any(
            available_durations(list(prices), slug=slug, tier=tier, category=cat)
            for tier in category_tiers(cat)
        ):
            hidden.append(pkg)
    return hidden


def package_has_time_limit(pkg: StorePackage | dict[str, Any] | None) -> bool:
    """Read ``has_time_limit`` from sales_flags, then options (default False)."""
    if pkg is None:
        return False
    if isinstance(pkg, dict):
        flags = pkg.get("sales_flags") or {}
        opts = pkg.get("options") or {}
    else:
        flags = dict(pkg.sales_flags or {})
        opts = dict(pkg.options or {})
    if not isinstance(flags, dict):
        flags = {}
    if not isinstance(opts, dict):
        opts = {}
    if "has_time_limit" in flags:
        return bool(flags.get("has_time_limit"))
    if "has_time_limit" in opts:
        return bool(opts.get("has_time_limit"))
    return False


def plan_order_mode(category: StoreCategory | dict[str, Any] | None) -> str:
    """Return ``auto`` (default) or ``manual`` from ``sales_flags.plan_order_mode``."""
    if category is None:
        return "auto"
    if isinstance(category, dict):
        flags = category.get("sales_flags") or {}
    else:
        flags = dict(category.sales_flags or {})
    if not isinstance(flags, dict):
        return "auto"
    mode = flags.get("plan_order_mode")
    if isinstance(mode, str) and mode.strip().lower() == "manual":
        return "manual"
    return "auto"


def _dim_value(pkg: StorePackage, key: str) -> int | None:
    opts = dict(pkg.options or {})
    raw = opts.get(key)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def sort_packages_for_display(
    packages: Sequence[StorePackage], category: StoreCategory | None
) -> list[StorePackage]:
    """Stable display order for a category's packages (admin list + customer shelf).

    ``auto`` (default): sort by the category's own dimension ascending; a plan
    missing that key sorts last. ``manual``: ``(sort_order, id)`` as today.
    Final tiebreak is always ``(sort_order, id)``.
    """
    pkgs = list(packages)
    if plan_order_mode(category) != "auto" or category is None:
        return sorted(pkgs, key=lambda p: (int(p.sort_order), int(p.id)))

    if is_volume_like(category):
        dim_key = "volume_gb"
        dur_key = "duration_days"
    elif is_unlimited_like(category):
        dim_key = "concurrent_users"
        dur_key = "duration_months"
    else:
        return sorted(pkgs, key=lambda p: (int(p.sort_order), int(p.id)))

    def sort_key(p: StorePackage) -> tuple[int, int, int, int, int, int]:
        val = _dim_value(p, dim_key)
        missing = 1 if val is None else 0
        # Volume secondary: duration_days, else months*30 from options.
        if dim_key == "volume_gb":
            dur = _dim_value(p, dur_key)
            if dur is None:
                months = _dim_value(p, "duration_months")
                dur = None if months is None else months * 30
        else:
            dur = _dim_value(p, dur_key)
            if dur is None:
                days = _dim_value(p, "duration_days")
                dur = None if days is None else max(1, days // 30)
        dur_missing = 1 if dur is None else 0
        return (
            missing,
            0 if val is None else val,
            dur_missing,
            0 if dur is None else dur,
            int(p.sort_order),
            int(p.id),
        )

    return sorted(pkgs, key=sort_key)


def filter_ready_plans_by_time_limit(
    packages: list[StorePackage],
    *,
    category: StoreCategory | None = None,
) -> list[StorePackage]:
    """Ready + enabled + allow_new_sale plans, in display order.

    The old ``want_time_limit`` filter was removed with the purchase duration
    toggle (T-006); «بدون انقضا» is now a duration tab, not a prior step.
    """
    ready: list[StorePackage] = []
    for pkg in packages:
        kind_val = pkg.kind.value if hasattr(pkg.kind, "value") else str(pkg.kind)
        if kind_val not in {StorePackageKind.ready.value, "ready"}:
            continue
        if not pkg.enabled or not pkg.allow_new_sale:
            continue
        ready.append(pkg)
    return sort_packages_for_display(ready, category)


def package_hwid_limit(pkg: StorePackage | dict[str, Any] | None) -> int:
    """Device limit for a ready volume plan. Missing key ⇒ 0 (نامحدود).

    Unlimited plans use ``concurrent_users`` instead; callers must not use this
    for that path. ``0`` is a real value (HWID check off) — never ``or 1``.
    """
    if pkg is None:
        return 0
    opts = pkg.get("options") or {} if isinstance(pkg, dict) else dict(pkg.options or {})
    if not isinstance(opts, dict):
        return 0
    raw = opts.get("hwid_limit")
    if raw is None:
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def category_volume_hwid_limit(category: StoreCategory | dict[str, Any] | None) -> int:
    """Custom-volume device limit from ``sales_flags.volume_hwid_limit`` (default 0)."""
    if category is None:
        return 0
    if isinstance(category, dict):
        flags = category.get("sales_flags") or {}
    else:
        flags = dict(category.sales_flags or {})
    if not isinstance(flags, dict):
        return 0
    raw = flags.get("volume_hwid_limit")
    if raw is None:
        # Older seeds wrote ``default_hwid_limit``; prefer the new key when absent.
        raw = flags.get("default_hwid_limit")
    if raw is None:
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


# A price typed into the trial prompt must not become a 150000-GB trial.
TRIAL_GB_MAX = 100
TRIAL_DAYS_MAX = 30


def _flags_of(category: StoreCategory | dict[str, Any] | None) -> dict[str, Any]:
    if category is None:
        return {}
    if isinstance(category, dict):
        flags = category.get("sales_flags") or {}
    else:
        flags = dict(category.sales_flags or {})
    return flags if isinstance(flags, dict) else {}


def category_trial_shape(
    category: StoreCategory | dict[str, Any] | None,
) -> tuple[int, int] | None:
    """``(gb, days)`` of this group's free trial, or ``None`` when it has none.

    A trial belongs to the group it tests: its panel group is the group's own,
    so a customer on a direct trial learns whether direct works on their ISP.
    Whole GB only — the order and panel paths are GB-wide, and a fraction that
    rounds to 0 would be read as unlimited data.
    """
    flags = _flags_of(category)
    try:
        gb = int(flags.get("trial_gb") or 0)
        days = int(flags.get("trial_days") or 0)
    except (TypeError, ValueError):
        return None
    if gb <= 0 or days <= 0:
        return None
    return gb, days


def category_reseller_trial(category: StoreCategory | dict[str, Any] | None) -> bool:
    """Whether a reseller's giveaway may come from this group."""
    return bool(_flags_of(category).get("reseller_trial"))


def hwid_limit_label_fa(limit: int) -> str:
    """«نامحدود» for 0, otherwise Persian digits."""
    if int(limit) <= 0:
        return "نامحدود"
    return to_persian_digits(int(limit))


def ready_package_purchase_payload(
    pkg: StorePackage,
    prices: list[Any] | None = None,
    *,
    duration_days: int | None = None,
    tier: str = DEFAULT_TIER,
    use_selected_duration: bool = False,
) -> dict[str, Any]:
    """Map a ready package into purchase FSM fields (dims + optional fixed price).

    When ``use_selected_duration`` is true, the price cell and volume
    ``duration_days`` come from the shelf selection (T-006), not from
    ``options``. ``duration_days=None`` then means «بدون انقضا».
    """
    opts = dict(pkg.options or {})
    flags = dict(pkg.sales_flags or {})
    payload: dict[str, Any] = {
        "store_package_id": pkg.id,
        "store_package_slug": pkg.slug,
        "ready_title_fa": pkg.title_fa,
        "forbid_extra_discount": bool(pkg.forbid_extra_discount),
        "has_time_limit": package_has_time_limit(pkg),
    }
    if prices is not None:
        lookup_days = (
            duration_days if use_selected_duration else duration_from_package_options(opts)
        )
        cell = get_price(
            prices,
            slug=str(pkg.slug),
            duration_days=lookup_days,
            tier=tier,
        )
        if cell is not None:
            payload["fixed_price_toman"] = int(cell)

    # Volume dims
    if opts.get("volume_gb") is not None:
        payload["volume_gb"] = int(opts["volume_gb"])
    elif opts.get("data_limit_gb") is not None:
        payload["volume_gb"] = int(opts["data_limit_gb"])
    if opts.get("duration_days") is not None:
        payload["duration_days"] = int(opts["duration_days"])

    # Unlimited dims
    if opts.get("concurrent_users") is not None:
        payload["concurrent_users"] = int(opts["concurrent_users"])
    elif opts.get("user_count") is not None:
        payload["concurrent_users"] = int(opts["user_count"])
    if opts.get("duration_months") is not None:
        payload["duration_months"] = int(opts["duration_months"])

    # Volume device limit (0 = نامحدود). Unlimited plans keep concurrent_users.
    if payload.get("volume_gb") is not None:
        payload["hwid_limit"] = package_hwid_limit(pkg)

    # Carry flag mirror for admin visibility
    if "has_time_limit" in flags:
        payload["has_time_limit"] = bool(flags["has_time_limit"])
    if use_selected_duration:
        # Selected shelf duration wins over options (including None = بدون انقضا).
        payload["duration_days"] = duration_days
        if duration_days is None:
            payload.pop("duration_months", None)
    return payload


def normalize_category_sales_flags(
    flags: dict[str, Any] | None,
    *,
    kind: str | None = None,
    time_limit_filter_enabled: bool | None = None,
    custom_purchase_mode: str | None = None,
    flat_plan_list: bool | None = None,
    volume_hwid_limit: int | None = None,
    trial_gb: int | None = None,
    trial_days: int | None = None,
    reseller_trial: bool | None = None,
) -> dict[str, Any]:
    """Normalize category sales_flags; force filter off for non-volume kinds."""
    out = dict(flags or {})
    if kind is not None:
        k = str(kind).strip().lower()
        if k not in ALLOWED_CATEGORY_KINDS:
            raise ValueError(f"invalid category kind: {kind}")
        out["kind"] = k
        out["plan_type"] = k  # legacy mirror for older readers
    resolved_kind = out.get("kind") or out.get("plan_type")
    resolved_kind = resolved_kind.strip().lower() if isinstance(resolved_kind, str) else None

    if resolved_kind != CATEGORY_KIND_VOLUME:
        out["time_limit_filter_enabled"] = False
    elif time_limit_filter_enabled is not None:
        out["time_limit_filter_enabled"] = bool(time_limit_filter_enabled)
    else:
        out.setdefault("time_limit_filter_enabled", False)

    # One master (``custom_purchase_mode``), read through ``custom_purchase_mode()``;
    # the old bool is written alongside as true only for «everyone», so an older
    # reader never shows custom to more people than the owner chose.
    if custom_purchase_mode is not None:
        if custom_purchase_mode not in CUSTOM_PURCHASE_MODES:
            raise ValueError(f"invalid custom purchase mode: {custom_purchase_mode}")
        mode = custom_purchase_mode
    else:
        mode = _custom_mode_of_flags(out)
    out["custom_purchase_mode"] = mode
    out["allow_custom_purchase"] = mode == CUSTOM_PURCHASE_EVERYONE

    if flat_plan_list is not None:
        out["flat_plan_list"] = bool(flat_plan_list)
    else:
        out.setdefault("flat_plan_list", False)

    if volume_hwid_limit is not None:
        out["volume_hwid_limit"] = max(0, int(volume_hwid_limit))
    elif "volume_hwid_limit" not in out and "default_hwid_limit" in out:
        try:
            out["volume_hwid_limit"] = max(0, int(out["default_hwid_limit"]))
        except (TypeError, ValueError):
            out.setdefault("volume_hwid_limit", 0)
    else:
        out.setdefault("volume_hwid_limit", 0)

    if trial_gb is not None:
        out["trial_gb"] = max(0, int(trial_gb))
    else:
        out.setdefault("trial_gb", 0)
    if trial_days is not None:
        out["trial_days"] = max(0, int(trial_days))
    else:
        out.setdefault("trial_days", 0)
    if reseller_trial is not None:
        out["reseller_trial"] = bool(reseller_trial)
    else:
        out.setdefault("reseller_trial", False)

    out.setdefault("durations", [30])
    out.setdefault("tiers_enabled", False)
    out.setdefault("premium_panel_group_ids", [])
    return out


def set_package_has_time_limit(
    sales_flags: dict[str, Any] | None,
    options: dict[str, Any] | None,
    *,
    has_time_limit: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist ``has_time_limit`` on sales_flags; mirror into options for peek UIs."""
    flags = dict(sales_flags or {})
    opts = dict(options or {})
    flags["has_time_limit"] = bool(has_time_limit)
    opts["has_time_limit"] = bool(has_time_limit)
    return flags, opts


__all__ = [
    "CATEGORY_KIND_VOLUME",
    "CATEGORY_KIND_UNLIMITED",
    "ALLOWED_CATEGORY_KINDS",
    "category_kind",
    "is_volume_like",
    "is_unlimited_like",
    "time_limit_filter_enabled",
    "CUSTOM_PURCHASE_OFF",
    "CUSTOM_PURCHASE_RESELLERS",
    "CUSTOM_PURCHASE_EVERYONE",
    "CUSTOM_PURCHASE_MODES",
    "CUSTOM_VOLUME_GB_MIN",
    "CUSTOM_VOLUME_GB_MAX",
    "custom_purchase_mode",
    "next_custom_purchase_mode",
    "parse_custom_volume_gb",
    "custom_purchase_available",
    "custom_purchase_offered",
    "flat_plan_list_enabled",
    "plan_duration_label_fa",
    "flat_plan_dim_label",
    "flat_plan_button_label",
    "ladder_warnings",
    "package_has_time_limit",
    "package_hwid_limit",
    "category_volume_hwid_limit",
    "category_trial_shape",
    "category_reseller_trial",
    "TRIAL_GB_MAX",
    "TRIAL_DAYS_MAX",
    "hwid_limit_label_fa",
    "plan_order_mode",
    "sort_packages_for_display",
    "filter_ready_plans_by_time_limit",
    "ready_package_purchase_payload",
    "normalize_category_sales_flags",
    "set_package_has_time_limit",
]

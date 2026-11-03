"""Reseller prices as an admin sets them: one set of rules for the bot and the Mini App.

The bot's typed prompts and the panel's forms end in the same functions here, so the
parsing, the limits, the preview of a percent cut and the guard of a write cannot
drift apart. The pricing itself is untouched: ``services/reseller.py`` still owns what
a reseller is charged (``quote_ready_plan``, ``quote_custom_volume``).

Three groups of things live here:

* **typed numbers**: ``parse_reseller_input`` (the bot's «150,000» / «۱۵۰٬۰۰۰», bounded
  by the column, «۰» only where zero means something);
* **the percent cut**: which plans are on a screen (``screen_cells``), the preview of a
  cut (``preview_cut``, ``cut_preview_text``), applying exactly that set (``apply_cuts``)
  and taking the newest cut back (``undo_ops``, ``undo_screen_cut``);
* **the guard of a web write** (``guarded_reseller_write``): the form carries what it
  was drawn from (a row's ``updated_at``, ``none`` while there is no row, or the digest
  of a cut's preview), and a write against anything else is refused, never merged.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.texts import texts
from database.models import ResellerPlanPrice, ResellerPriceOp, ResellerRate, User
from services.reseller import (
    ResellerError,
    apply_percent_cut,
    get_rate,
    latest_price_op,
    list_plan_prices,
    percent_cut_preview,
    undo_flat_percent_cut,
    undo_percent_cut,
)
from services.store_admin import CatalogView
from services.store_catalog import flat_plan_list_enabled
from services.store_edit import parse_toman
from services.store_pricing import (
    DEFAULT_TIER,
    PREMIUM_TIER,
    category_durations,
    category_tiers,
    duration_from_package_options,
    get_price,
)
from services.ux_format import fa_toman, format_toman, to_persian_digits

_T = TypeVar("_T")
_P = TypeVar("_P")

CellKey = tuple[str, int | None, str]  # (plan slug, duration in days or None, tier)

NO_ROW_TOKEN = "none"
TEXT_STALE_RESELLER = (
    "قیمت‌های این نماینده در این فاصله عوض شد. چیزی ذخیره نشد؛ صفحه را دوباره باز کن."
)

# The bot's field keys of its rate prompts, and the column each one writes.
RATE_FIELD_ATTRS = {
    "gb": "price_per_gb_toman",
    "day": "price_per_day_toman",
    "quota": "trial_quota_monthly",
    "tprice": "trial_price_toman",
}
# ``trial_quota_monthly`` is a 32-bit ``Integer`` column; the others are ``BigInteger``.
QUOTA_MAX = 2**31 - 1

_ZERO_SPELLINGS = frozenset({"0", "۰"})


class StaleResellerSheet(ResellerError):
    """The reseller's prices changed since the form was drawn; nothing was written."""


class NotAReseller(ResellerError):
    """The person is not (or no longer) a reseller; there is no sheet to write."""


# --------------------------------------------------------------------------- #
# Typed numbers
# --------------------------------------------------------------------------- #


def invalid_number_line() -> str:
    """The bot's answer to a number it cannot read."""
    return texts.admin_store_number_invalid.format(example="1200")


def parse_reseller_amount(raw: str) -> int | None:
    """«150000», «150,000», «۱۵۰٬۰۰۰»; «۰» only as a lone zero; ``None`` for anything else.

    ``None`` also covers a number past SQLite's 64-bit integer and a digit such as «²»
    that ``str.isdigit()`` accepts and ``int()`` then refuses: both used to be an
    unhandled error out of the bot's prompt.
    """
    value = parse_toman(raw)
    if value is None:
        return None
    if value == 0 and raw.strip() not in _ZERO_SPELLINGS:
        return None
    return value


def parse_reseller_input(field: str, raw: str) -> int | None:
    """What the bot's number prompt reads for ``field`` (``plan`` or a ``RATE_FIELD_ATTRS`` key).

    A plan price of zero parses (``set_plan_price`` refuses it with its own line); a rate
    of zero is meaningful («این جزء رو حساب نکن»).
    """
    value = parse_reseller_amount(raw)
    if value is not None and field == "quota" and value > QUOTA_MAX:
        return None
    return value


# --------------------------------------------------------------------------- #
# Labels and the status block (the bot's keyboards and the page print the same words)
# --------------------------------------------------------------------------- #


def duration_tab_label(duration_days: int | None) -> str:
    """«۱ ماهه» / «بدون انقضا»: the duration tab of the reseller matrix."""
    if duration_days is None:
        return texts.purchase_duration_none
    months = max(1, int(duration_days) // 30)
    return texts.purchase_duration_month.format(n=to_persian_digits(months))


def tier_tab_label(tier: str) -> str:
    return texts.purchase_tier_premium if tier == PREMIUM_TIER else texts.purchase_tier_economy


async def status_block(session: AsyncSession, user_id: int, *, wallet_toman: int) -> str:
    """The shop at a glance, above its price sections (the bot's panel head)."""
    # Lazy: ``services.trial`` reaches ``bot.keyboards.admin``, which imports this module.
    from services.trial import get_reseller_trial_quota, reseller_month_sales

    rate = await get_rate(session, user_id)
    quota = await get_reseller_trial_quota(session, user_id)
    per_gb_set = bool(rate and int(rate.price_per_gb_toman or 0) > 0)
    lines = [
        texts.admin_reseller_status_month.format(
            sales=to_persian_digits(await reseller_month_sales(session, user_id)),
            used=to_persian_digits(quota.used),
            quota=to_persian_digits(quota.quota),
        ),
        texts.admin_reseller_status_wallet.format(wallet=fa_toman(int(wallet_toman or 0))),
    ]
    # The builder is offered to a shop only with a per-gigabyte rate
    # (`custom_purchase_offered`): say so, whatever the groups are set to.
    if not per_gb_set:
        lines.append(texts.admin_reseller_status_no_rate)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Which plans a screen holds
# --------------------------------------------------------------------------- #


def is_ready_plan(package: Any) -> bool:
    """Only ready plans can carry a reseller price; the custom-purchase
    template has no price of its own to override."""
    kind = getattr(package.kind, "value", package.kind)
    return str(kind) == "ready"


def screen_cells(
    view: CatalogView,
    *,
    category_id: int | None,
    duration: int | None,
    tier: str,
) -> tuple[bool, list[tuple[Any, int]]]:
    """Whether the matrix screen is a flat list, and its rows as ``(plan, retail)``.

    A percent cut and its undo are scoped to exactly these plans. Every group
    has a tab at a common duration, so the tab alone reaches other groups'
    agreed prices and other groups' undo snapshots.
    """
    if category_id is None:
        return False, []
    cat = view.category(int(category_id))
    if cat is None:
        return False, []
    flat = flat_plan_list_enabled(cat)
    prices = list(view.prices or [])
    rows: list[tuple[Any, int]] = []
    for pkg in view.packages_of(int(cat.id)):
        if not is_ready_plan(pkg):
            continue
        if flat:
            plan_dur = duration_from_package_options(dict(pkg.options or {}))
            cell = get_price(prices, slug=str(pkg.slug), duration_days=plan_dur, tier=DEFAULT_TIER)
            if cell is None:
                fixed = getattr(pkg, "fixed_price_toman", None)
                if fixed is None:
                    continue
                try:
                    cell = int(fixed)
                except (TypeError, ValueError):
                    continue
        else:
            cell = get_price(prices, slug=str(pkg.slug), duration_days=duration, tier=tier)
        if cell is not None:
            rows.append((pkg, int(cell)))
    return flat, rows


def encode_duration(duration_days: int | None) -> str:
    """A duration in a URL or a form: ``n`` for «بدون انقضا», else the day count."""
    return "n" if duration_days is None else str(int(duration_days))


def decode_duration(raw: str) -> int | None:
    """Inverse of :func:`encode_duration`; anything else is a ``ValueError``."""
    token = (raw or "").strip()
    if token == "n":
        return None
    if token.isascii() and token.isdigit() and len(token) < 10:
        return int(token)
    raise ValueError(f"invalid duration: {raw!r}")


def ready_categories(view: CatalogView) -> list[Any]:
    """The groups the matrix lists: those with at least one ready plan, in catalog order."""
    return [c for c in view.categories if any(is_ready_plan(p) for p in view.packages_of(c.id))]


def category_tabs(cat: Any) -> tuple[bool, list[int | None], list[str]]:
    """``(flat, durations, tiers)`` of a group. A flat list has no shelf tabs."""
    if flat_plan_list_enabled(cat):
        return True, [], []
    return False, category_durations(cat), category_tiers(cat)


def shown_cells(view: CatalogView) -> dict[CellKey, tuple[Any, int]]:
    """Every cell a reseller price can sit on today: ``(slug, duration, tier) -> (plan, retail)``.

    Built from ``screen_cells`` for each tab of each group, so a cell is on this list
    exactly when the matrix screen would draw it.
    """
    out: dict[CellKey, tuple[Any, int]] = {}
    for cat in ready_categories(view):
        flat, durations, tiers = category_tabs(cat)
        if flat:
            _flat, rows = screen_cells(view, category_id=cat.id, duration=None, tier=DEFAULT_TIER)
            for pkg, retail in rows:
                dur = duration_from_package_options(dict(pkg.options or {}))
                out[(str(pkg.slug), dur, DEFAULT_TIER)] = (pkg, retail)
            continue
        for dur in durations:
            for tier in tiers:
                _flat, rows = screen_cells(view, category_id=cat.id, duration=dur, tier=tier)
                for pkg, retail in rows:
                    out[(str(pkg.slug), dur, tier)] = (pkg, retail)
    return out


def tab_is_shown(cat: Any, duration: int | None, tier: str) -> bool:
    """Whether (duration, tier) is a tab of this group (a flat list has one, implicitly)."""
    flat, durations, tiers = category_tabs(cat)
    return flat or (duration in durations and tier in tiers)


# --------------------------------------------------------------------------- #
# The percent cut
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Cut:
    """One agreed price a cut would move."""

    duration_days: int | None
    slug: str
    before: int
    after: int


async def preview_cut(
    session: AsyncSession,
    user_id: int,
    view: CatalogView,
    *,
    category_id: int | None,
    duration: int | None,
    tier: str,
    pct: int,
) -> list[Cut]:
    """The prices ``pct`` percent off would move on this screen; nothing is written.

    Only cells that already have an agreed price, only the plans on the screen: other
    groups share this tab's duration.
    """
    flat, rows = screen_cells(view, category_id=category_id, duration=duration, tier=tier)
    cuts: list[Cut] = []
    if flat:
        by_duration: dict[int | None, set[str]] = {}
        for pkg, _retail in rows:
            dur = duration_from_package_options(dict(pkg.options or {}))
            by_duration.setdefault(dur, set()).add(str(pkg.slug))
        for dur in sorted(by_duration, key=lambda item: (item is None, item or 0)):
            agreed = await list_plan_prices(session, user_id, duration_days=dur, tier=DEFAULT_TIER)
            agreed = {s: p for s, p in agreed.items() if s in by_duration[dur]}
            for slug, before, after in percent_cut_preview(agreed, pct=pct):
                cuts.append(Cut(dur, slug, before, after))
        return cuts
    on_screen = {str(pkg.slug) for pkg, _retail in rows}
    agreed = await list_plan_prices(session, user_id, duration_days=duration, tier=tier)
    for slug, before, after in percent_cut_preview(
        {s: v for s, v in agreed.items() if s in on_screen}, pct=pct
    ):
        cuts.append(Cut(duration, slug, before, after))
    return cuts


def cut_lines(cuts: list[Cut], view: CatalogView) -> list[str]:
    """«پلن: قبل ← بعد», one per price the cut moves."""
    title_by_slug = {str(p.slug): str(p.title_fa) for p in view.packages}
    return [
        texts.admin_reseller_pct_preview_line.format(
            plan=title_by_slug.get(c.slug, c.slug),
            before=format_toman(c.before, persian_digits=True),
            after=format_toman(c.after, persian_digits=True),
        )
        for c in cuts
    ]


def cut_preview_text(cuts: list[Cut], view: CatalogView) -> str:
    return texts.admin_reseller_pct_preview.format(
        lines="\n".join(cut_lines(cuts, view)),
        count=to_persian_digits(len(cuts)),
    )


def cut_digest(cuts: list[Cut], pct: int) -> str:
    """A short fingerprint of a preview: the percent and every (duration, plan, old, new)."""
    body = json.dumps([int(pct), [[c.duration_days, c.slug, c.before, c.after] for c in cuts]])
    return hashlib.sha256(body.encode()).hexdigest()[:32]


async def apply_cuts(
    session: AsyncSession,
    user_id: int,
    *,
    flat: bool,
    tier: str,
    pct: int,
    cuts: list[Cut],
) -> int:
    """Write the cut and its undo snapshots. Returns the number of prices changed.

    One timestamp across durations is what lets undo take back this cut and nothing
    older. A flat list reads and writes the economy cell of each plan's own duration.
    """
    by_duration: dict[int | None, list[tuple[str, int, int]]] = {}
    for cut in cuts:
        by_duration.setdefault(cut.duration_days, []).append((cut.slug, cut.before, cut.after))
    cut_at = datetime.now(UTC)
    count = 0
    for dur, changes in by_duration.items():
        count += await apply_percent_cut(
            session,
            int(user_id),
            duration_days=dur,
            tier=DEFAULT_TIER if flat else tier,
            pct=int(pct),
            changes=changes,
            created_at=cut_at,
        )
    return count


async def undo_ops(
    session: AsyncSession,
    user_id: int,
    *,
    flat: bool,
    rows: list[tuple[Any, int]],
    duration: int | None,
    tier: str,
) -> list[ResellerPriceOp]:
    """The newest undo snapshot of each tab this screen spans, made of this screen's plans."""
    on_screen = {str(pkg.slug) for pkg, _retail in rows}
    if flat:
        tabs = {duration_from_package_options(dict(pkg.options or {})) for pkg, _r in rows}
        undo_tier = DEFAULT_TIER
    else:
        tabs = {duration}
        undo_tier = tier
    found: list[ResellerPriceOp] = []
    for days in sorted(tabs, key=lambda item: (item is None, item or 0)):
        op = await latest_price_op(
            session, user_id, duration_days=days, tier=undo_tier, slugs=on_screen
        )
        if op is not None:
            found.append(op)
    return found


async def undo_screen_cut(
    session: AsyncSession,
    user_id: int,
    *,
    flat: bool,
    rows: list[tuple[Any, int]],
    duration: int | None,
    tier: str,
) -> int | None:
    """Take back the newest cut on this screen; ``None`` when there is nothing to undo."""
    on_screen = {str(pkg.slug) for pkg, _retail in rows}
    if flat:
        return await undo_flat_percent_cut(
            session,
            int(user_id),
            durations=[duration_from_package_options(dict(p.options or {})) for p, _r in rows],
            tier=DEFAULT_TIER,
            slugs=on_screen,
        )
    return await undo_percent_cut(
        session, int(user_id), duration_days=duration, tier=tier, slugs=on_screen
    )


def undo_token(ops: list[ResellerPriceOp]) -> str:
    """What an undo form is drawn from: the snapshots it would look at. Empty: nothing to undo."""
    return ",".join(str(op.id) for op in ops)


# --------------------------------------------------------------------------- #
# What a form was drawn from
# --------------------------------------------------------------------------- #


def row_token(row: ResellerPlanPrice | ResellerRate | None) -> str:
    """A row's last change, or ``none`` while there is no row."""
    if row is None:
        return NO_ROW_TOKEN
    return row.updated_at.isoformat()


async def plan_price_rows(session: AsyncSession, user_id: int) -> dict[CellKey, ResellerPlanPrice]:
    """Every stored plan price of one reseller, by (slug, duration, tier)."""
    result = await session.execute(
        select(ResellerPlanPrice).where(ResellerPlanPrice.user_id == user_id)
    )
    return {
        (str(r.package_slug), None if r.duration_days is None else int(r.duration_days), r.tier): r
        for r in result.scalars()
    }


async def cell_token(session: AsyncSession, user_id: int, key: CellKey) -> str:
    return row_token((await plan_price_rows(session, user_id)).get(key))


async def rate_token(session: AsyncSession, user_id: int) -> str:
    return row_token(await get_rate(session, user_id))


async def guarded_reseller_write(
    session: AsyncSession,
    user_id: int,
    *,
    seen: str | None,
    current: Callable[[AsyncSession], Awaitable[tuple[str, _P]]],
    apply: Callable[[AsyncSession, _P], Awaitable[_T]],
) -> _T:
    """Every panel write to one reseller's prices: the form's token only, nothing merged.

    ``current`` reads the token (and whatever ``apply`` needs) inside the transaction that
    writes; a form whose ``seen`` is not that token is refused. The reseller's user row is
    locked first (``FOR UPDATE`` on a server database), so two writers for one shop run
    one after the other and the second sees the first's change. SQLite's «database is
    locked» (a writer whose snapshot another commit has passed) and a lost race to insert
    the same row map to the same refusal: both mean this check lost.
    """
    try:
        user = await session.get(User, int(user_id), with_for_update=True)
        if user is None or not user.is_reseller:
            raise NotAReseller(texts.admin_customer_not_found)
        token, payload = await current(session)
        if seen is None or token != seen:
            raise StaleResellerSheet(TEXT_STALE_RESELLER)
        result = await apply(session, payload)
        await session.commit()
        return result
    except ResellerError:
        await session.rollback()
        raise
    except (OperationalError, IntegrityError) as exc:
        await session.rollback()
        if isinstance(exc, IntegrityError) or "locked" in str(exc).lower():
            raise StaleResellerSheet(TEXT_STALE_RESELLER) from exc
        raise

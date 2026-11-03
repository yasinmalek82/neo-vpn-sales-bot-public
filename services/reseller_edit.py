"""The Mini App's reseller writes: each is the bot's own call, behind the form's token.

Any admin may do all of this, as in the bot (``_deny_if_not_admin`` is the only check
its reseller controls make), so unlike the store's writes there is no owner check here.
Every write goes through ``guarded_reseller_write``: the form carries what it was drawn
from, the token is read again inside the transaction that writes, and a form drawn from
anything else is refused (``StaleResellerSheet``) and never merged.

* a plan's price and its clear: the cell's row (``updated_at``, ``none`` before a first
  price), through ``set_plan_price`` / ``clear_plan_price``;
* a rate figure: the shop's rate row, through ``set_rate``;
* a percent cut: asks first (``plan_cut`` writes nothing); the second request carries the
  digest of the preview it was shown and applies that set or nothing (``apply_planned_cut``);
* its undo: the snapshots it would look at (``undo_cut``).

Nothing here sends anything or writes a report: the bot's reseller controls do not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from bot.texts import texts
from database.models import User
from services.reseller import (
    ResellerError,
    clear_plan_price,
    parse_percent_input,
    set_plan_price,
    set_rate,
)
from services.reseller_admin import (
    RATE_FIELD_ATTRS,
    TEXT_STALE_RESELLER,
    CellKey,
    Cut,
    NotAReseller,
    StaleResellerSheet,
    apply_cuts,
    cell_token,
    cut_digest,
    cut_preview_text,
    guarded_reseller_write,
    invalid_number_line,
    parse_reseller_input,
    preview_cut,
    rate_token,
    ready_categories,
    screen_cells,
    shown_cells,
    tab_is_shown,
    undo_ops,
    undo_screen_cut,
    undo_token,
)
from services.store_admin import CatalogView, get_catalog_view
from services.ux_format import to_persian_digits


class NothingToUndo(ResellerError):
    """There is no cut on this screen to take back (or it was already taken back)."""


def _stale() -> StaleResellerSheet:
    return StaleResellerSheet(TEXT_STALE_RESELLER)


async def require_reseller(session: AsyncSession, user_id: int) -> None:
    user = await session.get(User, int(user_id))
    if user is None or not user.is_reseller:
        raise NotAReseller(texts.admin_customer_not_found)


# --------------------------------------------------------------------------- #
# One plan's price
# --------------------------------------------------------------------------- #


async def edit_plan_price(
    session: AsyncSession,
    user_id: int,
    *,
    slug: str,
    duration: int | None,
    tier: str,
    raw: str,
    seen: str | None,
) -> str:
    """Set the agreed price of one cell. Returns the line to show.

    The cell must be one the matrix draws today (a forged duration or tier, or a plan
    that left the catalog, is a stale form). A flat list's cell is its plan's own
    duration at the economy tier, as the bot forces it.
    """
    value = parse_reseller_input("plan", raw)
    if value is None:
        raise ResellerError(invalid_number_line())
    key: CellKey = (slug, duration, tier)

    async def current(s: AsyncSession) -> tuple[str, None]:
        if key not in shown_cells(await get_catalog_view(s)):
            raise _stale()
        return await cell_token(s, user_id, key), None

    async def apply(s: AsyncSession, _payload: None) -> None:
        await set_plan_price(s, user_id, slug, value, duration_days=duration, tier=tier)

    await guarded_reseller_write(session, user_id, seen=seen, current=current, apply=apply)
    return texts.admin_store_saved


async def clear_cell_price(
    session: AsyncSession,
    user_id: int,
    *,
    slug: str,
    duration: int | None,
    tier: str,
    seen: str | None,
) -> str:
    """Back to retail for one cell («↩️»)."""
    key: CellKey = (slug, duration, tier)

    async def current(s: AsyncSession) -> tuple[str, None]:
        if key not in shown_cells(await get_catalog_view(s)):
            raise _stale()
        return await cell_token(s, user_id, key), None

    async def apply(s: AsyncSession, _payload: None) -> None:
        await clear_plan_price(s, user_id, slug, duration_days=duration, tier=tier)

    await guarded_reseller_write(session, user_id, seen=seen, current=current, apply=apply)
    return texts.admin_reseller_price_cleared


# --------------------------------------------------------------------------- #
# The rate card and the trial figures
# --------------------------------------------------------------------------- #


async def edit_rate_field(
    session: AsyncSession,
    user_id: int,
    *,
    field: str,
    raw: str,
    seen: str | None,
) -> str:
    """Set one of toman/GB, toman/day, the monthly trial quota, the extra trial's price."""
    if field not in RATE_FIELD_ATTRS:
        raise ResellerError(texts.admin_panel_invalid)
    value = parse_reseller_input(field, raw)
    if value is None:
        raise ResellerError(invalid_number_line())

    async def current(s: AsyncSession) -> tuple[str, None]:
        return await rate_token(s, user_id), None

    async def apply(s: AsyncSession, _payload: None) -> None:
        await set_rate(s, user_id, **{RATE_FIELD_ATTRS[field]: value})

    await guarded_reseller_write(session, user_id, seen=seen, current=current, apply=apply)
    return texts.admin_store_saved


# --------------------------------------------------------------------------- #
# The percent cut and its undo
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CutPlan:
    """What the confirm page shows, and the digest its second request must carry."""

    pct: int
    text: str
    count: int
    digest: str


def _category(view: CatalogView, slug: str, duration: int | None, tier: str) -> Any:
    """The group a cut is on, only if it has that tab: anything else is a stale form."""
    cat = next((c for c in ready_categories(view) if str(c.slug) == slug), None)
    if cat is None or not tab_is_shown(cat, duration, tier):
        raise _stale()
    return cat


async def plan_cut(
    session: AsyncSession,
    user_id: int,
    *,
    category: str,
    duration: int | None,
    tier: str,
    raw_pct: str,
) -> CutPlan:
    """The preview of ``raw_pct`` percent off this tab, as the bot words it. Writes nothing."""
    await require_reseller(session, user_id)
    pct = parse_percent_input(raw_pct)
    if pct is None:
        raise ResellerError(texts.admin_reseller_pct_invalid)
    view = await get_catalog_view(session)
    cat = _category(view, category, duration, tier)
    cuts = await preview_cut(
        session, user_id, view, category_id=cat.id, duration=duration, tier=tier, pct=pct
    )
    if not cuts:
        raise ResellerError(texts.admin_reseller_pct_nothing)
    return CutPlan(
        pct=pct,
        text=cut_preview_text(cuts, view),
        count=len(cuts),
        digest=cut_digest(cuts, pct),
    )


async def apply_planned_cut(
    session: AsyncSession,
    user_id: int,
    *,
    category: str,
    duration: int | None,
    tier: str,
    raw_pct: str,
    seen: str | None,
) -> str:
    """Apply the cut whose preview the page showed. If any price of that set moved since
    (or the set is no longer the same), nothing is written: a different set is never applied.
    """
    pct = parse_percent_input(raw_pct)
    if pct is None:
        raise ResellerError(texts.admin_reseller_pct_invalid)

    async def current(s: AsyncSession) -> tuple[str, tuple[bool, list[Cut]]]:
        view = await get_catalog_view(s)
        cat = _category(view, category, duration, tier)
        cuts = await preview_cut(
            s, user_id, view, category_id=cat.id, duration=duration, tier=tier, pct=pct
        )
        flat, _rows = screen_cells(view, category_id=cat.id, duration=duration, tier=tier)
        return cut_digest(cuts, pct), (flat, cuts)

    async def apply(s: AsyncSession, payload: tuple[bool, list[Cut]]) -> int:
        flat, cuts = payload
        if not cuts:
            raise _stale()
        return await apply_cuts(s, user_id, flat=flat, tier=tier, pct=pct, cuts=cuts)

    count = await guarded_reseller_write(session, user_id, seen=seen, current=current, apply=apply)
    return texts.admin_reseller_pct_done.format(count=to_persian_digits(count))


async def undo_cut(
    session: AsyncSession,
    user_id: int,
    *,
    category: str,
    duration: int | None,
    tier: str,
    seen: str | None,
) -> str:
    """Take back the newest cut on this tab, if the snapshots are the ones the page saw."""

    async def current(s: AsyncSession) -> tuple[str, tuple[bool, list[tuple[Any, int]]]]:
        view = await get_catalog_view(s)
        cat = _category(view, category, duration, tier)
        flat, rows = screen_cells(view, category_id=cat.id, duration=duration, tier=tier)
        ops = await undo_ops(s, user_id, flat=flat, rows=rows, duration=duration, tier=tier)
        if not ops:
            raise NothingToUndo(texts.admin_reseller_nothing_to_undo)
        return undo_token(ops), (flat, rows)

    async def apply(s: AsyncSession, payload: tuple[bool, list[tuple[Any, int]]]) -> int:
        flat, rows = payload
        restored = await undo_screen_cut(
            s, user_id, flat=flat, rows=rows, duration=duration, tier=tier
        )
        if restored is None:
            raise NothingToUndo(texts.admin_reseller_nothing_to_undo)
        return restored

    restored = await guarded_reseller_write(
        session, user_id, seen=seen, current=current, apply=apply
    )
    return texts.admin_reseller_undo_done.format(count=to_persian_digits(restored))


__all__ = [
    "CutPlan",
    "NotAReseller",
    "NothingToUndo",
    "StaleResellerSheet",
    "apply_planned_cut",
    "clear_cell_price",
    "edit_plan_price",
    "edit_rate_field",
    "plan_cut",
    "require_reseller",
    "undo_cut",
]

"""What the Mini App shows about the resellers: the list and one reseller's price sheet.

Read only. Every figure comes from a function the bot's reseller screens call too: the
Jalali-month counts from ``services/trial.py``, the rate from ``get_rate``, the agreed
prices from the stored rows, the retail of a cell from ``screen_cells`` (the matrix
screen's own rows), and what a reseller would actually be charged from
``quote_ready_plan``. The ⚠️ beside a cell, an agreed price above that cell's retail
price, is the quote's ``crossed_over``: this module decides nothing about prices.

A plan is identified by its slug. A stored price whose plan, duration or tier no longer
has a cell in the catalog is listed as such (``orphans``), not hidden.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ResellerPlanPrice, User
from services.customer_card import customer_name
from services.reseller import get_rate, has_per_gb_rate, quote_ready_plan
from services.reseller_admin import (
    CellKey,
    category_tabs,
    decode_duration,
    duration_tab_label,
    encode_duration,
    plan_price_rows,
    ready_categories,
    row_token,
    screen_cells,
    shown_cells,
    status_block,
    tier_tab_label,
    undo_ops,
    undo_token,
)
from services.store_admin import get_catalog_view
from services.store_pricing import DEFAULT_TIER, duration_from_package_options
from services.trial import get_reseller_trial_quota, reseller_month_sales

PAGE_SIZE = 30

TEXT_CROSSED = "⚠️ بالاتر از قیمت خرده است؛ این نماینده همین‌جا با قیمت خرده حساب می‌شود."


@dataclass(frozen=True)
class ResellerRow:
    user_id: int
    name: str
    handle: str
    rate_set: bool
    priced_count: int
    sales: int
    trials_used: int
    trial_quota: int


@dataclass(frozen=True)
class ResellerPage:
    rows: list[ResellerRow]
    total: int
    offset: int

    @property
    def has_prev(self) -> bool:
        return self.offset > 0

    @property
    def has_next(self) -> bool:
        return self.offset + len(self.rows) < self.total

    @property
    def prev_offset(self) -> int:
        return max(0, self.offset - PAGE_SIZE)

    @property
    def next_offset(self) -> int:
        return self.offset + PAGE_SIZE


async def list_resellers(session: AsyncSession, *, offset: int = 0) -> ResellerPage:
    """The resellers, oldest first, one page. Counts are this Jalali month's, as in the bot."""
    total = int(
        await session.scalar(
            select(func.count()).select_from(User).where(User.is_reseller.is_(True))
        )
        or 0
    )
    result = await session.execute(
        select(User)
        .where(User.is_reseller.is_(True))
        .order_by(User.id)
        .offset(max(0, int(offset)))
        .limit(PAGE_SIZE)
    )
    rows: list[ResellerRow] = []
    for shop in result.scalars().all():
        quota = await get_reseller_trial_quota(session, shop.id)
        priced = int(
            await session.scalar(
                select(func.count())
                .select_from(ResellerPlanPrice)
                .where(ResellerPlanPrice.user_id == shop.id)
            )
            or 0
        )
        rows.append(
            ResellerRow(
                user_id=int(shop.id),
                name=customer_name(shop),
                handle=f"@{shop.username}" if shop.username else "—",
                rate_set=await has_per_gb_rate(session, shop.id),
                priced_count=priced,
                sales=await reseller_month_sales(session, shop.id),
                trials_used=quota.used,
                trial_quota=quota.quota,
            )
        )
    return ResellerPage(rows=rows, total=total, offset=max(0, int(offset)))


@dataclass(frozen=True)
class Tab:
    value: str
    label: str
    selected: bool


@dataclass(frozen=True)
class CellView:
    slug: str
    title: str
    duration: str  # encoded, as the form carries it
    tier: str
    retail: int
    agreed: int | None
    charged: int | None
    crossed: bool
    token: str  # what a form for this cell is drawn from


@dataclass(frozen=True)
class OrphanView:
    slug: str
    title: str | None  # None: the plan itself is gone from the catalog
    duration: str
    tier: str
    price: int


@dataclass(frozen=True)
class UndoView:
    token: str
    when: datetime


@dataclass(frozen=True)
class SheetView:
    user_id: int
    name: str
    handle: str
    status: str
    per_gb: int
    per_day: int
    trial_quota: int
    trial_price: int
    rate_token: str
    categories: list[Tab]
    category_slug: str
    flat: bool
    durations: list[Tab]
    tiers: list[Tab]
    duration: str  # the selected tab, encoded
    tier: str
    cells: list[CellView]
    orphans: list[OrphanView]
    undo: UndoView | None
    has_plans: bool


async def build_sheet(
    session: AsyncSession,
    user_id: int,
    *,
    category_slug: str | None,
    duration: str | None,
    tier: str | None,
) -> SheetView | None:
    """One reseller's sheet, or ``None`` when the person is not a reseller (a 404 page)."""
    user = await session.get(User, int(user_id))
    if user is None or not user.is_reseller:
        return None
    view = await get_catalog_view(session)
    rate = await get_rate(session, user.id)
    price_rows = await plan_price_rows(session, user.id)

    cats = ready_categories(view)
    cat = next((c for c in cats if c.slug == category_slug), cats[0] if cats else None)
    flat = False
    durations: list[Tab] = []
    tiers: list[Tab] = []
    sel_duration: int | None = None
    sel_tier = DEFAULT_TIER
    cells: list[CellView] = []
    undo: UndoView | None = None
    if cat is not None:
        flat, durs, tier_names = category_tabs(cat)
        if not flat:
            try:
                wanted = decode_duration(duration or "")
            except ValueError:
                wanted = durs[0]
            sel_duration = wanted if wanted in durs else durs[0]
            sel_tier = tier if tier in tier_names else tier_names[0]
            durations = [
                Tab(encode_duration(d), duration_tab_label(d), d == sel_duration) for d in durs
            ]
            if len(tier_names) > 1:
                tiers = [Tab(t, tier_tab_label(t), t == sel_tier) for t in tier_names]
        _flat, rows = screen_cells(view, category_id=cat.id, duration=sel_duration, tier=sel_tier)
        for pkg, retail in rows:
            cell_dur = (
                duration_from_package_options(dict(pkg.options or {})) if flat else sel_duration
            )
            cell_tier = DEFAULT_TIER if flat else sel_tier
            stored = price_rows.get((str(pkg.slug), cell_dur, cell_tier))
            quote = None
            if stored is not None:
                quote = await quote_ready_plan(
                    session,
                    user_id=user.id,
                    package_slug=str(pkg.slug),
                    retail_price_toman=retail,
                    duration_days=cell_dur,
                    tier=cell_tier,
                )
            cells.append(
                CellView(
                    slug=str(pkg.slug),
                    title=str(pkg.title_fa),
                    duration=encode_duration(cell_dur),
                    tier=cell_tier,
                    retail=retail,
                    agreed=int(stored.price_toman) if stored is not None else None,
                    charged=quote.price_toman if quote is not None else None,
                    crossed=bool(quote is not None and quote.crossed_over),
                    token=row_token(stored),
                )
            )
        ops = await undo_ops(
            session, user.id, flat=flat, rows=rows, duration=sel_duration, tier=sel_tier
        )
        if ops:
            undo = UndoView(token=undo_token(ops), when=max(op.created_at for op in ops))

    shown: set[CellKey] = set(shown_cells(view))
    title_of = {str(p.slug): str(p.title_fa) for p in view.packages}
    orphans = [
        OrphanView(
            slug=key[0],
            title=title_of.get(key[0]),
            duration=duration_tab_label(key[1]),
            tier=tier_tab_label(key[2]),
            price=int(row.price_toman),
        )
        for key, row in sorted(price_rows.items(), key=lambda item: (item[0][0], str(item[0][1])))
        if key not in shown
    ]
    return SheetView(
        user_id=int(user.id),
        name=customer_name(user),
        handle=f"@{user.username}" if user.username else "—",
        status=await status_block(
            session, user.id, wallet_toman=int(user.wallet_balance_toman or 0)
        ),
        per_gb=int(getattr(rate, "price_per_gb_toman", 0) or 0),
        per_day=int(getattr(rate, "price_per_day_toman", 0) or 0),
        trial_quota=int(getattr(rate, "trial_quota_monthly", 0) or 0),
        trial_price=int(getattr(rate, "trial_price_toman", 0) or 0),
        rate_token=row_token(rate),
        categories=[
            Tab(str(c.slug), str(c.title_fa), cat is not None and c.id == cat.id) for c in cats
        ],
        category_slug=str(cat.slug) if cat is not None else "",
        flat=flat,
        durations=durations,
        tiers=tiers,
        duration=encode_duration(sel_duration),
        tier=sel_tier,
        cells=cells,
        orphans=orphans,
        undo=undo,
        has_plans=bool(cats),
    )

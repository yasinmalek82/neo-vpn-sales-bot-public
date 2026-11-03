"""Brand facts: the numbers and contact points customer copy is allowed to claim.

``BRAND.md`` rule 2 is «عدد به جای صفت» — a number instead of an adjective, and
«اگه عدد نداری، اون ادعا رو ننویس»: if you don't have the number, don't make the
claim. That rule only works if the numbers are *true at read time*, so none of
them are hardcoded in the Persian strings:

- **Catalog numbers** (cheapest price, how many groups, how many plans) are read
  from the published store version on every call. Change a price in the admin
  panel and the sentence that quotes it changes with it.
- **Contact points** (support handle, support hours, channel) come from env, per
  ``PROJECT_SPEC.md §6``: no commercial configuration in source.

Every field degrades to empty/``None`` rather than to a placeholder. A caller that
gets ``None`` must drop the whole claim — printing «از تومان شروع می‌شه» or a bare
«@» is worse than saying nothing, and is exactly the unprovable claim BRAND.md's
checklist rejects.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from database.models.store import StoreCategory, StorePackage
from services.store_config import get_published_store_config
from services.store_pricing import (
    DEFAULT_TIER,
    category_durations,
    get_price,
    is_buyable,
)


def normalize_handle(value: str) -> str:
    """``neo_network_support`` / ``@neo_network_support`` → ``@neo_network_support``.

    Empty stays empty: an unset handle must not render as a lone ``@``.
    """
    handle = (value or "").strip().lstrip("@").strip()
    return f"@{handle}" if handle else ""


def strip_handle(value: str) -> str:
    """Handle without the ``@`` — for ``t.me/<username>`` style links."""
    return (value or "").strip().lstrip("@").strip()


@dataclass(frozen=True)
class CatalogFacts:
    """What the live published catalog lets us claim.

    ``group_count`` counts **product groups**, not locations. BRAND.md's ad copy
    says «[تعداد] لوکیشن», but this project has no location field at all — the
    server group is decided from the product group at purchase time. Quoting a
    group count as a location count would be the unprovable claim BRAND.md
    forbids, so the honest number is exposed under an honest name and Phase 3's
    copy must not relabel it.
    """

    cheapest_price_toman: int | None
    plan_count: int
    group_count: int

    @property
    def has_any_plan(self) -> bool:
        return self.plan_count > 0


def _category_is_live(category: StoreCategory, by_id: dict[int, StoreCategory]) -> bool:
    """Enabled, and not hidden behind a disabled parent.

    Groups nest one level, so a disabled «حجمی» takes «ویژه» off the shelf with
    it even though the child's own ``enabled`` is still true.
    """
    if not category.enabled:
        return False
    parent_id = category.parent_id
    if parent_id is None:
        return True
    parent = by_id.get(parent_id)
    return parent is not None and parent.enabled


def _is_buyable(package: StorePackage, prices: list) -> bool:
    """A plan a customer can actually pay a known price for."""
    return is_buyable(package, prices, tier=DEFAULT_TIER)


async def get_catalog_facts(session: AsyncSession) -> CatalogFacts:
    """Derive the quotable numbers from the **published** catalog.

    Published, never the draft: a half-finished price the operator is still
    editing must not reach an ad. Nothing published yet → all-zero facts, and
    every caller drops its claim.
    """
    config = await get_published_store_config(session)
    if config is None:
        return CatalogFacts(cheapest_price_toman=None, plan_count=0, group_count=0)

    by_id = {c.id: c for c in config.categories}
    live_category_ids = {c.id for c in config.categories if _category_is_live(c, by_id)}

    matrix = list(config.prices)
    buyable = [
        p for p in config.packages if p.category_id in live_category_ids and _is_buyable(p, matrix)
    ]
    by_cat = {c.id: c for c in config.categories}
    prices: list[int] = []
    for p in buyable:
        cat = by_cat.get(p.category_id)
        durs = category_durations(cat)
        first = durs[0] if durs else 30
        cell = get_price(matrix, slug=str(p.slug), duration_days=first, tier=DEFAULT_TIER)
        if cell is not None and cell > 0:
            prices.append(int(cell))

    # Only groups that lead somewhere: an empty group is not a shelf.
    groups_with_stock = {p.category_id for p in buyable}
    top_level_with_stock = set()
    for category_id in groups_with_stock:
        category = by_id.get(category_id)
        if category is None:
            continue
        top_level_with_stock.add(category.parent_id or category.id)

    return CatalogFacts(
        cheapest_price_toman=min(prices) if prices else None,
        plan_count=len(buyable),
        group_count=len(top_level_with_stock),
    )


@dataclass(frozen=True)
class BrandFacts:
    """Everything customer copy may quote, in one place.

    Handles are normalized with a leading ``@``; empty means «not configured»,
    and the caller drops that line.
    """

    support_handle: str
    support_hours: str
    channel_handle: str
    catalog: CatalogFacts

    @property
    def has_support(self) -> bool:
        return bool(self.support_handle)

    @property
    def has_support_hours(self) -> bool:
        return bool(self.support_hours)

    @property
    def has_channel(self) -> bool:
        return bool(self.channel_handle)


def get_contact_facts(settings: Settings) -> BrandFacts:
    """Contact points only — no database round-trip.

    Used by copy that needs the support handle but not a price.
    """
    return BrandFacts(
        support_handle=normalize_handle(settings.support_handle),
        support_hours=(settings.support_hours_fa or "").strip(),
        channel_handle=normalize_handle(settings.channel_handle),
        catalog=CatalogFacts(cheapest_price_toman=None, plan_count=0, group_count=0),
    )


async def get_brand_facts(session: AsyncSession, settings: Settings) -> BrandFacts:
    """Contact points plus live catalog numbers."""
    contacts = get_contact_facts(settings)
    return BrandFacts(
        support_handle=contacts.support_handle,
        support_hours=contacts.support_hours,
        channel_handle=contacts.channel_handle,
        catalog=await get_catalog_facts(session),
    )


def support_line(facts: BrandFacts) -> str:
    """One line pointing at a human, or nothing at all.

    Until Phase 2 the bot never gave a customer any way to reach support: the
    «پشتیبانی» screen told them to message «ادمین» without naming one. An empty
    handle still returns "" rather than a placeholder, so an unconfigured
    deployment simply says less instead of saying something useless.
    """
    if not facts.support_handle:
        return ""
    if facts.support_hours:
        return f"پشتیبانی: {facts.support_handle} ({facts.support_hours})"
    return f"پشتیبانی: {facts.support_handle}"


def with_support(body: str, settings: Settings) -> str:
    """Append the support line to ``body`` when one is configured."""
    line = support_line(get_contact_facts(settings))
    return f"{body}\n\n{line}" if line else body


async def resolve_bot_username(bot: Bot, settings: Settings) -> str:
    """The bot's own ``@username``, without the ``@``.

    ``BOT_USERNAME`` is an override, not a requirement: when it is empty — which
    it is on this deployment — Telegram is asked instead, so the referral link
    and the BRAND.md channel signature keep working with no config at all.
    """
    configured = strip_handle(settings.bot_username)
    if configured:
        return configured
    me = await bot.get_me()
    return (me.username or "").strip()


# --------------------------------------------------------------------------- #
# The closing signature (BRAND.md §4)
# --------------------------------------------------------------------------- #

SIGNATURE_BRAND_FA = "نئو نتورک"
SIGNATURE_RULE = "─────"


def brand_signature(bot_username: str) -> str:
    """BRAND.md §4's closing block, or nothing when the handle is unknown.

    «پست‌های فوروارد شده منبعشون گم نمی‌شه» — a forwarded message loses its
    sender, so anything meant to travel carries the brand and the bot handle at
    the end. BRAND.md scopes this to channel posts; in this bot the equivalent
    is a **broadcast**, which is the one message type a customer forwards.

    Deliberately *not* on the referral pitch: that text ends with the referrer's
    own `?start=<code>` link, and a bare `@handle` underneath it would hand the
    reader an unattributed way in — the referrer loses the credit the message
    exists to earn.
    """
    handle = strip_handle(bot_username)
    if not handle:
        return ""
    return f"{SIGNATURE_RULE}\n{SIGNATURE_BRAND_FA} · @{handle}"


def with_signature(body: str, bot_username: str) -> str:
    """Append the signature once.

    Idempotent on purpose: the owner edits and resubmits a draft, and an
    announcement is re-previewed before it is confirmed. A second copy of the
    block is the kind of sloppiness the signature is supposed to prevent.
    """
    signature = brand_signature(bot_username)
    if not signature or signature in body:
        return body
    return f"{body.rstrip()}\n\n{signature}"

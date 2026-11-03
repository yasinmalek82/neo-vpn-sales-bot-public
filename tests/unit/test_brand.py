"""Brand facts must be true at read time, or absent.

``BRAND.md`` rule 2 allows a number instead of an adjective, and forbids the
claim outright when the number is missing. That makes «absent» a real answer
here, not an error case: every test below that expects ``None`` or ``0`` is
checking that the bot stays quiet rather than quoting a price that nobody can
actually pay.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from bot.config import Settings
from database.base import Base
from database.models.store import (
    StoreCategory,
    StorePackage,
    StorePackageKind,
    StorePackagePrice,
    StoreVersion,
    StoreVersionStatus,
)
from services.brand import (
    get_brand_facts,
    get_catalog_facts,
    get_contact_facts,
    normalize_handle,
    resolve_bot_username,
    strip_handle,
)


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _version(
    session: AsyncSession, *, status: StoreVersionStatus, number: int = 1
) -> StoreVersion:
    version = StoreVersion(version=number, status=status, pricing_document={"currency": "toman"})
    session.add(version)
    await session.flush()
    return version


async def _category(
    session: AsyncSession,
    version: StoreVersion,
    *,
    slug: str,
    enabled: bool = True,
    parent: StoreCategory | None = None,
) -> StoreCategory:
    category = StoreCategory(
        store_version_id=version.id,
        parent_id=parent.id if parent is not None else None,
        slug=slug,
        title_fa=slug,
        enabled=enabled,
        sales_flags={"durations": [30], "tiers_enabled": False},
    )
    session.add(category)
    await session.flush()
    return category


async def _package(
    session: AsyncSession,
    version: StoreVersion,
    category: StoreCategory,
    *,
    slug: str,
    price: int | None = 150_000,
    kind: StorePackageKind = StorePackageKind.ready,
    enabled: bool = True,
    allow_new_sale: bool = True,
) -> StorePackage:
    options = {"volume_gb": 50, "duration_days": 30}
    package = StorePackage(
        store_version_id=version.id,
        category_id=category.id,
        slug=slug,
        kind=kind,
        title_fa=slug,
        enabled=enabled,
        allow_new_sale=allow_new_sale,
        fixed_price_toman=price,
        options=options,
    )
    session.add(package)
    await session.flush()
    if kind is StorePackageKind.ready and price is not None:
        session.add(
            StorePackagePrice(
                store_version_id=version.id,
                package_slug=slug,
                duration_days=30,
                tier="economy",
                price_toman=int(price),
            )
        )
        await session.flush()
    return package


# --- handles ---------------------------------------------------------------


def test_a_handle_is_normalized_whether_or_not_it_was_typed_with_an_at() -> None:
    assert normalize_handle("neo_network_support") == "@neo_network_support"
    assert normalize_handle("@neo_network_support") == "@neo_network_support"
    assert normalize_handle("  @neo_network_support  ") == "@neo_network_support"


def test_an_unset_handle_stays_empty_instead_of_becoming_a_lone_at() -> None:
    """«پشتیبانی: @» is worse than no support line at all."""
    assert normalize_handle("") == ""
    assert normalize_handle("   ") == ""
    assert normalize_handle("@") == ""


def test_strip_handle_gives_the_bare_username_for_a_t_me_link() -> None:
    assert strip_handle("@neo_network_channel") == "neo_network_channel"
    assert strip_handle("") == ""


def test_contact_facts_report_what_is_missing() -> None:
    configured = get_contact_facts(
        Settings(BOT_TOKEN="t", SUPPORT_HANDLE="@s", SUPPORT_HOURS_FA="۹ تا ۱۲", CHANNEL_HANDLE="c")
    )
    assert configured.has_support and configured.has_support_hours and configured.has_channel
    assert configured.channel_handle == "@c"

    blank = get_contact_facts(Settings(BOT_TOKEN="t", SUPPORT_HANDLE="", SUPPORT_HOURS_FA=""))
    assert not blank.has_support
    assert not blank.has_support_hours


# --- catalog numbers -------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unpublished_store_supports_no_claim_at_all(session: AsyncSession) -> None:
    facts = await get_catalog_facts(session)
    assert facts.cheapest_price_toman is None
    assert facts.plan_count == 0
    assert facts.group_count == 0
    assert not facts.has_any_plan


@pytest.mark.asyncio
async def test_the_cheapest_price_is_the_lowest_a_customer_can_actually_pay(
    session: AsyncSession,
) -> None:
    version = await _version(session, status=StoreVersionStatus.published)
    economy = await _category(session, version, slug="economy")
    await _package(session, version, economy, slug="a", price=150_000)
    await _package(session, version, economy, slug="b", price=90_000)
    await session.commit()

    facts = await get_catalog_facts(session)
    assert facts.cheapest_price_toman == 90_000
    assert facts.plan_count == 2


@pytest.mark.asyncio
async def test_a_custom_template_is_not_quoted_because_it_has_no_fixed_price(
    session: AsyncSession,
) -> None:
    """Its price comes from the dynamic builder, so there is no number to quote."""
    version = await _version(session, status=StoreVersionStatus.published)
    category = await _category(session, version, slug="volume")
    await _package(
        session,
        version,
        category,
        slug="custom",
        price=None,
        kind=StorePackageKind.custom_template,
    )
    await session.commit()

    facts = await get_catalog_facts(session)
    assert facts.cheapest_price_toman is None
    assert facts.plan_count == 0


@pytest.mark.asyncio
async def test_a_disabled_or_unsellable_plan_is_not_counted(session: AsyncSession) -> None:
    version = await _version(session, status=StoreVersionStatus.published)
    category = await _category(session, version, slug="volume")
    await _package(session, version, category, slug="off", price=10_000, enabled=False)
    await _package(
        session, version, category, slug="renew-only", price=20_000, allow_new_sale=False
    )
    await _package(session, version, category, slug="live", price=150_000)
    await session.commit()

    facts = await get_catalog_facts(session)
    assert facts.cheapest_price_toman == 150_000
    assert facts.plan_count == 1


@pytest.mark.asyncio
async def test_a_plan_in_a_disabled_group_is_off_the_shelf(session: AsyncSession) -> None:
    version = await _version(session, status=StoreVersionStatus.published)
    hidden = await _category(session, version, slug="hidden", enabled=False)
    await _package(session, version, hidden, slug="cheap", price=1_000)
    live = await _category(session, version, slug="live")
    await _package(session, version, live, slug="normal", price=150_000)
    await session.commit()

    facts = await get_catalog_facts(session)
    assert facts.cheapest_price_toman == 150_000


@pytest.mark.asyncio
async def test_a_disabled_parent_takes_its_children_off_the_shelf_too(
    session: AsyncSession,
) -> None:
    """«ویژه» can be enabled and still be unreachable if «حجمی» above it is off."""
    version = await _version(session, status=StoreVersionStatus.published)
    parent = await _category(session, version, slug="volume", enabled=False)
    child = await _category(session, version, slug="special", parent=parent, enabled=True)
    await _package(session, version, child, slug="cheap", price=1_000)
    await session.commit()

    facts = await get_catalog_facts(session)
    assert facts.cheapest_price_toman is None
    assert facts.plan_count == 0
    assert facts.group_count == 0


@pytest.mark.asyncio
async def test_child_groups_roll_up_to_the_one_group_the_customer_sees(
    session: AsyncSession,
) -> None:
    """«حجمی» with two children is one shelf, not three."""
    version = await _version(session, status=StoreVersionStatus.published)
    volume = await _category(session, version, slug="volume")
    special = await _category(session, version, slug="special", parent=volume)
    economy = await _category(session, version, slug="economy", parent=volume)
    await _package(session, version, special, slug="s1", price=150_000)
    await _package(session, version, economy, slug="e1", price=90_000)

    unlimited = await _category(session, version, slug="unlimited")
    await _package(session, version, unlimited, slug="u1", price=250_000)
    await session.commit()

    facts = await get_catalog_facts(session)
    assert facts.group_count == 2
    assert facts.plan_count == 3
    assert facts.cheapest_price_toman == 90_000


@pytest.mark.asyncio
async def test_an_empty_group_is_not_counted_as_a_shelf(session: AsyncSession) -> None:
    version = await _version(session, status=StoreVersionStatus.published)
    stocked = await _category(session, version, slug="volume")
    await _package(session, version, stocked, slug="v1", price=150_000)
    await _category(session, version, slug="coming-soon")
    await session.commit()

    facts = await get_catalog_facts(session)
    assert facts.group_count == 1


@pytest.mark.asyncio
async def test_a_draft_price_never_reaches_customer_copy(session: AsyncSession) -> None:
    """A price the operator is still editing must not be advertised."""
    published = await _version(session, status=StoreVersionStatus.published, number=1)
    live = await _category(session, published, slug="volume")
    await _package(session, published, live, slug="live", price=150_000)

    draft = await _version(session, status=StoreVersionStatus.draft, number=2)
    draft_category = await _category(session, draft, slug="volume-draft")
    await _package(session, draft, draft_category, slug="wip", price=1)
    await session.commit()

    facts = await get_catalog_facts(session)
    assert facts.cheapest_price_toman == 150_000
    assert facts.plan_count == 1


@pytest.mark.asyncio
async def test_brand_facts_combine_contact_points_with_live_numbers(
    session: AsyncSession,
) -> None:
    version = await _version(session, status=StoreVersionStatus.published)
    category = await _category(session, version, slug="volume")
    await _package(session, version, category, slug="v1", price=90_000)
    await session.commit()

    facts = await get_brand_facts(
        session,
        Settings(BOT_TOKEN="t", SUPPORT_HANDLE="neo_network_support", SUPPORT_HOURS_FA="۹ تا ۱۲"),
    )
    assert facts.support_handle == "@neo_network_support"
    assert facts.support_hours == "۹ تا ۱۲"
    assert facts.catalog.cheapest_price_toman == 90_000


# --- bot username ----------------------------------------------------------


@pytest.mark.asyncio
async def test_an_empty_bot_username_is_answered_by_telegram_not_by_config() -> None:
    """BOT_USERNAME is empty on this deployment, so get_me() has to carry it."""
    calls: list[int] = []

    async def _get_me() -> SimpleNamespace:
        calls.append(1)
        return SimpleNamespace(username="neo666_bot")

    bot = SimpleNamespace(get_me=_get_me)
    resolved = await resolve_bot_username(bot, Settings(BOT_TOKEN="t", BOT_USERNAME=""))
    assert resolved == "neo666_bot"
    assert calls == [1]


@pytest.mark.asyncio
async def test_a_configured_bot_username_wins_and_costs_no_api_call() -> None:
    async def _get_me() -> SimpleNamespace:  # pragma: no cover - must not run
        raise AssertionError("get_me must not be called when BOT_USERNAME is set")

    bot = SimpleNamespace(get_me=_get_me)
    assert await resolve_bot_username(bot, Settings(BOT_TOKEN="t", BOT_USERNAME="@neo666_bot")) == (
        "neo666_bot"
    )

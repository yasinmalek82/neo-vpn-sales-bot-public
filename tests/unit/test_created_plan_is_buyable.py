"""A plan the owner creates must be buyable exactly as specified.

The first version of the create-plan flow asked only for a title and a price.
A title is a string: «۵۰ گیگ / ۱ ماه» told the bot nothing about 50 GB or 30
days, so ``options`` stayed empty, the purchase handler saw incomplete
dimensions, and it silently dropped the customer into the dynamic گیگ/ماه
builder — discarding the fixed price the owner had set.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from database.base import Base
from services.store_admin import (
    create_draft_category,
    create_draft_package,
    get_or_create_draft,
    publish_draft,
)
from services.store_catalog import (
    filter_ready_plans_by_time_limit,
    package_has_time_limit,
    ready_package_purchase_payload,
)
from services.store_config import get_published_store_config


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _volume_dims_complete(payload: dict) -> bool:
    """The exact condition ``choose_ready_plan`` uses to decide it can proceed."""
    return payload.get("volume_gb") is not None and payload.get("duration_days") is not None


def _unlimited_dims_complete(payload: dict) -> bool:
    return (
        payload.get("concurrent_users") is not None and payload.get("duration_months") is not None
    )


@pytest.mark.asyncio
async def test_a_created_volume_plan_carries_its_dimensions(
    session: AsyncSession,
) -> None:
    cat = await create_draft_category(session, title_fa="ویژه", kind="volume")
    pkg = await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۵۰ گیگ / ۱ ماه",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
    )
    await session.commit()

    draft = await get_or_create_draft(session)
    payload = ready_package_purchase_payload(pkg, list(draft.package_prices or []))
    assert payload["volume_gb"] == 50
    assert payload["duration_days"] == 30
    assert payload["fixed_price_toman"] == 150_000
    assert _volume_dims_complete(payload), (
        "incomplete dims make choose_ready_plan fall back to the custom builder, "
        "which throws the fixed price away"
    )


@pytest.mark.asyncio
async def test_a_created_unlimited_plan_carries_its_dimensions(
    session: AsyncSession,
) -> None:
    cat = await create_draft_category(session, title_fa="نامحدود", kind="unlimited")
    pkg = await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۲ کاربره / ۳ ماه",
        fixed_price_toman=450_000,
        concurrent_users=2,
        duration_months=3,
    )
    await session.commit()

    draft = await get_or_create_draft(session)
    payload = ready_package_purchase_payload(pkg, list(draft.package_prices or []))
    assert payload["concurrent_users"] == 2
    assert payload["duration_months"] == 3
    assert _unlimited_dims_complete(payload)


@pytest.mark.asyncio
async def test_a_dated_plan_is_marked_as_time_limited(session: AsyncSession) -> None:
    """Otherwise the «فیلتر محدودیت زمانی» step hides it from customers."""
    cat = await create_draft_category(session, title_fa="ویژه", kind="volume")
    pkg = await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۵۰ گیگ / ۱ ماه",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
    )
    await session.commit()

    assert package_has_time_limit(pkg) is True
    assert filter_ready_plans_by_time_limit([pkg]) == [pkg]


@pytest.mark.asyncio
async def test_a_plan_without_an_expiry_is_not_time_limited(
    session: AsyncSession,
) -> None:
    cat = await create_draft_category(session, title_fa="ویژه", kind="volume")
    pkg = await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۵۰ گیگ بدون انقضا",
        fixed_price_toman=120_000,
        volume_gb=50,
    )
    await session.commit()
    assert package_has_time_limit(pkg) is False


@pytest.mark.asyncio
async def test_dimensions_survive_publish(session: AsyncSession) -> None:
    """The clone loop must carry ``options`` across, or the live plan breaks."""
    cat = await create_draft_category(session, title_fa="ویژه", kind="volume")
    await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۱۰۰ گیگ / ۳ ماه",
        fixed_price_toman=390_000,
        volume_gb=100,
        duration_days=90,
    )
    await session.commit()
    await publish_draft(session)
    await session.commit()

    cfg = await get_published_store_config(session)
    assert cfg is not None
    payload = ready_package_purchase_payload(cfg.packages[0], list(cfg.prices or []))
    assert _volume_dims_complete(payload)
    assert payload["volume_gb"] == 100
    assert payload["duration_days"] == 90
    assert payload["fixed_price_toman"] == 390_000


@pytest.mark.asyncio
async def test_dimensions_must_be_positive(session: AsyncSession) -> None:
    from services.store_admin import StoreAdminError

    cat = await create_draft_category(session, title_fa="ویژه", kind="volume")
    with pytest.raises(StoreAdminError):
        await create_draft_package(
            session,
            category_id=cat.id,
            title_fa="بد",
            fixed_price_toman=1000,
            volume_gb=0,
        )
    with pytest.raises(StoreAdminError):
        await create_draft_package(
            session,
            category_id=cat.id,
            title_fa="بد",
            fixed_price_toman=1000,
            volume_gb=10,
            duration_days=-5,
        )


# --------------------------------------------------------------------------- #
# The admin-side parsing that feeds those dimensions
# --------------------------------------------------------------------------- #


def test_number_parsing_accepts_persian_and_separators() -> None:
    from bot.handlers.store_admin import _parse_number

    assert _parse_number("50") == 50
    assert _parse_number(" 50 ") == 50
    assert _parse_number("۵۰") == 50
    assert _parse_number("150,000") == 150_000
    assert _parse_number("۱۵۰٬۰۰۰") == 150_000


def test_number_parsing_refuses_junk_instead_of_raising() -> None:
    """A raw ValueError here would print English into the Persian UI."""
    from bot.handlers.store_admin import _parse_number

    for bad in ("", "abc", "۱۰ گیگ  | یک ماهه", "50 30", "0", "-5", "۵۰ خیلی"):
        assert _parse_number(bad) is None, bad


def test_a_price_typed_into_a_size_prompt_is_refused() -> None:
    """The live trap: «60000» at a size prompt became a 60000 GB plan."""
    from services.store_admin import DIM_LIMITS

    low, high = DIM_LIMITS["volume_gb"]
    assert high < 60_000


@pytest.mark.asyncio
async def test_absurd_dimensions_are_refused(session: AsyncSession) -> None:
    from services.store_admin import StoreAdminError

    cat = await create_draft_category(session, title_fa="ویژه", kind="volume")
    with pytest.raises(StoreAdminError):
        await create_draft_package(
            session,
            category_id=cat.id,
            title_fa="۶۰۰۰۰ گیگ",
            fixed_price_toman=60_000,
            volume_gb=60_000,
        )
    with pytest.raises(StoreAdminError):
        await create_draft_package(
            session,
            category_id=cat.id,
            title_fa="ابدی",
            fixed_price_toman=1000,
            volume_gb=50,
            duration_days=99_999,
        )


def test_derived_titles_read_naturally_in_persian() -> None:
    from services.store_plan_edit import derive_plan_title as _derive_plan_title

    assert _derive_plan_title(kind="volume", size=50, duration=30) == "۵۰ گیگ / ۳۰ روز"
    assert _derive_plan_title(kind="volume", size=50, duration=None) == "۵۰ گیگ بدون انقضا"
    assert _derive_plan_title(kind="unlimited", size=2, duration=3) == "۲ کاربره / ۳ ماه"


def test_the_size_step_offers_presets_so_typing_is_optional() -> None:
    """The single free-text «گیگ روز» prompt is what produced the live trap."""
    from bot.keyboards.store_admin import StoreCB, store_new_pkg_size_keyboard

    kb = store_new_pkg_size_keyboard(kind="volume")
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any(c == f"{StoreCB.PKG_SIZE}50" for c in cbs)
    assert StoreCB.PKG_SIZE_OTHER in cbs


def test_only_a_volume_plan_may_have_no_expiry() -> None:
    from bot.keyboards.store_admin import StoreCB, store_new_pkg_duration_keyboard

    volume = [
        b.callback_data
        for row in store_new_pkg_duration_keyboard(kind="volume").inline_keyboard
        for b in row
    ]
    unlimited = [
        b.callback_data
        for row in store_new_pkg_duration_keyboard(kind="unlimited").inline_keyboard
        for b in row
    ]
    assert f"{StoreCB.PKG_DUR}0" in volume
    assert f"{StoreCB.PKG_DUR}0" not in unlimited


# --------------------------------------------------------------------------- #
# Editing an existing plan's dimensions
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dimensions_can_be_edited_without_rebuilding_the_plan(
    session: AsyncSession,
) -> None:
    from services.store_admin import update_draft_package

    cat = await create_draft_category(session, title_fa="ویژه", kind="volume")
    pkg = await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۵۰ گیگ / ۳۰ روز",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
    )
    await session.commit()

    await update_draft_package(session, pkg.id, volume_gb=100, duration_days=90)
    await session.commit()

    draft = await get_or_create_draft(session)
    payload = ready_package_purchase_payload(pkg, list(draft.package_prices or []))
    assert payload["volume_gb"] == 100
    assert payload["duration_days"] == 90
    assert payload["fixed_price_toman"] == 150_000, "editing size must not touch price"


@pytest.mark.asyncio
async def test_the_time_limit_flag_follows_the_duration(session: AsyncSession) -> None:
    """It is derived, so it can never disagree with the plan's actual expiry."""
    from services.store_admin import update_draft_package

    cat = await create_draft_category(session, title_fa="ویژه", kind="volume")
    pkg = await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۵۰ گیگ / ۳۰ روز",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
    )
    await session.commit()
    assert package_has_time_limit(pkg) is True

    await update_draft_package(session, pkg.id, volume_gb=50, clear_duration=True)
    await session.commit()
    assert package_has_time_limit(pkg) is False

    await update_draft_package(session, pkg.id, duration_days=60)
    await session.commit()
    assert package_has_time_limit(pkg) is True


@pytest.mark.asyncio
async def test_a_zero_price_is_refused(session: AsyncSession) -> None:
    """A free service is the trial feature; a 0-toman catalog plan is a typo."""
    from services.store_admin import StoreAdminError

    cat = await create_draft_category(session, title_fa="ویژه", kind="volume")
    with pytest.raises(StoreAdminError):
        await create_draft_package(
            session,
            category_id=cat.id,
            title_fa="رایگان؟",
            fixed_price_toman=0,
            volume_gb=50,
            duration_days=30,
        )


@pytest.mark.asyncio
async def test_the_admin_screen_shows_the_dimensions_it_saved(
    session: AsyncSession,
) -> None:
    """The detail screen only knew the custom-template keys, so a created plan
    displayed «گزینه‌ها: —» and the owner could not verify what they built."""
    from bot.handlers.store_admin import _package_detail_text

    cat = await create_draft_category(session, title_fa="ویژه", kind="volume")
    pkg = await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۵۰ گیگ / ۳۰ روز",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
    )
    await session.commit()

    from services.store_admin import get_catalog_view

    view = await get_catalog_view(session)
    body = _package_detail_text(pkg, view.prices)
    # The summary line carries the dimensions in Persian digits: size, duration, price.
    assert "۵۰ گیگ" in body
    assert "۱ ماهه" in body
    assert "۱۵۰٬۰۰۰" in body or "۱۵۰,۰۰۰" in body or "150,000" in body


@pytest.mark.asyncio
async def test_an_incomplete_plan_is_flagged_to_the_owner(
    session: AsyncSession,
) -> None:
    """Legacy rows (seeded, or inserted by SQL) must announce themselves."""
    from bot.handlers.store_admin import _package_detail_text
    from database.models.store import StorePackage, StorePackageKind
    from services.store_admin import get_or_create_draft
    from services.store_plan_view import ready_dims_complete as _ready_dims_complete

    cat = await create_draft_category(session, title_fa="ویژه", kind="volume")
    draft = await get_or_create_draft(session)
    broken = StorePackage(
        store_version_id=draft.id,
        category_id=cat.id,
        slug="legacy-row",
        kind=StorePackageKind.ready,
        title_fa="۵۰ گیگ / ۱ ماه",
        enabled=True,
        sort_order=10,
        fixed_price_toman=150_000,
        options={},  # the shape the old create flow produced
        sales_flags={},
    )
    session.add(broken)
    await session.commit()

    assert _ready_dims_complete(broken) is False
    assert "⚠️" in _package_detail_text(broken)

"""Creating and deleting catalog rows — the operations the store never had.

Before this, a seeded catalog held only two ``custom_template`` packages, there
was no ``create_draft_package`` at all, and so no price a customer actually pays
could be changed from inside Telegram.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from database.base import Base
from database.models.store import StorePackageKind, StoreVersionStatus
from services.store_admin import (
    MAX_CATEGORY_DEPTH,
    StoreAdminError,
    commit_and_activate,
    create_draft_category,
    create_draft_package,
    delete_draft_category,
    delete_draft_package,
    discard_draft,
    get_catalog_view,
    get_draft_version,
    get_or_create_draft,
    list_draft_categories,
    list_draft_packages,
    publish_draft,
    update_draft_category,
    update_draft_package,
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


async def _volume_group(session: AsyncSession, title: str = "حجمی"):
    return await create_draft_category(session, title_fa=title, kind="volume")


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_owner_can_create_a_priced_plan(session: AsyncSession) -> None:
    cat = await _volume_group(session)
    pkg = await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۵۰ گیگ / ۱ ماه",
        fixed_price_toman=150_000,
        options={"volume_gb": 50, "duration_days": 30},
    )
    await session.commit()

    assert pkg.kind is StorePackageKind.ready
    assert pkg.fixed_price_toman == 150_000
    assert pkg.pricing_ref is None
    assert pkg.options["volume_gb"] == 50

    plans = await list_draft_packages(session)
    assert [p.id for p in plans] == [pkg.id]


@pytest.mark.asyncio
async def test_plan_price_is_editable_after_creation(session: AsyncSession) -> None:
    cat = await _volume_group(session)
    pkg = await create_draft_package(
        session, category_id=cat.id, title_fa="پایه", fixed_price_toman=100_000
    )
    await session.commit()

    await update_draft_package(session, pkg.id, fixed_price_toman=125_000)
    await session.commit()
    assert (await list_draft_packages(session))[0].fixed_price_toman == 125_000


@pytest.mark.asyncio
async def test_plans_get_distinct_slugs_within_a_version(session: AsyncSession) -> None:
    """``uq_store_packages_version_slug`` would reject a repeat."""
    cat = await _volume_group(session)
    a = await create_draft_package(session, category_id=cat.id, title_fa="اول", fixed_price_toman=1)
    b = await create_draft_package(session, category_id=cat.id, title_fa="دوم", fixed_price_toman=2)
    await session.commit()
    assert a.slug != b.slug


@pytest.mark.asyncio
async def test_negative_price_is_refused(session: AsyncSession) -> None:
    cat = await _volume_group(session)
    with pytest.raises(StoreAdminError):
        await create_draft_package(session, category_id=cat.id, title_fa="بد", fixed_price_toman=-1)


@pytest.mark.asyncio
async def test_blank_title_is_refused(session: AsyncSession) -> None:
    cat = await _volume_group(session)
    with pytest.raises(StoreAdminError):
        await create_draft_package(
            session, category_id=cat.id, title_fa="   ", fixed_price_toman=1000
        )


@pytest.mark.asyncio
async def test_plan_can_be_deleted(session: AsyncSession) -> None:
    cat = await _volume_group(session)
    pkg = await create_draft_package(
        session, category_id=cat.id, title_fa="موقت", fixed_price_toman=1000
    )
    await session.commit()

    await delete_draft_package(session, pkg.id)
    await session.commit()
    assert await list_draft_packages(session) == []


# --------------------------------------------------------------------------- #
# Nested groups — «حجمی» → «ویژه» / «اقتصادی»
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_child_groups_nest_one_level(session: AsyncSession) -> None:
    parent = await _volume_group(session)
    premium = await create_draft_category(
        session, title_fa="ویژه", kind="volume", parent_id=parent.id
    )
    economy = await create_draft_category(
        session, title_fa="اقتصادی", kind="volume", parent_id=parent.id
    )
    await session.commit()

    assert premium.parent_id == parent.id
    assert economy.parent_id == parent.id
    cats = await list_draft_categories(session)
    assert {c.id for c in cats} == {parent.id, premium.id, economy.id}


@pytest.mark.asyncio
async def test_a_third_level_is_refused(session: AsyncSession) -> None:
    """A customer must never tap more than three times to reach a price."""
    assert MAX_CATEGORY_DEPTH == 2
    parent = await _volume_group(session)
    child = await create_draft_category(
        session, title_fa="ویژه", kind="volume", parent_id=parent.id
    )
    await session.commit()
    with pytest.raises(StoreAdminError):
        await create_draft_category(session, title_fa="طلایی", kind="volume", parent_id=child.id)


@pytest.mark.asyncio
async def test_a_group_cannot_hold_both_plans_and_child_groups(
    session: AsyncSession,
) -> None:
    """Otherwise the customer screen would mix two kinds of button."""
    parent = await _volume_group(session)
    await create_draft_package(
        session, category_id=parent.id, title_fa="مستقیم", fixed_price_toman=1000
    )
    await session.commit()
    with pytest.raises(StoreAdminError):
        await create_draft_category(session, title_fa="ویژه", kind="volume", parent_id=parent.id)


@pytest.mark.asyncio
async def test_plans_cannot_hang_off_a_group_that_has_children(
    session: AsyncSession,
) -> None:
    parent = await _volume_group(session)
    await create_draft_category(session, title_fa="ویژه", kind="volume", parent_id=parent.id)
    await session.commit()
    with pytest.raises(StoreAdminError):
        await create_draft_package(
            session, category_id=parent.id, title_fa="نامعتبر", fixed_price_toman=1000
        )


@pytest.mark.asyncio
async def test_deleting_a_parent_takes_its_children_and_their_plans(
    session: AsyncSession,
) -> None:
    parent = await _volume_group(session)
    child = await create_draft_category(
        session, title_fa="ویژه", kind="volume", parent_id=parent.id
    )
    await create_draft_package(
        session, category_id=child.id, title_fa="۵۰ گیگ", fixed_price_toman=150_000
    )
    other = await create_draft_category(session, title_fa="نامحدود", kind="unlimited")
    await session.commit()

    await delete_draft_category(session, parent.id)
    await session.commit()

    assert [c.id for c in await list_draft_categories(session)] == [other.id]
    assert await list_draft_packages(session) == []


@pytest.mark.asyncio
async def test_the_last_top_level_group_cannot_be_deleted(session: AsyncSession) -> None:
    """A catalog with no group cannot be published, so refuse it early."""
    only = await _volume_group(session)
    await session.commit()
    with pytest.raises(StoreAdminError):
        await delete_draft_category(session, only.id)


# --------------------------------------------------------------------------- #
# Draft lifecycle
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_draft_can_be_discarded(session: AsyncSession) -> None:
    """The only previous exits were publishing or restoring an archived version."""
    await _volume_group(session)
    await session.commit()
    assert await get_draft_version(session) is not None

    await discard_draft(session)
    await session.commit()
    assert await get_draft_version(session) is None


@pytest.mark.asyncio
async def test_discarding_without_a_draft_is_an_explicit_error(
    session: AsyncSession,
) -> None:
    with pytest.raises(StoreAdminError):
        await discard_draft(session)


@pytest.mark.asyncio
async def test_nesting_and_prices_survive_publish_then_reclone(
    session: AsyncSession,
) -> None:
    """Cloning remaps category ids; parent links must be remapped with them."""
    parent = await _volume_group(session)
    child = await create_draft_category(
        session, title_fa="ویژه", kind="volume", parent_id=parent.id
    )
    await create_draft_package(
        session, category_id=child.id, title_fa="۵۰ گیگ", fixed_price_toman=150_000
    )
    await session.commit()

    published = await publish_draft(session)
    await session.commit()
    assert published.status is StoreVersionStatus.published

    # Touching the catalog again clones the published version into a new draft.
    draft = await get_or_create_draft(session)
    await session.commit()
    assert draft.id != published.id

    cats = {c.title_fa: c for c in await list_draft_categories(session)}
    assert cats["ویژه"].parent_id == cats["حجمی"].id
    assert cats["ویژه"].parent_id != child.id  # a fresh row, not the old id

    plans = await list_draft_packages(session)
    assert [p.fixed_price_toman for p in plans] == [150_000]
    assert plans[0].category_id == cats["ویژه"].id


# --------------------------------------------------------------------------- #
# No publish step: an edit is live the moment it is confirmed
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_confirmed_edit_is_live_with_no_publish_step(
    session: AsyncSession,
) -> None:
    """«این دردسر و اضافه‌کاری پیش‌نویس و انتشار دیگه نباشه».

    The hub no longer carries a publish button, so if an edit only reached the
    draft it would be stranded there **forever** — invisible to every customer,
    with nothing left in the UI able to release it. This is the test that says
    the removal did not create that trap.
    """
    cat = await _volume_group(session)
    await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۵۰ گیگ / ۳۰ روز",
        fixed_price_toman=150_000,
        options={"volume_gb": 50, "duration_days": 30},
    )
    published = await commit_and_activate(session, notes="test")

    assert published is not None
    assert published.status is StoreVersionStatus.published
    assert await get_draft_version(session) is None, "a draft was left behind"

    view = await get_catalog_view(session)
    assert view.is_draft is False
    titles = [p.title_fa for p in view.packages]
    assert "۵۰ گیگ / ۳۰ روز" in titles


@pytest.mark.asyncio
async def test_activating_with_nothing_pending_is_a_no_op(
    session: AsyncSession,
) -> None:
    """Every edit path calls this, including ones that changed nothing."""
    cat = await _volume_group(session)
    await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۱۰۰ گیگ / ۳۰ روز",
        fixed_price_toman=200_000,
        options={"volume_gb": 100, "duration_days": 30},
    )
    first = await commit_and_activate(session)
    assert first is not None

    second = await commit_and_activate(session)
    assert second is None, "a second activation must not burn another version"


@pytest.mark.asyncio
async def test_a_second_edit_lands_on_top_of_the_first(session: AsyncSession) -> None:
    """Two edits in a row must both survive — the second must not revert the first."""
    cat = await _volume_group(session)
    pkg = await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="اولی",
        fixed_price_toman=100_000,
        options={"volume_gb": 30, "duration_days": 30},
    )
    first = await commit_and_activate(session)
    assert first is not None

    view = await get_catalog_view(session)
    live_pkg = next(p for p in view.packages if p.title_fa == "اولی")
    await update_draft_package(session, live_pkg.id, fixed_price_toman=120_000)
    second = await commit_and_activate(session)
    assert second is not None
    assert second.version > first.version

    view = await get_catalog_view(session)
    prices = {p.title_fa: p.fixed_price_toman for p in view.packages}
    assert prices["اولی"] == 120_000
    assert len(view.packages) == 1, "the edit duplicated the plan instead of updating it"
    del pkg


@pytest.mark.asyncio
async def test_a_button_from_before_the_last_edit_still_opens_its_group(
    session: AsyncSession,
) -> None:
    """The owner's report: «پلن‌های این گروه هیچی نمایش نمی‌ده».

    Publishing clones the version, and a clone mints new row ids. Every button
    already on the owner's screen therefore carries an id from *before* the last
    edit. Looking a group up by that id found nothing, so the group's plan list
    came back empty — while «پلن‌های آماده», which lists packages without
    filtering by category, still showed the plan. That asymmetry is the whole
    symptom.
    """
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session)

    view = await get_catalog_view(session)
    button_id = view.categories[0].id  # what the owner's screen is holding

    await create_draft_package(
        session,
        category_id=button_id,
        title_fa="۵۰ گیگ / ۳۰ روز",
        fixed_price_toman=150_000,
        options={"volume_gb": 50, "duration_days": 30},
    )
    await commit_and_activate(session)

    # The owner taps that same button.
    view = await get_catalog_view(session)
    assert view.categories[0].id != button_id, "precondition: ids really do shift"

    opened = view.category(button_id)
    assert opened is not None, "the group could not be opened at all"
    plans = view.packages_of(button_id)
    assert [p.title_fa for p in plans] == ["۵۰ گیگ / ۳۰ روز"]
    del cat


@pytest.mark.asyncio
async def test_a_stale_plan_button_still_opens_its_plan(session: AsyncSession) -> None:
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session)
    view = await get_catalog_view(session)

    await create_draft_package(
        session,
        category_id=view.categories[0].id,
        title_fa="۱۰۰ گیگ / ۳۰ روز",
        fixed_price_toman=200_000,
        options={"volume_gb": 100, "duration_days": 30},
    )
    await commit_and_activate(session)
    view = await get_catalog_view(session)
    plan_button_id = view.packages[0].id

    # Any later edit shifts the ids again.
    await update_draft_category(session, view.categories[0].id, title_fa="حجمی ویژه")
    await commit_and_activate(session)

    view = await get_catalog_view(session)
    assert view.packages[0].id != plan_button_id
    assert view.package(plan_button_id) is not None
    assert view.package(plan_button_id).title_fa == "۱۰۰ گیگ / ۳۰ روز"
    del cat

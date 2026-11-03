"""Deep service-layer bug hunt for the versioned store catalog.

Covers nesting, ready-plan dimensions, commit_and_activate id/slug identity,
undo/change-log honesty, panel-group resolution, and derived has_time_limit.
Asserts observable behaviour through the real service functions — never by
restating the rule under test.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from database.base import Base
from database.models.store import StoreVersion, StoreVersionStatus
from scheduler.jobs.cleanup import (
    KEEP_ARCHIVED_STORE_VERSIONS,
    STORE_CHANGE_RETENTION_DAYS,
    prune_old_rows,
)
from services.panel_settings import resolve_groups_for_plan_type
from services.pricing import PlanType
from services.store_admin import (
    StoreAdminError,
    commit_and_activate,
    create_draft_category,
    create_draft_package,
    delete_draft_category,
    get_catalog_view,
    get_draft_category,
    get_draft_package,
    get_draft_version,
    get_or_create_draft,
    list_draft_packages,
    update_draft_category,
    update_draft_package,
)
from services.store_catalog import package_has_time_limit, ready_package_purchase_payload
from services.store_strategy import list_store_changes, undo_store_change


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _settings(*, defaults: list[int] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        pasar_guard_base_url="https://panel.example",
        pasar_guard_api_key="pg_key_test",
        pasar_guard_sub_path="sub",
        pasar_guard_timeout=10.0,
        default_panel_group_ids=list(defaults or [900]),
    )


async def _version_count(session: AsyncSession) -> int:
    return int((await session.scalar(select(func.count()).select_from(StoreVersion))) or 0)


async def _seed_volume_shop(session: AsyncSession) -> int:
    """One published volume group; return its live category id."""
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="گروه محصول ساخته شد")
    view = await get_catalog_view(session)
    return view.categories[0].id


# --------------------------------------------------------------------------- #
# Categories / nesting / slugs
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rename_keeps_slug_identity(session: AsyncSession) -> None:
    """Slug is the identity across publishes; a title change must not mint a new one."""
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    slug_before = cat.slug
    await update_draft_category(session, cat.id, title_fa="حجمی ویژه")
    await session.flush()
    assert cat.slug == slug_before
    assert cat.title_fa == "حجمی ویژه"


@pytest.mark.asyncio
async def test_same_kind_categories_get_distinct_slugs(session: AsyncSession) -> None:
    a = await create_draft_category(session, title_fa="حجمی الف", kind="volume")
    b = await create_draft_category(session, title_fa="حجمی ب", kind="volume")
    await session.commit()
    assert a.slug != b.slug
    assert a.slug == "volume"
    assert b.slug.startswith("volume")


@pytest.mark.asyncio
async def test_deleted_category_slug_is_not_reused_fresh(
    session: AsyncSession,
) -> None:
    """Taken-slug set spans versions; recycling would collide with archived rows."""
    first = await create_draft_category(session, title_fa="حجمی", kind="volume")
    await create_draft_category(session, title_fa="نامحدود", kind="unlimited")
    await commit_and_activate(session, notes="دو گروه")
    view = await get_catalog_view(session)
    doomed = next(c for c in view.categories if c.slug == first.slug)
    await delete_draft_category(session, doomed.id)
    await commit_and_activate(session, notes="حذف حجمی")

    again = await create_draft_category(session, title_fa="حجمی دوباره", kind="volume")
    assert again.slug != first.slug


@pytest.mark.asyncio
async def test_child_created_with_stale_parent_id_links_to_draft_parent(
    session: AsyncSession,
) -> None:
    """After publish, buttons/FSM hold the *old* parent id.

    ``get_draft_category`` remaps via slug, but the child row must store the
    remapped draft parent's id — not the stale input — or inheritance and
    ``children_of`` silently lose the subgroup.
    """
    parent = await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="گروه ساخته شد")
    view = await get_catalog_view(session)
    stale_parent_id = view.categories[0].id

    # Any later edit shifts ids; the owner's FSM still holds the pre-edit id.
    await update_draft_category(session, stale_parent_id, title_fa="حجمی")
    await commit_and_activate(session, notes="ویرایش عنوان")
    view = await get_catalog_view(session)
    live_parent = view.categories[0]
    assert live_parent.id != stale_parent_id

    child = await create_draft_category(
        session, title_fa="ویژه", kind="volume", parent_id=stale_parent_id
    )
    await commit_and_activate(session, notes="زیرگروه ساخته شد")

    view = await get_catalog_view(session)
    live_parent = next(c for c in view.categories if c.parent_id is None)
    children = view.children_of(live_parent.id)
    assert [c.title_fa for c in children] == ["ویژه"]
    assert children[0].parent_id == live_parent.id
    # The stale id must still resolve through CatalogView.
    assert [c.title_fa for c in view.children_of(stale_parent_id)] == ["ویژه"]
    del parent, child


@pytest.mark.asyncio
async def test_third_level_nesting_refused(session: AsyncSession) -> None:
    parent = await create_draft_category(session, title_fa="حجمی", kind="volume")
    child = await create_draft_category(
        session, title_fa="ویژه", kind="volume", parent_id=parent.id
    )
    with pytest.raises(StoreAdminError, match="یک سطح"):
        await create_draft_category(session, title_fa="طلایی", kind="volume", parent_id=child.id)


@pytest.mark.asyncio
async def test_mixing_direct_plans_and_child_groups_rejected_both_ways(
    session: AsyncSession,
) -> None:
    parent = await create_draft_category(session, title_fa="حجمی", kind="volume")
    await create_draft_package(
        session,
        category_id=parent.id,
        title_fa="مستقیم",
        fixed_price_toman=10_000,
        volume_gb=10,
        duration_days=30,
    )
    with pytest.raises(StoreAdminError):
        await create_draft_category(session, title_fa="ویژه", kind="volume", parent_id=parent.id)

    parent2 = await create_draft_category(session, title_fa="نامحدود", kind="unlimited")
    await create_draft_category(session, title_fa="ویژه", kind="unlimited", parent_id=parent2.id)
    with pytest.raises(StoreAdminError):
        await create_draft_package(
            session,
            category_id=parent2.id,
            title_fa="روی والد",
            fixed_price_toman=10_000,
            concurrent_users=1,
            duration_months=1,
        )


@pytest.mark.asyncio
async def test_deleting_parent_cascades_children_and_plans(session: AsyncSession) -> None:
    parent = await create_draft_category(session, title_fa="حجمی", kind="volume")
    child = await create_draft_category(
        session, title_fa="ویژه", kind="volume", parent_id=parent.id
    )
    await create_draft_package(
        session,
        category_id=child.id,
        title_fa="۵۰ گیگ",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
    )
    keeper = await create_draft_category(session, title_fa="نامحدود", kind="unlimited")
    await delete_draft_category(session, parent.id)
    await session.flush()
    draft = await get_draft_version(session)
    assert draft is not None
    assert {c.id for c in draft.categories} == {keeper.id}
    assert list(draft.packages) == []


@pytest.mark.asyncio
async def test_last_top_level_group_cannot_be_deleted(session: AsyncSession) -> None:
    only = await create_draft_category(session, title_fa="حجمی", kind="volume")
    with pytest.raises(StoreAdminError, match="آخرین"):
        await delete_draft_category(session, only.id)


# --------------------------------------------------------------------------- #
# Ready plans
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_volume_and_unlimited_dimension_shapes(session: AsyncSession) -> None:
    vol = await create_draft_category(session, title_fa="حجمی", kind="volume")
    unl = await create_draft_category(session, title_fa="نامحدود", kind="unlimited")
    vpkg = await create_draft_package(
        session,
        category_id=vol.id,
        title_fa="۵۰/۳۰",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
    )
    upkg = await create_draft_package(
        session,
        category_id=unl.id,
        title_fa="۲/۳",
        fixed_price_toman=450_000,
        concurrent_users=2,
        duration_months=3,
    )
    draft = await get_or_create_draft(session)
    vpay = ready_package_purchase_payload(vpkg, list(draft.package_prices or []))
    upay = ready_package_purchase_payload(upkg, list(draft.package_prices or []))
    assert vpay["volume_gb"] == 50 and vpay["duration_days"] == 30
    assert vpay["fixed_price_toman"] == 150_000
    assert upay["concurrent_users"] == 2 and upay["duration_months"] == 3
    assert upay["fixed_price_toman"] == 450_000


@pytest.mark.asyncio
async def test_dimension_bounds_refuse_absurd_values(session: AsyncSession) -> None:
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    with pytest.raises(StoreAdminError):
        await create_draft_package(
            session,
            category_id=cat.id,
            title_fa="بد",
            fixed_price_toman=10_000,
            volume_gb=60_000,
            duration_days=30,
        )
    with pytest.raises(StoreAdminError):
        await create_draft_package(
            session,
            category_id=cat.id,
            title_fa="بد",
            fixed_price_toman=10_000,
            volume_gb=50,
            duration_days=0,
        )


@pytest.mark.asyncio
async def test_zero_and_negative_price_refused(session: AsyncSession) -> None:
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    with pytest.raises(StoreAdminError):
        await create_draft_package(
            session, category_id=cat.id, title_fa="رایگان", fixed_price_toman=0
        )
    with pytest.raises(StoreAdminError):
        await create_draft_package(
            session, category_id=cat.id, title_fa="منفی", fixed_price_toman=-1
        )


@pytest.mark.asyncio
async def test_price_edit_and_disable_visibility(session: AsyncSession) -> None:
    cat_id = await _seed_volume_shop(session)
    pkg = await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="۵۰ گیگ",
        fixed_price_toman=100_000,
        volume_gb=50,
        duration_days=30,
    )
    await commit_and_activate(session, notes="ساخته شد")
    view = await get_catalog_view(session)
    live = view.packages[0]
    await update_draft_package(session, live.id, fixed_price_toman=125_000, enabled=False)
    await commit_and_activate(session, notes="قیمت و خاموشی")
    view = await get_catalog_view(session)
    assert view.packages[0].fixed_price_toman == 125_000
    assert view.packages[0].enabled is False
    del pkg


@pytest.mark.asyncio
async def test_duplicate_plan_slugs_within_version_are_unique(
    session: AsyncSession,
) -> None:
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    a = await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="الف",
        fixed_price_toman=1,
        volume_gb=10,
        duration_days=30,
    )
    b = await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="ب",
        fixed_price_toman=2,
        volume_gb=20,
        duration_days=30,
    )
    assert a.slug != b.slug


@pytest.mark.asyncio
async def test_list_draft_packages_with_stale_category_id(
    session: AsyncSession,
) -> None:
    """Same stale-id problem as CatalogView, on the draft list path."""
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۵۰ گیگ",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
    )
    await commit_and_activate(session, notes="ساخته شد")
    view = await get_catalog_view(session)
    stale_cat_id = view.categories[0].id
    stale_pkg_id = view.packages[0].id

    await update_draft_category(session, stale_cat_id, title_fa="حجمی ۲")
    await commit_and_activate(session, notes="ویرایش")

    view = await get_catalog_view(session)
    assert view.categories[0].id != stale_cat_id

    # Opening a draft edit with the button id from before the rename.
    plans = await list_draft_packages(session, category_id=stale_cat_id)
    assert [p.title_fa for p in plans] == ["۵۰ گیگ"]

    resolved = await get_draft_package(session, stale_pkg_id)
    assert resolved.title_fa == "۵۰ گیگ"
    resolved_cat = await get_draft_category(session, stale_cat_id)
    assert resolved_cat.title_fa == "حجمی ۲"


# --------------------------------------------------------------------------- #
# Publish / version / draft lifecycle
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_catalog_view_never_creates_a_draft(session: AsyncSession) -> None:
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="ساخته شد")
    before = await _version_count(session)
    assert await get_draft_version(session) is None

    view = await get_catalog_view(session)
    assert view.exists and view.is_draft is False
    assert await _version_count(session) == before
    assert await get_draft_version(session) is None


@pytest.mark.asyncio
async def test_get_or_create_draft_is_idempotent(session: AsyncSession) -> None:
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="ساخته شد")
    d1 = await get_or_create_draft(session)
    d2 = await get_or_create_draft(session)
    assert d1.id == d2.id
    assert await _version_count(session) == 2  # published + one draft


@pytest.mark.asyncio
async def test_old_ids_resolve_after_commit_and_second_edit(
    session: AsyncSession,
) -> None:
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="گروه")
    view = await get_catalog_view(session)
    cat_btn = view.categories[0].id

    await create_draft_package(
        session,
        category_id=cat_btn,
        title_fa="۵۰ گیگ",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
    )
    await commit_and_activate(session, notes="پلن")
    view = await get_catalog_view(session)
    pkg_btn = view.packages[0].id
    assert view.categories[0].id != cat_btn

    await update_draft_package(session, pkg_btn, fixed_price_toman=160_000)
    await commit_and_activate(session, notes="قیمت")

    view = await get_catalog_view(session)
    assert view.category(cat_btn) is not None
    assert view.package(pkg_btn) is not None
    assert view.package(pkg_btn).fixed_price_toman == 160_000
    assert view.packages_of(cat_btn)[0].title_fa == "۵۰ گیگ"


# --------------------------------------------------------------------------- #
# Undo / pruning
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_undo_latest_and_middle_with_undoes_also(
    session: AsyncSession,
) -> None:
    cat_id = await _seed_volume_shop(session)
    await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="الف",
        fixed_price_toman=100_000,
        volume_gb=10,
        duration_days=30,
    )
    await commit_and_activate(session, notes="الف")
    await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="ب",
        fixed_price_toman=200_000,
        volume_gb=20,
        duration_days=30,
    )
    await commit_and_activate(session, notes="ب")
    await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="ج",
        fixed_price_toman=300_000,
        volume_gb=30,
        duration_days=30,
    )
    await commit_and_activate(session, notes="ج")

    entries = await list_store_changes(session)
    assert entries[0].description == "ج"
    assert entries[0].undoes_also == 0
    assert entries[1].undoes_also == 1
    assert entries[2].undoes_also == 2

    # Undo middle («ب») also rolls back «ج».
    middle = next(e for e in entries if e.description == "ب")
    assert middle.undoes_also == 1
    await undo_store_change(session, middle.version_id)
    await commit_and_activate(session, notes="برگشت: ب")

    view = await get_catalog_view(session)
    titles = sorted(p.title_fa for p in view.packages)
    assert titles == ["الف"]


@pytest.mark.asyncio
async def test_undo_of_undo_restores_later_state(session: AsyncSession) -> None:
    cat_id = await _seed_volume_shop(session)
    await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="تنها",
        fixed_price_toman=100_000,
        volume_gb=10,
        duration_days=30,
    )
    await commit_and_activate(session, notes="پلن")
    await update_draft_package(
        session, (await get_catalog_view(session)).packages[0].id, fixed_price_toman=111_000
    )
    await commit_and_activate(session, notes="گران شد")

    entries = await list_store_changes(session)
    await undo_store_change(session, entries[0].version_id)
    await commit_and_activate(session, notes="برگشت: گران شد")
    assert (await get_catalog_view(session)).packages[0].fixed_price_toman == 100_000

    entries = await list_store_changes(session)
    assert entries[0].description.startswith("برگشت:")
    await undo_store_change(session, entries[0].version_id)
    await commit_and_activate(session, notes="برگشت: برگشت")
    assert (await get_catalog_view(session)).packages[0].fixed_price_toman == 111_000


@pytest.mark.asyncio
async def test_prune_keeps_newest_count_even_when_all_are_old(
    session: AsyncSession,
) -> None:
    """Age alone must not empty the log: KEEP_ARCHIVED_STORE_VERSIONS wins."""
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="v1")
    for i in range(2, 12):
        await update_draft_category(
            session, (await get_catalog_view(session)).categories[0].id, title_fa=f"حجمی {i}"
        )
        await commit_and_activate(session, notes=f"v{i}")

    old = datetime.now(UTC) - timedelta(days=STORE_CHANGE_RETENTION_DAYS + 3)
    rows = list((await session.execute(select(StoreVersion))).scalars().all())
    for v in rows:
        if v.status == StoreVersionStatus.archived:
            v.published_at = old
            v.updated_at = old
    await session.flush()

    removed = await prune_old_rows(session, retention_days=90)
    archived = list(
        (
            await session.execute(
                select(StoreVersion)
                .where(StoreVersion.status == StoreVersionStatus.archived)
                .order_by(StoreVersion.version.desc())
            )
        )
        .scalars()
        .all()
    )
    assert removed["store_versions"] == 10 - KEEP_ARCHIVED_STORE_VERSIONS
    assert len(archived) == KEEP_ARCHIVED_STORE_VERSIONS


@pytest.mark.asyncio
async def test_prune_keeps_young_archived_beyond_count_cap(
    session: AsyncSession,
) -> None:
    """Younger than retention must survive even past KEEP_ARCHIVED_STORE_VERSIONS."""
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="v1")
    for i in range(2, 10):
        await update_draft_category(
            session, (await get_catalog_view(session)).categories[0].id, title_fa=f"حجمی {i}"
        )
        await commit_and_activate(session, notes=f"v{i}")

    removed = await prune_old_rows(session, retention_days=90)
    assert removed.get("store_versions", 0) == 0
    archived_n = await session.scalar(
        select(func.count())
        .select_from(StoreVersion)
        .where(StoreVersion.status == StoreVersionStatus.archived)
    )
    assert int(archived_n or 0) > KEEP_ARCHIVED_STORE_VERSIONS


# --------------------------------------------------------------------------- #
# Panel group resolution (via category writes + resolve)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_panel_groups_child_inherits_parent_override_and_empty_when_unset(
    session: AsyncSession,
) -> None:
    parent = await create_draft_category(session, title_fa="حجمی", kind="volume")
    child = await create_draft_category(
        session, title_fa="ویژه", kind="volume", parent_id=parent.id
    )
    await update_draft_category(session, parent.id, panel_group_ids="7,8")
    await session.commit()
    settings = _settings(defaults=[900])

    assert await resolve_groups_for_plan_type(
        session, settings, PlanType.VOLUME, store_category_id=child.id
    ) == [7, 8]

    # Child override wins over inherited parent.
    await update_draft_category(session, child.id, panel_group_ids="11")
    await session.commit()
    assert await resolve_groups_for_plan_type(
        session, settings, PlanType.VOLUME, store_category_id=child.id
    ) == [11]

    # Clear child → inherit again; clear parent too → nothing (not for sale).
    await update_draft_category(session, child.id, panel_group_ids="")
    await session.commit()
    assert await resolve_groups_for_plan_type(
        session, settings, PlanType.VOLUME, store_category_id=child.id
    ) == [7, 8]

    await update_draft_category(session, parent.id, panel_group_ids="")
    await session.commit()
    assert (
        await resolve_groups_for_plan_type(
            session, settings, PlanType.VOLUME, store_category_id=child.id
        )
        == []
    )


# --------------------------------------------------------------------------- #
# has_time_limit derivation + idempotent mutations
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_has_time_limit_derived_from_duration_not_manual_wish(
    session: AsyncSession,
) -> None:
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    with_expiry = await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="با انقضا",
        fixed_price_toman=100_000,
        volume_gb=50,
        duration_days=30,
    )
    no_expiry = await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="بدون انقضا",
        fixed_price_toman=200_000,
        volume_gb=50,
    )
    assert package_has_time_limit(with_expiry) is True
    assert package_has_time_limit(no_expiry) is False

    await update_draft_package(session, with_expiry.id, clear_duration=True)
    assert package_has_time_limit(with_expiry) is False
    assert with_expiry.options.get("duration_days") is None

    await update_draft_package(session, no_expiry.id, duration_days=60)
    assert package_has_time_limit(no_expiry) is True


@pytest.mark.asyncio
async def test_commit_and_activate_twice_is_noop_second_time(
    session: AsyncSession,
) -> None:
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    first = await commit_and_activate(session, notes="یکبار")
    assert first is not None
    count = await _version_count(session)
    second = await commit_and_activate(session, notes="دوباره")
    assert second is None
    assert await _version_count(session) == count


@pytest.mark.asyncio
async def test_get_or_create_draft_twice_does_not_fork(
    session: AsyncSession,
) -> None:
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="زنده")
    a = await get_or_create_draft(session)
    await update_draft_category(session, a.categories[0].id, title_fa="حجمی ۲")
    b = await get_or_create_draft(session)
    assert a.id == b.id
    assert (await get_draft_category(session, a.categories[0].id)).title_fa == "حجمی ۲"


@pytest.mark.asyncio
async def test_slug_index_covers_archived_ids_after_many_publishes(
    session: AsyncSession,
) -> None:
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="۱")
    ids: list[int] = []
    for i in range(3):
        view = await get_catalog_view(session)
        ids.append(view.categories[0].id)
        await update_draft_category(session, ids[-1], title_fa=f"حجمی {i}")
        await commit_and_activate(session, notes=f"ویرایش {i}")

    view = await get_catalog_view(session)
    for old_id in ids:
        assert view.category(old_id) is not None
        assert view.category(old_id).slug == view.categories[0].slug

"""STORE_ADMIN: draft clone, publish archive, owner gate, updates."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from bot.config import Settings
from bot.keyboards.admin import AdminCB
from bot.keyboards.store_admin import StoreCB, store_hub_keyboard
from database.base import Base
from database.models.store import StoreVersion, StoreVersionStatus
from services.store_admin import (
    StoreAdminError,
    StoreOwnerRequired,
    assert_store_owner,
    format_preview_fa,
    get_or_create_draft,
    get_store_overview,
    list_draft_categories,
    preview_draft,
    publish_draft,
    update_draft_category,
    update_draft_package,
)
from services.store_config import (
    get_published_store_config,
    is_store_owner,
    seed_store_from_pricing_json,
)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def pricing_path(tmp_path: Path) -> Path:
    src = Path("config/pricing.json")
    path = tmp_path / "pricing.json"
    path.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def test_owner_gate_helper() -> None:
    settings = Settings(
        BOT_TOKEN="t",
        STORE_OWNER_TELEGRAM_IDS="100",
        ADMIN_TELEGRAM_IDS="200",
    )
    assert is_store_owner(settings, 100) is True
    assert is_store_owner(settings, 200) is False
    assert_store_owner(settings, 100)
    with pytest.raises(StoreOwnerRequired):
        assert_store_owner(settings, 200)


@pytest.mark.asyncio
async def test_draft_clones_from_published(session: AsyncSession, pricing_path: Path) -> None:
    seeded = await seed_store_from_pricing_json(session, pricing_path)
    assert seeded is not None
    await session.commit()

    ov = await get_store_overview(session)
    assert ov.has_published and not ov.has_draft

    draft = await get_or_create_draft(session)
    await session.commit()
    assert draft.status == StoreVersionStatus.draft
    assert draft.version == seeded.version + 1
    assert len(draft.categories) == 2
    assert len(draft.packages) == 2
    # published still intact
    pub = await get_published_store_config(session)
    assert pub is not None
    assert pub.version.id == seeded.id


@pytest.mark.asyncio
async def test_publish_archives_old(session: AsyncSession, pricing_path: Path) -> None:
    seeded = await seed_store_from_pricing_json(session, pricing_path)
    assert seeded is not None
    await session.commit()
    old_id = seeded.id

    draft = await get_or_create_draft(session)
    cats = await list_draft_categories(session)
    await update_draft_category(session, cats[0].id, title_fa="حجمی ویژه")
    published = await publish_draft(session, notes="admin publish")
    await session.commit()

    assert published.status == StoreVersionStatus.published
    assert published.id == draft.id

    old = await session.get(StoreVersion, old_id)
    assert old is not None
    assert old.status == StoreVersionStatus.archived

    versions = (await session.execute(select(StoreVersion))).scalars().all()
    published_rows = [v for v in versions if v.status == StoreVersionStatus.published]
    assert len(published_rows) == 1

    cfg = await get_published_store_config(session)
    assert cfg is not None
    titles = {c.title_fa for c in cfg.categories}
    assert "حجمی ویژه" in titles


@pytest.mark.asyncio
async def test_update_package_and_locked_rules(session: AsyncSession, pricing_path: Path) -> None:
    await seed_store_from_pricing_json(session, pricing_path)
    await session.commit()
    draft = await get_or_create_draft(session)
    # custom template — cannot set fixed price
    pkg = draft.packages[0]
    with pytest.raises(StoreAdminError):
        await update_draft_package(session, pkg.id, fixed_price_toman=1000)

    await update_draft_package(session, pkg.id, title_fa="عنوان جدید", allow_renew=False)
    await session.commit()
    draft2 = await get_or_create_draft(session)
    updated = next(p for p in draft2.packages if p.id == pkg.id)
    assert updated.title_fa == "عنوان جدید"
    assert updated.allow_renew is False
    assert draft2.sales_rules["payment_correctness_locked"] is True
    assert draft2.sales_rules["service_ownership_locked"] is True


@pytest.mark.asyncio
async def test_preview_persian_summary(session: AsyncSession, pricing_path: Path) -> None:
    await seed_store_from_pricing_json(session, pricing_path)
    await session.commit()
    preview = await preview_draft(session)
    await session.commit()
    text = format_preview_fa(preview)
    assert "پیش‌نمایش" in text
    assert "حجمی" in text or "volume" in text.lower()
    assert "مراحل خرید" in text


def test_store_hub_keyboard_owner_vs_ops() -> None:
    owner_kb = store_hub_keyboard(is_owner=True, has_categories=False)
    owner_cbs = [b.callback_data for row in owner_kb.inline_keyboard for b in row]
    assert StoreCB.SEED in owner_cbs
    assert AdminCB.HOME in owner_cbs

    ops_kb = store_hub_keyboard(is_owner=False, has_categories=True, groups=[(7, "حجمی")])
    ops_cbs = [b.callback_data for row in ops_kb.inline_keyboard for b in row]
    assert StoreCB.SEED not in ops_cbs
    # The groups are the hub's own buttons now; there is no «گروه‌های محصول» page.
    assert f"{StoreCB.CAT}7" in ops_cbs
    assert StoreCB.PREVIEW in ops_cbs


def test_the_hub_no_longer_asks_the_owner_to_publish() -> None:
    """«این دردسر و اضافه‌کاری پیش‌نویس و انتشار دیگه نباشه».

    Every edit now ends in its own confirm and goes live there, so a hub that
    still offered «انتشار» would be offering a step that does nothing — the
    inert control CLAUDE.md rejects.
    """
    for has_categories in (False, True):
        kb = store_hub_keyboard(is_owner=True, has_categories=has_categories)
        cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
        labels = [b.text for row in kb.inline_keyboard for b in row]
        assert not any("انتشار" in t or "پیش‌نویس" in t for t in labels)
        # History is the safety net the owner asked to keep — still reachable.
        assert StoreCB.HIST in cbs
        assert StoreCB.ADV not in cbs

    # The constants went with the buttons: an unreachable handler is the same
    # debt as a dead button, so neither may be left behind.
    for gone in ("PUBLISH", "PUBLISH_OK", "DRAFT", "DRAFT_DISCARD", "DRAFT_DISCARD_OK"):
        assert not hasattr(StoreCB, gone), f"StoreCB.{gone} outlived its buttons"


def test_store_callback_prefixes_short() -> None:
    for name in dir(StoreCB):
        if name.startswith("_"):
            continue
        val = getattr(StoreCB, name)
        if isinstance(val, str):
            assert len(val) <= 64, name

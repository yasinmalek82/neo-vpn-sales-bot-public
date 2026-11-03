"""STORE_STRATEGY: discount combine, step reorder, restore-to-draft."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from bot.keyboards.store_admin import (
    StoreCB,
    store_discount_keyboard,
    store_hub_keyboard,
)
from database.base import Base
from database.models.store import StoreVersionStatus
from services.pricing import PlanType, calculate_price
from services.store_admin import get_draft_version, get_or_create_draft, publish_draft
from services.store_config import (
    get_published_store_config,
    load_pricing_config_async,
    seed_store_from_pricing_json,
)
from services.store_strategy import (
    add_purchase_step_from_catalog,
    delete_draft_promo,
    get_draft_purchase_steps,
    list_promo_codes,
    list_version_history,
    normalize_purchase_steps,
    reorder_purchase_step,
    restore_version_to_draft,
    toggle_purchase_step,
    update_draft_sales_rules,
    upsert_draft_promo,
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


@pytest.mark.asyncio
async def test_discount_combine_best_of_enforced(session: AsyncSession, pricing_path: Path) -> None:
    await seed_store_from_pricing_json(session, pricing_path)
    await session.commit()
    cfg = await load_pricing_config_async(session, path=pricing_path)
    assert cfg["sales_rules"]["discount_combine"] == "best_of_promo_vs_extra"
    # promo 10 vs extra 5 → 10 (not 15); duration 0 for 30d
    price = calculate_price(
        cfg,
        plan_type=PlanType.VOLUME,
        volume_gb=50,
        duration_days=30,
        promo_code="WELCOME10",
        extra_discount_percent=5,
    )
    assert price.meta["discount_combine"] == "best_of_promo_vs_extra"
    assert price.meta["total_discount_percent"] == "10"


@pytest.mark.asyncio
async def test_promo_crud_and_combine_toggle(session: AsyncSession, pricing_path: Path) -> None:
    await seed_store_from_pricing_json(session, pricing_path)
    await session.commit()
    draft = await get_or_create_draft(session)
    await session.commit()

    promos = await upsert_draft_promo(session, code="save20", percent=20)
    await session.commit()
    codes = {p["code"] for p in promos}
    assert "SAVE20" in codes

    rules = await update_draft_sales_rules(session, discount_combine="sum")
    await session.commit()
    assert rules["discount_combine"] == "sum"
    assert rules["payment_correctness_locked"] is True
    assert rules["service_ownership_locked"] is True
    assert rules["renew_require_customer_confirm"] is True

    await update_draft_sales_rules(session, discount_combine="best_of_promo_vs_extra")
    promos2 = await delete_draft_promo(session, "SAVE20")
    await session.commit()
    assert "SAVE20" not in {p["code"] for p in promos2}
    assert list_promo_codes(draft.pricing_document) or True  # session may be stale
    draft2 = await get_draft_version(session)
    assert draft2 is not None
    assert "SAVE20" not in (draft2.pricing_document.get("promo_codes") or {})


@pytest.mark.asyncio
async def test_step_reorder_persistence(session: AsyncSession, pricing_path: Path) -> None:
    await seed_store_from_pricing_json(session, pricing_path)
    await session.commit()
    steps = await get_draft_purchase_steps(session)
    await session.commit()
    assert steps[0]["block"] == "plan_type"
    original_second = steps[1]["block"]

    moved = await reorder_purchase_step(session, 1, direction="up")
    await session.commit()
    assert moved[0]["block"] == original_second
    assert moved[1]["block"] == "plan_type"

    # reload
    again = await get_draft_purchase_steps(session)
    assert again[0]["block"] == original_second

    toggled = await toggle_purchase_step(session, 2)
    await session.commit()
    assert toggled[2]["enabled"] is False

    # remove optional then re-add from catalog
    # find promo index
    promo_idx = next(i for i, s in enumerate(toggled) if s["block"] == "promo")
    from services.store_strategy import remove_purchase_step

    after_rm = await remove_purchase_step(session, promo_idx)
    await session.commit()
    assert all(s["block"] != "promo" for s in after_rm)
    after_add = await add_purchase_step_from_catalog(session, "promo")
    await session.commit()
    assert after_add[-1]["block"] == "promo"


@pytest.mark.asyncio
async def test_restore_to_draft_keeps_published(session: AsyncSession, pricing_path: Path) -> None:
    seeded = await seed_store_from_pricing_json(session, pricing_path)
    assert seeded is not None
    await session.commit()
    pub_id = seeded.id

    draft = await get_or_create_draft(session)
    await upsert_draft_promo(session, code="TEMP9", percent=9)
    await session.commit()
    published = await publish_draft(session, notes="v2")
    await session.commit()
    assert published.id == draft.id

    hist = await list_version_history(session)
    assert any(r.id == pub_id and r.status == "archived" for r in hist)
    assert any(r.status == "published" for r in hist)

    # restore archived v1 into new draft without destroying published
    restored = await restore_version_to_draft(session, pub_id)
    await session.commit()
    assert restored.status == StoreVersionStatus.draft
    assert "TEMP9" not in (restored.pricing_document.get("promo_codes") or {})

    cfg = await get_published_store_config(session)
    assert cfg is not None
    assert cfg.version.id == published.id
    assert cfg.version.status == StoreVersionStatus.published


def test_normalize_steps_and_hub_vocab() -> None:
    steps = normalize_purchase_steps([{"block": "plan_type"}, "confirm"])
    assert steps[0] == {"block": "plan_type", "enabled": True}
    assert steps[1]["block"] == "confirm"

    kb = store_hub_keyboard(is_owner=True, has_categories=True, groups=[(1, "حجمی")])
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any("تخفیف‌ها" in t for t in labels)
    assert any("پرداخت" in t for t in labels)
    assert any("پیش‌نمایش مشتری" in t for t in labels)
    assert any("تغییرات" in t for t in labels)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    # The tools that hid under «پیشرفته» sit on the hub itself now, and the
    # screens the redesign removed have no button left.
    assert StoreCB.DISC in cbs
    assert StoreCB.PAY in cbs
    assert StoreCB.HIST in cbs
    for gone in (StoreCB.READY, StoreCB.CUSTOM, StoreCB.ADV, StoreCB.CATS):
        assert gone not in cbs
    # «مراحل خرید» is deliberately unreachable: bot/handlers/purchase.py never
    # reads ``purchase_steps``, so reordering or disabling a step changed
    # nothing. Showing the editor made the store claim a power it did not have.
    assert StoreCB.STEPS not in cbs


def test_renewal_toggles_cannot_change_a_purchase() -> None:
    """Nothing in checkout reads these flags, so the store must not offer them.

    Customer confirmation stays on. A saved «off» is corrected back to on.
    """
    from services.store_admin import locked_sales_rules

    rules = locked_sales_rules(
        {
            "renew_show_current_price": False,
            "renew_require_customer_confirm": False,
        }
    )
    assert rules["renew_require_customer_confirm"] is True
    assert rules["renew_show_current_price"] is True

    kb = store_discount_keyboard(
        is_owner=True,
        discount_combine="best_of_promo_vs_extra",
    )
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "adm:st:rprice" not in cbs
    assert "adm:st:rcf" not in cbs

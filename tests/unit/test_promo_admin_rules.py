"""M3b: the store pricing document's promo rules, edited as a draft and published.

The four keys ride on the code's entry. Every editor that rewrites an entry must keep
them: the «غیرفعال» toggle once wrote only ``active`` and a re-save rebuilt the dict, so a
rule the owner set would have vanished the next time he touched the code.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from database.base import Base
from database.models import Order, OrderStatus, Plan, PromoRedemption, User
from database.models.plan import DataLimitResetStrategy
from services.promo_redemptions import live_usage_by_code
from services.promo_rules import PromoRules
from services.store_admin import (
    StoreAdminError,
    get_draft_version,
    get_or_create_draft,
    publish_draft,
)
from services.store_config import load_pricing_config_async, seed_store_from_pricing_json
from services.store_strategy import (
    list_promo_codes,
    toggle_draft_promo_active,
    upsert_draft_promo,
)

EXPIRES = datetime(2026, 12, 1, 9, 30, tzinfo=UTC)
FULL = PromoRules(first_purchase_only=True, per_user_limit=1, total_limit=50, expires_at=EXPIRES)


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
async def seeded(session: AsyncSession, tmp_path: Path) -> AsyncSession:
    path = tmp_path / "pricing.json"
    path.write_text(Path("config/pricing.json").read_text(encoding="utf-8"), encoding="utf-8")
    await seed_store_from_pricing_json(session, path)
    await session.commit()
    await get_or_create_draft(session)
    await session.commit()
    return session


def _row(promos: list[dict[str, object]], code: str) -> dict[str, object]:
    return next(p for p in promos if p["code"] == code)


async def _draft_entry(session: AsyncSession, code: str) -> object:
    draft = await get_draft_version(session)
    assert draft is not None
    return (draft.pricing_document.get("promo_codes") or {})[code]


def test_the_list_reads_the_four_keys_and_defaults_the_old_shapes() -> None:
    doc = {
        "promo_codes": {
            "OLD": "10",
            "MID": {"percent": 12, "active": True},
            "NEW": {
                "percent": 20,
                "active": True,
                "first_purchase_only": True,
                "per_user_limit": 2,
                "total_limit": 9,
                "expires_at": EXPIRES.isoformat(),
            },
        }
    }
    rows = {p["code"]: p for p in list_promo_codes(doc)}
    for plain in ("OLD", "MID"):
        assert rows[plain]["first_purchase_only"] is False
        assert (rows[plain]["per_user_limit"], rows[plain]["total_limit"]) == (0, 0)
        assert rows[plain]["expires_at"] is None
    assert rows["NEW"]["first_purchase_only"] is True
    assert (rows["NEW"]["per_user_limit"], rows["NEW"]["total_limit"]) == (2, 9)
    assert rows["NEW"]["expires_at"] == EXPIRES


@pytest.mark.asyncio
async def test_a_code_saved_with_rules_keeps_them_in_the_draft(seeded: AsyncSession) -> None:
    promos = await upsert_draft_promo(seeded, code="vip", percent=20, rules=FULL)
    row = _row(promos, "VIP")
    assert row["first_purchase_only"] is True
    assert (row["per_user_limit"], row["total_limit"]) == (1, 50)
    assert row["expires_at"] == EXPIRES
    entry = await _draft_entry(seeded, "VIP")
    assert isinstance(entry, dict)
    assert entry["expires_at"] == EXPIRES.isoformat()
    assert entry["per_user_limit"] == 1 and entry["total_limit"] == 50
    assert entry["first_purchase_only"] is True


@pytest.mark.asyncio
async def test_the_toggle_keeps_all_four_rules_both_ways(seeded: AsyncSession) -> None:
    await upsert_draft_promo(seeded, code="vip", percent=20, rules=FULL)
    off = _row(await toggle_draft_promo_active(seeded, "VIP"), "VIP")
    assert off["active"] is False
    assert off["first_purchase_only"] is True and off["total_limit"] == 50
    assert off["per_user_limit"] == 1 and off["expires_at"] == EXPIRES
    on = _row(await toggle_draft_promo_active(seeded, "VIP"), "VIP")
    assert on["active"] is True
    assert on["first_purchase_only"] is True and on["total_limit"] == 50
    assert on["per_user_limit"] == 1 and on["expires_at"] == EXPIRES


@pytest.mark.asyncio
async def test_saving_a_code_again_without_rules_keeps_the_rules_it_had(
    seeded: AsyncSession,
) -> None:
    await upsert_draft_promo(seeded, code="vip", percent=20, rules=FULL)
    again = _row(await upsert_draft_promo(seeded, code="VIP", percent=25), "VIP")
    assert again["percent"] == 25
    assert again["total_limit"] == 50 and again["first_purchase_only"] is True
    assert again["expires_at"] == EXPIRES


@pytest.mark.asyncio
async def test_saving_with_new_rules_replaces_them_and_default_rules_clear_them(
    seeded: AsyncSession,
) -> None:
    await upsert_draft_promo(seeded, code="vip", percent=20, rules=FULL)
    changed = _row(
        await upsert_draft_promo(seeded, code="vip", percent=20, rules=PromoRules(total_limit=5)),
        "VIP",
    )
    assert changed["total_limit"] == 5 and changed["per_user_limit"] == 0
    assert changed["first_purchase_only"] is False and changed["expires_at"] is None
    cleared = _row(
        await upsert_draft_promo(seeded, code="vip", percent=20, rules=PromoRules()), "VIP"
    )
    assert cleared["total_limit"] == 0 and cleared["expires_at"] is None


@pytest.mark.asyncio
async def test_the_toggle_on_a_bare_percent_entry_still_works(seeded: AsyncSession) -> None:
    draft = await get_or_create_draft(seeded)
    doc = dict(draft.pricing_document)
    doc["promo_codes"] = {**(doc.get("promo_codes") or {}), "BARE": "10"}
    draft.pricing_document = doc
    await seeded.flush()
    row = _row(await toggle_draft_promo_active(seeded, "BARE"), "BARE")
    assert row["active"] is False and row["total_limit"] == 0
    assert row["percent"] == "10"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        PromoRules(per_user_limit=-1),
        PromoRules(total_limit=-5),
        PromoRules(expires_at=datetime(2026, 12, 1, 9, 30)),  # no timezone
    ],
)
async def test_a_malformed_rule_can_never_be_stored(seeded: AsyncSession, bad: PromoRules) -> None:
    with pytest.raises(StoreAdminError):
        await upsert_draft_promo(seeded, code="bad", percent=10, rules=bad)
    draft = await get_draft_version(seeded)
    assert draft is not None
    assert "BAD" not in (draft.pricing_document.get("promo_codes") or {})


@pytest.mark.asyncio
async def test_publishing_keeps_the_rules_and_the_usage_counts_key_on_the_code(
    seeded: AsyncSession,
) -> None:
    await upsert_draft_promo(seeded, code="vip", percent=20, rules=FULL)
    await publish_draft(seeded, notes="promo with rules")
    await seeded.commit()

    user = User(telegram_id=9901, referral_code="P9901")
    plan = Plan(
        name="p",
        description="t",
        data_limit_gb=10,
        duration_days=30,
        price_toman=1000,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1],
        hwid_limit=0,
        is_active=True,
    )
    seeded.add_all([user, plan])
    await seeded.flush()
    order = Order(
        user_id=user.id, plan_id=plan.id, status=OrderStatus.PENDING_PAYMENT, price_toman=1000
    )
    seeded.add(order)
    await seeded.flush()
    seeded.add(PromoRedemption(code="VIP", user_id=user.id, order_id=order.id))
    await seeded.commit()
    assert await live_usage_by_code(seeded) == {"VIP": 1}

    # Editing and publishing another version mints new row ids for the catalog; the usage
    # still belongs to the code string.
    await upsert_draft_promo(seeded, code="other", percent=5)
    await publish_draft(seeded, notes="another promo")
    await seeded.commit()
    assert await live_usage_by_code(seeded) == {"VIP": 1}

    live = await load_pricing_config_async(seeded)
    entry = live["promo_codes"]["VIP"]
    assert entry["total_limit"] == 50 and entry["per_user_limit"] == 1
    assert entry["expires_at"] == EXPIRES.isoformat()


@pytest.mark.asyncio
async def test_usage_counts_only_live_orders(seeded: AsyncSession) -> None:
    user = User(telegram_id=9902, referral_code="P9902")
    plan = Plan(
        name="p2",
        description="t",
        data_limit_gb=10,
        duration_days=30,
        price_toman=1000,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1],
        hwid_limit=0,
        is_active=True,
    )
    seeded.add_all([user, plan])
    await seeded.flush()
    for status in (OrderStatus.ACTIVE, OrderStatus.EXPIRED, OrderStatus.REJECTED):
        order = Order(user_id=user.id, plan_id=plan.id, status=status, price_toman=1000)
        seeded.add(order)
        await seeded.flush()
        seeded.add(PromoRedemption(code="VIP", user_id=user.id, order_id=order.id))
    await seeded.commit()
    assert await live_usage_by_code(seeded) == {"VIP": 1}

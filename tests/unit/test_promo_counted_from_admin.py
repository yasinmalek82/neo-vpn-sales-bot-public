"""M3b: ``counted_from`` is stamped when a code is created and kept by every later edit.

Deleting a code and creating it again starts its count from zero; editing a code that exists
(percent, rules through the wizard, the on/off toggle) keeps it; publishing keeps it; an
entry that has no stamp stays without one.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from database.base import Base
from database.models import Order, OrderStatus, Plan, PromoRedemption, User
from database.models.plan import DataLimitResetStrategy
from services.promo_redemptions import live_usage_by_code
from services.promo_rules import PromoRules
from services.store_admin import get_draft_version, get_or_create_draft, publish_draft
from services.store_config import load_pricing_config_async, seed_store_from_pricing_json
from services.store_strategy import (
    delete_draft_promo,
    list_promo_codes,
    toggle_draft_promo_active,
    upsert_draft_promo,
)

OLD_STAMP = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
EXPIRES = datetime(2026, 12, 1, 9, 30, tzinfo=UTC)
FULL = PromoRules(first_purchase_only=True, per_user_limit=1, total_limit=50, expires_at=EXPIRES)


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
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


async def _draft_entry(session: AsyncSession, code: str) -> object:
    draft = await get_draft_version(session)
    assert draft is not None
    return (draft.pricing_document.get("promo_codes") or {})[code]


async def _set_entry(session: AsyncSession, code: str, entry: object) -> None:
    """Put an entry in the draft as a hand-edited or older document would hold it."""
    draft = await get_draft_version(session)
    assert draft is not None
    doc = copy.deepcopy(draft.pricing_document)
    promos = dict(doc.get("promo_codes") or {})
    promos[code] = entry
    doc["promo_codes"] = promos
    draft.pricing_document = doc
    await session.flush()


async def _stamp(session: AsyncSession, code: str) -> datetime | None:
    entry = await _draft_entry(session, code)
    assert isinstance(entry, dict)
    raw = entry.get("counted_from")
    return datetime.fromisoformat(raw) if raw else None


async def _backdated(session: AsyncSession, code: str = "VIP") -> None:
    """A code created long ago: ``VIP``, 20 percent, with a stamp in January."""
    await upsert_draft_promo(session, code=code, percent=20)
    entry = await _draft_entry(session, code)
    assert isinstance(entry, dict)
    await _set_entry(session, code, {**entry, "counted_from": OLD_STAMP.isoformat()})


@pytest.mark.asyncio
async def test_a_new_code_is_stamped_with_the_moment_it_was_created(seeded: AsyncSession) -> None:
    before = datetime.now(UTC)
    await upsert_draft_promo(seeded, code="fresh", percent=10)
    await upsert_draft_promo(seeded, code="ruled", percent=10, rules=FULL)
    after = datetime.now(UTC)
    for code in ("FRESH", "RULED"):
        stamp = await _stamp(seeded, code)
        assert stamp is not None and stamp.tzinfo is not None
        assert before <= stamp <= after, (code, stamp)


@pytest.mark.asyncio
async def test_the_list_carries_the_stamp(seeded: AsyncSession) -> None:
    await _backdated(seeded)
    draft = await get_draft_version(seeded)
    assert draft is not None
    row = next(p for p in list_promo_codes(draft.pricing_document) if p["code"] == "VIP")
    assert row["counted_from"] == OLD_STAMP


@pytest.mark.asyncio
async def test_editing_the_percent_keeps_the_stamp(seeded: AsyncSession) -> None:
    await _backdated(seeded)
    await upsert_draft_promo(seeded, code="vip", percent=35)
    assert await _stamp(seeded, "VIP") == OLD_STAMP
    entry = await _draft_entry(seeded, "VIP")
    assert isinstance(entry, dict) and entry["percent"] == 35.0


@pytest.mark.asyncio
async def test_replacing_the_rules_through_the_wizard_path_keeps_the_stamp(
    seeded: AsyncSession,
) -> None:
    await _backdated(seeded)
    await upsert_draft_promo(seeded, code="VIP", percent=20, rules=FULL)
    assert await _stamp(seeded, "VIP") == OLD_STAMP
    # the default rules clear the limits, and still the code is the same code
    await upsert_draft_promo(seeded, code="VIP", percent=20, rules=PromoRules())
    assert await _stamp(seeded, "VIP") == OLD_STAMP
    entry = await _draft_entry(seeded, "VIP")
    assert isinstance(entry, dict) and "total_limit" not in entry


@pytest.mark.asyncio
async def test_the_toggle_keeps_the_stamp_both_ways(seeded: AsyncSession) -> None:
    await _backdated(seeded)
    await toggle_draft_promo_active(seeded, "VIP")
    assert await _stamp(seeded, "VIP") == OLD_STAMP
    await toggle_draft_promo_active(seeded, "VIP")
    assert await _stamp(seeded, "VIP") == OLD_STAMP


@pytest.mark.asyncio
async def test_deleting_and_creating_again_restamps_the_code(seeded: AsyncSession) -> None:
    await _backdated(seeded)
    await delete_draft_promo(seeded, "VIP")
    before = datetime.now(UTC)
    await upsert_draft_promo(seeded, code="VIP", percent=20)
    stamp = await _stamp(seeded, "VIP")
    assert stamp is not None and stamp >= before, stamp


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["bare", "dict"])
async def test_an_entry_that_has_no_stamp_stays_without_one_after_every_edit(
    seeded: AsyncSession, shape: str
) -> None:
    entry: object = "10" if shape == "bare" else {"percent": "10", "active": True}
    await _set_entry(seeded, "OLD", entry)
    await upsert_draft_promo(seeded, code="OLD", percent=12)
    await upsert_draft_promo(seeded, code="OLD", percent=12, rules=FULL)
    await toggle_draft_promo_active(seeded, "OLD")
    await toggle_draft_promo_active(seeded, "OLD")
    now = await _draft_entry(seeded, "OLD")
    assert isinstance(now, dict) and "counted_from" not in now, now
    assert await _stamp(seeded, "OLD") is None


@pytest.mark.asyncio
async def test_publishing_keeps_the_stamp_and_the_count_that_goes_with_it(
    seeded: AsyncSession,
) -> None:
    await _backdated(seeded)
    await upsert_draft_promo(seeded, code="VIP", percent=20, rules=FULL)
    user = User(telegram_id=9951, referral_code="P9951")
    plan = Plan(
        name="stamp-plan",
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
    for created in (OLD_STAMP - timedelta(days=1), OLD_STAMP + timedelta(days=1)):
        order = Order(user_id=user.id, plan_id=plan.id, status=OrderStatus.ACTIVE, price_toman=1000)
        seeded.add(order)
        await seeded.flush()
        seeded.add(
            PromoRedemption(code="VIP", user_id=user.id, order_id=order.id, created_at=created)
        )
    await seeded.commit()

    await upsert_draft_promo(seeded, code="other", percent=5)  # another edit, a new version
    await publish_draft(seeded, notes="stamp survives")
    await seeded.commit()
    live = await load_pricing_config_async(seeded)
    assert live["promo_codes"]["VIP"]["counted_from"] == OLD_STAMP.isoformat()

    since = {row["code"]: row["counted_from"] for row in list_promo_codes(live)}
    assert await live_usage_by_code(seeded, since=since) == {"VIP": 1}


@pytest.mark.asyncio
async def test_a_deleted_and_recreated_code_shows_none_of_the_old_uses_in_the_list(
    seeded: AsyncSession,
) -> None:
    await upsert_draft_promo(seeded, code="WELCOME10", percent=10, rules=FULL)
    user = User(telegram_id=9952, referral_code="P9952")
    plan = Plan(
        name="stamp-plan-2",
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
    await asyncio.sleep(0.01)
    order = Order(user_id=user.id, plan_id=plan.id, status=OrderStatus.ACTIVE, price_toman=1000)
    seeded.add(order)
    await seeded.flush()
    seeded.add(PromoRedemption(code="WELCOME10", user_id=user.id, order_id=order.id))
    await seeded.commit()

    def since_of(doc: dict[str, object]) -> dict[str, datetime | None]:
        return {row["code"]: row["counted_from"] for row in list_promo_codes(doc)}

    draft = await get_draft_version(seeded)
    assert draft is not None
    assert await live_usage_by_code(seeded, since=since_of(draft.pricing_document)) == {
        "WELCOME10": 1
    }

    await delete_draft_promo(seeded, "WELCOME10")
    await asyncio.sleep(0.01)
    await upsert_draft_promo(seeded, code="WELCOME10", percent=10, rules=FULL)
    draft = await get_draft_version(seeded)
    assert draft is not None
    assert await live_usage_by_code(seeded, since=since_of(draft.pricing_document)) == {}

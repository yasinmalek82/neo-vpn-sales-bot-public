"""M3b: a code counts its uses from the moment it was created.

A code that is deleted and created again is a new code: its entry carries ``counted_from``
and only the redemptions written at or after that moment count, at the quote, at the till and
in the admin list. An entry with no ``counted_from`` (every code made before this, the bare
shape) counts everything, as it always did.

Old rows are written with an explicit ``created_at`` before the stamp; every assertion is on
what a customer is told or on the rows that exist, never on how the count is built.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from bot.handlers import purchase
from bot.handlers.store_admin import _promo_body
from database.base import Base
from database.models import Order, OrderStatus, PromoRedemption, User
from services.orders import create_pending_order
from services.pricing import PriceResult
from services.promo_redemptions import live_usage_by_code
from services.promo_rules import PromoUsedUp, PromoUserLimit
from services.store_strategy import list_promo_codes
from services.ux_format import to_persian_digits
from tests.unit.test_customer_purchase_flow import FIXED, _customer, _patch_panel, _settings
from tests.unit.test_promo_rules import (
    CODE,
    _confirm,
    _count,
    _discounted,
    _entry,
    _file,
    _quote,
    _shown,
)
from tests.unit.test_user_offers_checkout import _order, _plan


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.fixture(autouse=True)
def _group(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(purchase, "resolve_groups_for_plan_type", AsyncMock(return_value=[1]))


NOW = datetime.now(UTC)
STAMP = NOW - timedelta(hours=1)  # the code was created an hour ago
BEFORE = NOW - timedelta(hours=2)  # a use of the deleted code with the same name
AFTER = NOW - timedelta(minutes=10)  # a use of the new code


async def _redeem_at(
    session: AsyncSession,
    user: User,
    created_at: datetime,
    status: OrderStatus = OrderStatus.ACTIVE,
) -> Order:
    order = await _order(session, user, status)
    session.add(
        PromoRedemption(code=CODE, user_id=user.id, order_id=order.id, created_at=created_at)
    )
    await session.commit()
    return order


def _stamped(**rules: object) -> dict[str, object]:
    return _entry(counted_from=STAMP.isoformat(), **rules)


# --- the total limit -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_recreated_code_is_open_to_the_next_customer_at_the_quote_and_the_till(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_panel(monkeypatch)
    old_user = await _customer(session, balance=0, telegram_id=8301)
    buyer = await _customer(session, balance=0, telegram_id=8302)
    await _redeem_at(session, old_user, BEFORE)

    # The same entry without the stamp is the old code: used up. This is what the stamp changes.
    with pytest.raises(PromoUsedUp):
        await _quote(session, tmp_path, buyer, _entry(total_limit=1))
    price = await _quote(session, tmp_path, buyer, _stamped(total_limit=1))
    assert price.final_price == _discounted(15)

    settings = _settings(_file(tmp_path, {CODE: _stamped(total_limit=1)}))
    await _confirm(session, settings, buyer, _shown(15))
    orders = (await session.execute(select(Order).where(Order.user_id == buyer.id))).scalars().all()
    rows = (
        (await session.execute(select(PromoRedemption).where(PromoRedemption.user_id == buyer.id)))
        .scalars()
        .all()
    )
    assert len(orders) == 1, "the order was written"
    assert [(r.code, r.order_id) for r in rows] == [(CODE, orders[0].id)]
    assert await _count(session) == 2


@pytest.mark.asyncio
async def test_uses_of_the_new_code_still_count_against_its_total_limit(
    session: AsyncSession, tmp_path: Path
) -> None:
    old_user = await _customer(session, balance=0, telegram_id=8303)
    new_user = await _customer(session, balance=0, telegram_id=8304)
    buyer = await _customer(session, balance=0, telegram_id=8305)
    await _redeem_at(session, old_user, BEFORE)
    await _redeem_at(session, new_user, AFTER)
    with pytest.raises(PromoUsedUp):
        await _quote(session, tmp_path, buyer, _stamped(total_limit=1))
    # one more room, and the old row still does not take it
    assert (await _quote(session, tmp_path, buyer, _stamped(total_limit=2))).final_price == (
        _discounted(15)
    )


@pytest.mark.asyncio
async def test_a_use_written_at_the_very_moment_of_the_stamp_counts(
    session: AsyncSession, tmp_path: Path
) -> None:
    holder = await _customer(session, balance=0, telegram_id=8306)
    buyer = await _customer(session, balance=0, telegram_id=8307)
    await _redeem_at(session, holder, STAMP)
    with pytest.raises(PromoUsedUp):
        await _quote(session, tmp_path, buyer, _stamped(total_limit=1))


# --- the per-user limit ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_customer_who_used_the_deleted_code_may_use_the_recreated_one_once(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_panel(monkeypatch)
    buyer = await _customer(session, balance=0, telegram_id=8311)
    await _redeem_at(session, buyer, BEFORE)

    with pytest.raises(PromoUserLimit):
        await _quote(session, tmp_path, buyer, _entry(per_user_limit=1))
    assert (await _quote(session, tmp_path, buyer, _stamped(per_user_limit=1))).final_price == (
        _discounted(15)
    )

    settings = _settings(_file(tmp_path, {CODE: _stamped(per_user_limit=1)}))
    await _confirm(session, settings, buyer, _shown(15))
    rows = (
        (await session.execute(select(PromoRedemption).where(PromoRedemption.user_id == buyer.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 2, "the old row and the new one"
    assert (await session.execute(select(Order).where(Order.user_id == buyer.id))).scalars().all()

    # The new use counts: the second try on the re-created code is refused.
    with pytest.raises(PromoUserLimit):
        await _quote(session, tmp_path, buyer, _stamped(per_user_limit=1))


# --- an entry with no stamp counts everything ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stamp", [None, "", "not a date", 12345, "2026-13-45"])
async def test_an_entry_without_a_readable_stamp_counts_every_redemption(
    session: AsyncSession, tmp_path: Path, stamp: object
) -> None:
    holder = await _customer(session, balance=0, telegram_id=8321)
    buyer = await _customer(session, balance=0, telegram_id=8322)
    await _redeem_at(session, holder, BEFORE)
    await _redeem_at(session, buyer, BEFORE)
    entry = _entry(total_limit=1, per_user_limit=1)
    if stamp is not None:
        entry["counted_from"] = stamp
    with pytest.raises(PromoUserLimit):
        await _quote(session, tmp_path, buyer, entry)
    fresh = await _customer(session, balance=0, telegram_id=8323)
    with pytest.raises(PromoUsedUp):
        await _quote(session, tmp_path, fresh, entry)


@pytest.mark.asyncio
async def test_a_stamp_in_another_zone_is_the_instant_it_names(
    session: AsyncSession, tmp_path: Path
) -> None:
    holder = await _customer(session, balance=0, telegram_id=8324)
    buyer = await _customer(session, balance=0, telegram_id=8325)
    await _redeem_at(session, holder, NOW - timedelta(hours=2))
    # one hour ago expressed at +03:30: the use two hours ago is before it
    tehran = timezone(timedelta(hours=3, minutes=30))
    stamp = (NOW - timedelta(hours=1)).astimezone(tehran).isoformat()
    entry = _entry(total_limit=1, counted_from=stamp)
    assert (await _quote(session, tmp_path, buyer, entry)).final_price == _discounted(15)
    # and a use after it counts
    await _redeem_at(session, holder, NOW - timedelta(minutes=5))
    with pytest.raises(PromoUsedUp):
        await _quote(session, tmp_path, buyer, entry)


# --- the quote carries the stamp to the order ------------------------------------------------


@pytest.mark.asyncio
async def test_the_quote_carries_the_stamp_as_json_and_none_when_the_entry_has_none(
    session: AsyncSession, tmp_path: Path
) -> None:
    buyer = await _customer(session, balance=0, telegram_id=8331)
    stamped = await _quote(session, tmp_path, buyer, _stamped(total_limit=3))
    bare = await _quote(session, tmp_path, buyer, _entry(total_limit=3))
    sent = json.loads(json.dumps(stamped.meta["promo_redeem"]))
    assert datetime.fromisoformat(sent["counted_from"]) == STAMP
    assert json.loads(json.dumps(bare.meta["promo_redeem"]))["counted_from"] is None


# --- the recount inside the order's transaction ---------------------------------------------


def _price(total: int = 0, per_user: int = 0, counted_from: datetime | None = None) -> PriceResult:
    redeem: dict[str, object] = {
        "code": CODE,
        "per_user_limit": per_user,
        "total_limit": total,
        "counted_from": counted_from.isoformat() if counted_from else None,
    }
    return PriceResult(FIXED, 7_500, 42_500, "IRT", {"promo_redeem": redeem})


@pytest.mark.asyncio
async def test_the_recount_in_the_order_transaction_leaves_out_uses_before_the_stamp(
    session: AsyncSession,
) -> None:
    holder = await _customer(session, balance=0, telegram_id=8341)
    user = await _customer(session, balance=0, telegram_id=8342)
    await _redeem_at(session, holder, BEFORE)
    plan = await _plan(session)
    await session.commit()
    uid = int(user.id)

    # Without a stamp the old row fills the limit, so the order is refused ...
    with pytest.raises(PromoUsedUp):
        await create_pending_order(session, user_id=uid, plan=plan, price=_price(total=1))
    await session.rollback()
    await session.refresh(plan)
    assert (await session.execute(select(Order).where(Order.user_id == uid))).all() == []

    # ... with the stamp it is not, and the redemption is written with the order.
    order = await create_pending_order(
        session, user_id=uid, plan=plan, price=_price(total=1, counted_from=STAMP)
    )
    await session.commit()
    rows = (
        (await session.execute(select(PromoRedemption).where(PromoRedemption.user_id == uid)))
        .scalars()
        .all()
    )
    assert [(r.code, r.order_id) for r in rows] == [(CODE, order.id)]


@pytest.mark.asyncio
async def test_the_recount_still_refuses_when_a_use_after_the_stamp_took_the_room(
    session: AsyncSession,
) -> None:
    holder = await _customer(session, balance=0, telegram_id=8343)
    user = await _customer(session, balance=0, telegram_id=8344)
    await _redeem_at(session, holder, AFTER)
    plan = await _plan(session)
    await session.commit()
    uid = int(user.id)
    with pytest.raises(PromoUsedUp):
        await create_pending_order(
            session, user_id=uid, plan=plan, price=_price(total=1, counted_from=STAMP)
        )


@pytest.mark.asyncio
async def test_the_per_user_recount_leaves_out_this_customers_uses_before_the_stamp(
    session: AsyncSession,
) -> None:
    user = await _customer(session, balance=0, telegram_id=8345)
    await _redeem_at(session, user, BEFORE)
    plan = await _plan(session)
    await session.commit()
    uid = int(user.id)
    with pytest.raises(PromoUserLimit):
        await create_pending_order(session, user_id=uid, plan=plan, price=_price(per_user=1))
    await session.rollback()
    await session.refresh(plan)
    order = await create_pending_order(
        session, user_id=uid, plan=plan, price=_price(per_user=1, counted_from=STAMP)
    )
    assert order.id is not None


# --- the admin list shows the same number ----------------------------------------------------


def _doc(**rules: object) -> dict[str, object]:
    return {"promo_codes": {CODE: _stamped(**rules)}}


@pytest.mark.asyncio
async def test_the_admin_list_shows_uses_since_the_stamp_and_only_those(
    session: AsyncSession,
) -> None:
    a = await _customer(session, balance=0, telegram_id=8351)
    b = await _customer(session, balance=0, telegram_id=8352)
    c = await _customer(session, balance=0, telegram_id=8353)
    await _redeem_at(session, a, BEFORE)
    await _redeem_at(session, b, BEFORE)
    await _redeem_at(session, c, AFTER)

    promos = list_promo_codes(_doc(total_limit=5))
    body = await _promo_body(session, promos, hint="")
    assert f"مصرف: {to_persian_digits(1)} از {to_persian_digits(5)}" in body, body

    # The same code with no stamp is the old code and shows all three.
    old = list_promo_codes({"promo_codes": {CODE: _entry(total_limit=5)}})
    old_body = await _promo_body(session, old, hint="")
    assert f"مصرف: {to_persian_digits(3)} از {to_persian_digits(5)}" in old_body, old_body


@pytest.mark.asyncio
async def test_the_number_in_the_admin_list_is_the_one_that_refuses_a_customer(
    session: AsyncSession, tmp_path: Path
) -> None:
    a = await _customer(session, balance=0, telegram_id=8354)
    buyer = await _customer(session, balance=0, telegram_id=8355)
    await _redeem_at(session, a, BEFORE)
    await _redeem_at(session, a, AFTER)
    body = await _promo_body(session, list_promo_codes(_doc(total_limit=1)), hint="")
    assert f"مصرف: {to_persian_digits(1)} از {to_persian_digits(1)}" in body, body
    with pytest.raises(PromoUsedUp):
        await _quote(session, tmp_path, buyer, _stamped(total_limit=1))


@pytest.mark.asyncio
async def test_usage_by_code_without_a_stamp_is_every_live_use_and_omits_unused_codes(
    session: AsyncSession,
) -> None:
    a = await _customer(session, balance=0, telegram_id=8356)
    await _redeem_at(session, a, BEFORE)
    await _redeem_at(session, a, AFTER, OrderStatus.EXPIRED)
    assert await live_usage_by_code(session) == {CODE: 1}

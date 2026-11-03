"""M3b: a promo code with usage rules.

Everything here drives shipped functions: ``quote_checkout`` / ``resolve_price`` for the
refusal, ``confirm_order`` and ``create_pending_order`` for the redemption, and
``_show_summary`` for the customer's text. Each refusal is paired with the plain case that
passes, so a test goes red when the rule it names is broken and not when its setup is.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from bot.handlers import purchase
from bot.texts import texts
from database.base import Base
from database.models import DEAD_ORDER_STATUSES, Order, OrderStatus, PromoRedemption, User
from services.checkout import quote_checkout
from services.orders import create_pending_order
from services.pricing import PriceResult
from services.promo_rules import (
    PromoExpired,
    PromoFirstPurchaseOnly,
    PromoRuleError,
    PromoUsedUp,
    PromoUserLimit,
    check_promo_eligibility,
)
from tests.unit.test_customer_purchase_flow import (  # noqa: F401  (helpers)
    FIXED,
    _callback,
    _customer,
    _Message,
    _patch_panel,
    _pricing_file,
    _purchase_data,
    _settings,
    _state,
)
from tests.unit.test_user_offers_checkout import _offer, _order, _plan, _referred_buyer

CODE = "LIM"
_keys = itertools.count(1)


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


def _discounted(percent: int, base: int = FIXED) -> int:
    return base - base * percent // 100


def _iso(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).isoformat()


def _file(
    tmp_path: Path,
    promos: dict[str, object],
    *,
    combine: str = "best_of_promo_vs_extra",
) -> str:
    path = Path(_pricing_file(tmp_path))
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["promo_codes"] = promos
    doc["sales_rules"] = {"discount_combine": combine}
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def _entry(percent: str = "15", **rules: object) -> dict[str, object]:
    return {"percent": percent, "active": True, **rules}


async def _quote(
    session: AsyncSession,
    tmp_path: Path,
    user: User | None,
    entry: object,
    *,
    code: str = CODE,
    combine: str = "best_of_promo_vs_extra",
    settings_over: dict[str, object] | None = None,
    **data: object,
) -> PriceResult:
    settings = _settings(_file(tmp_path, {CODE: entry}, combine=combine), **(settings_over or {}))
    return await quote_checkout(
        session, settings, _purchase_data(**data), promo_code=code, db_user=user
    )


async def _redeem(
    session: AsyncSession,
    user: User,
    status: OrderStatus = OrderStatus.ACTIVE,
    *,
    code: str = CODE,
) -> Order:
    order = await _order(session, user, status)
    session.add(PromoRedemption(code=code, user_id=user.id, order_id=order.id))
    await session.commit()
    return order


async def _count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(PromoRedemption)) or 0)


async def _confirm(
    session: AsyncSession,
    settings: object,
    user: User,
    data: dict[str, object],
) -> tuple[_Message, FSMContext]:
    message = _Message()
    state = await _state(data)
    await purchase.confirm_order(_callback(message), state, session, settings, user)
    return message, state


def _shown(percent: int, **extra: object) -> dict[str, object]:
    return _purchase_data(
        promo_code="lim",
        final_price=_discounted(percent),
        card_amount=_discounted(percent),
        wallet_applied=0,
        **extra,
    )


# --- the four rules, each with its own error ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rule", "error", "stable_code"),
    [
        ("expired", PromoExpired, "promo_expired"),
        ("first_only", PromoFirstPurchaseOnly, "promo_first_only"),
        ("user_limit", PromoUserLimit, "promo_user_limit"),
        ("used_up", PromoUsedUp, "promo_used_up"),
    ],
)
async def test_each_rule_refuses_with_its_own_error_and_the_plain_buyer_passes(
    session: AsyncSession,
    tmp_path: Path,
    rule: str,
    error: type[PromoRuleError],
    stable_code: str,
) -> None:
    buyer = await _customer(session, balance=0, telegram_id=8001)
    other = await _customer(session, balance=0, telegram_id=8002)
    entries: dict[str, dict[str, object]] = {
        "expired": _entry(expires_at=_iso(-timedelta(seconds=5))),
        "first_only": _entry(first_purchase_only=True),
        "user_limit": _entry(per_user_limit=1),
        "used_up": _entry(total_limit=1),
    }
    # The same entry, with nobody having used anything, gives the discount (or, for an
    # expiry, with the date in the future).
    clean = dict(entries[rule])
    if rule == "expired":
        clean["expires_at"] = _iso(timedelta(hours=1))
    assert (await _quote(session, tmp_path, buyer, clean)).final_price == _discounted(15)

    if rule == "first_only":
        await _redeem_paid(session, buyer)
    elif rule == "user_limit":
        await _redeem(session, buyer)
    elif rule == "used_up":
        await _redeem(session, other)
    with pytest.raises(error) as caught:
        await _quote(session, tmp_path, buyer, entries[rule])
    assert caught.value.code == stable_code
    assert isinstance(caught.value, PromoRuleError) and isinstance(caught.value, ValueError)
    assert str(caught.value) != "", "the code is the message; it is never shown to a customer"


async def _redeem_paid(session: AsyncSession, user: User) -> None:
    """A real purchase: an ACTIVE order that cost something."""
    await _order(session, user, OrderStatus.ACTIVE, price=FIXED)


def test_the_four_error_classes_are_distinct_and_share_one_base() -> None:
    classes = {PromoExpired, PromoUsedUp, PromoFirstPurchaseOnly, PromoUserLimit}
    assert len(classes) == 4
    assert all(issubclass(c, PromoRuleError) for c in classes)
    codes = {c.code for c in classes}
    assert len(codes) == 4


@pytest.mark.asyncio
async def test_the_checks_run_in_order_expired_first_only_per_user_total(
    session: AsyncSession, tmp_path: Path
) -> None:
    buyer = await _customer(session, balance=0, telegram_id=8003)
    other = await _customer(session, balance=0, telegram_id=8004)
    await _redeem_paid(session, buyer)
    await _redeem(session, buyer)
    await _redeem(session, other)
    everything = _entry(
        first_purchase_only=True,
        per_user_limit=1,
        total_limit=1,
        expires_at=_iso(-timedelta(seconds=5)),
    )
    for expected in (PromoExpired, PromoFirstPurchaseOnly, PromoUserLimit, PromoUsedUp):
        with pytest.raises(expected):
            await _quote(session, tmp_path, buyer, everything)
        if expected is PromoExpired:
            everything.pop("expires_at")
        elif expected is PromoFirstPurchaseOnly:
            everything["first_purchase_only"] = False
        elif expected is PromoUserLimit:
            everything["per_user_limit"] = 0


# --- each limit at N-1, N and N+1 ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("used", "allowed"), [(2, True), (3, False), (4, False)])
async def test_the_per_user_limit_at_n_minus_one_n_and_n_plus_one(
    session: AsyncSession, tmp_path: Path, used: int, allowed: bool
) -> None:
    buyer = await _customer(session, balance=0, telegram_id=8010 + used)
    for _ in range(used):
        await _redeem(session, buyer)
    entry = _entry(per_user_limit=3)
    if allowed:
        assert (await _quote(session, tmp_path, buyer, entry)).final_price == _discounted(15)
    else:
        with pytest.raises(PromoUserLimit):
            await _quote(session, tmp_path, buyer, entry)


@pytest.mark.asyncio
@pytest.mark.parametrize(("used", "allowed"), [(2, True), (3, False), (4, False)])
async def test_the_total_limit_at_n_minus_one_n_and_n_plus_one(
    session: AsyncSession, tmp_path: Path, used: int, allowed: bool
) -> None:
    buyer = await _customer(session, balance=0, telegram_id=8020 + used)
    for i in range(used):
        await _redeem(
            session, await _customer(session, balance=0, telegram_id=8100 + used * 10 + i)
        )
    entry = _entry(total_limit=3)
    if allowed:
        assert (await _quote(session, tmp_path, buyer, entry)).final_price == _discounted(15)
    else:
        with pytest.raises(PromoUsedUp):
            await _quote(session, tmp_path, buyer, entry)


@pytest.mark.asyncio
async def test_a_limit_counts_only_its_own_code_and_zero_means_unlimited(
    session: AsyncSession, tmp_path: Path
) -> None:
    buyer = await _customer(session, balance=0, telegram_id=8030)
    for _ in range(3):
        await _redeem(session, buyer, code="OTHER")
    assert (
        await _quote(session, tmp_path, buyer, _entry(per_user_limit=1, total_limit=1))
    ).final_price == _discounted(15)
    for _ in range(5):
        await _redeem(session, buyer)
    free = _entry(per_user_limit=0, total_limit=0)
    assert (await _quote(session, tmp_path, buyer, free)).final_price == _discounted(15)


# --- expiry ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expires_at_just_before_and_just_after_now(session: AsyncSession) -> None:
    buyer = await _customer(session, balance=0, telegram_id=8040)
    now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    cfg = {"promo_codes": {CODE: _entry(expires_at=now.isoformat())}}
    # now == expires_at: the last second is over.
    with pytest.raises(PromoExpired):
        await check_promo_eligibility(session, cfg, CODE, buyer, now=now)
    with pytest.raises(PromoExpired):
        await check_promo_eligibility(session, cfg, CODE, buyer, now=now + timedelta(seconds=1))
    # one second earlier it still works
    await check_promo_eligibility(session, cfg, CODE, buyer, now=now - timedelta(seconds=1))


@pytest.mark.asyncio
async def test_an_offset_expiry_is_compared_as_the_instant_it_names(session: AsyncSession) -> None:
    buyer = await _customer(session, balance=0, telegram_id=8041)
    now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    # 14:00+03:30 is 10:30 UTC: already over at 12:00 UTC.
    cfg = {"promo_codes": {CODE: _entry(expires_at="2026-10-10T14:00:00+03:30")}}
    with pytest.raises(PromoExpired):
        await check_promo_eligibility(session, cfg, CODE, buyer, now=now)


# --- first purchase only ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_purchase_only_refuses_after_a_paid_order_and_allows_after_a_free_trial(
    session: AsyncSession, tmp_path: Path
) -> None:
    trial_only = await _customer(session, balance=0, telegram_id=8050)
    await _order(session, trial_only, OrderStatus.ACTIVE, price=0)  # a free trial
    await _order(session, trial_only, OrderStatus.PENDING_PAYMENT, price=FIXED)  # not yet paid
    entry = _entry(first_purchase_only=True)
    assert (await _quote(session, tmp_path, trial_only, entry)).final_price == _discounted(15)

    paid = await _customer(session, balance=0, telegram_id=8051)
    await _redeem_paid(session, paid)
    with pytest.raises(PromoFirstPurchaseOnly):
        await _quote(session, tmp_path, paid, entry)


# --- a dead order frees a use ----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "frees"),
    [
        (OrderStatus.EXPIRED, True),
        (OrderStatus.REJECTED, True),
        (OrderStatus.PENDING_PAYMENT, False),
        (OrderStatus.PAYMENT_REVIEW, False),
        (OrderStatus.ACTIVE, False),
        (OrderStatus.PROVISION_FAILED, False),
    ],
)
async def test_an_order_that_died_gives_the_use_back_and_any_other_holds_it(
    session: AsyncSession, tmp_path: Path, status: OrderStatus, frees: bool
) -> None:
    assert (status in DEAD_ORDER_STATUSES) is frees
    buyer = await _customer(session, balance=0, telegram_id=8060)
    other = await _customer(session, balance=0, telegram_id=8061)
    await _redeem(session, other, status)
    entry = _entry(total_limit=1)
    if frees:
        assert (await _quote(session, tmp_path, buyer, entry)).final_price == _discounted(15)
    else:
        with pytest.raises(PromoUsedUp):
            await _quote(session, tmp_path, buyer, entry)
    # the per-user count reads the same definition of «live»
    await _redeem(session, buyer, status)
    own = _entry(per_user_limit=1)
    if frees:
        assert (await _quote(session, tmp_path, buyer, own)).final_price == _discounted(15)
    else:
        with pytest.raises(PromoUserLimit):
            await _quote(session, tmp_path, buyer, own)


# --- old shapes, inactive codes --------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["bare", "dict"])
async def test_an_old_shaped_code_still_discounts_and_has_no_limits(
    session: AsyncSession, tmp_path: Path, shape: str
) -> None:
    buyer = await _customer(session, balance=0, telegram_id=8070)
    for _ in range(5):
        await _redeem(session, buyer)
    entry: object = "15" if shape == "bare" else {"percent": "15", "active": True}
    price = await _quote(session, tmp_path, buyer, entry)
    assert price.final_price == _discounted(15)
    assert price.meta["promo_redeem"] == {
        "code": CODE,
        "per_user_limit": 0,
        "total_limit": 0,
        "counted_from": None,
    }


@pytest.mark.asyncio
async def test_an_inactive_code_is_still_refused_by_pricing_whatever_its_rules_say(
    session: AsyncSession, tmp_path: Path
) -> None:
    buyer = await _customer(session, balance=0, telegram_id=8071)
    await _redeem(session, buyer)
    off = _entry(
        active=False,
        per_user_limit=1,
        total_limit=1,
        first_purchase_only=True,
        expires_at=_iso(-timedelta(hours=1)),
    )
    with pytest.raises(ValueError, match="invalid_promo_code") as caught:
        await _quote(session, tmp_path, buyer, off)
    assert not isinstance(caught.value, PromoRuleError)


@pytest.mark.asyncio
async def test_a_code_that_does_not_exist_is_refused_by_pricing_not_by_the_rules(
    session: AsyncSession, tmp_path: Path
) -> None:
    buyer = await _customer(session, balance=0, telegram_id=8072)
    with pytest.raises(ValueError, match="invalid_promo_code") as caught:
        await _quote(session, tmp_path, buyer, _entry(), code="NOPE")
    assert not isinstance(caught.value, PromoRuleError)
    cfg = {"promo_codes": {CODE: _entry(active=False, total_limit=1)}}
    assert await check_promo_eligibility(session, cfg, "NOPE", buyer) is None
    assert await check_promo_eligibility(session, cfg, CODE, buyer) is None


# --- which of the offer and the code was applied ----------------------------------------------


@pytest.mark.asyncio
async def test_a_promo_that_beats_a_smaller_offer_is_redeemed_and_the_offer_is_not(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_panel(monkeypatch)
    user = await _customer(session, balance=0, telegram_id=8080)
    offer = await _offer(session, user, percent=10)
    settings = _settings(_file(tmp_path, {CODE: _entry(total_limit=5)}))
    price = await quote_checkout(
        session, settings, _purchase_data(), promo_code="lim", db_user=user
    )
    assert price.final_price == _discounted(15)
    assert price.meta["promo_redeem"]["code"] == CODE
    assert "user_offer_id" not in price.meta

    await _confirm(session, settings, user, _shown(15))
    order = (await session.execute(select(Order))).scalar_one()
    row = (await session.execute(select(PromoRedemption))).scalar_one()
    assert (row.code, row.user_id, row.order_id) == (CODE, user.id, order.id)
    await session.refresh(offer)
    assert offer.order_id is None


@pytest.mark.asyncio
async def test_on_a_tie_the_promo_is_redeemed_and_the_offer_stays_unused(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_panel(monkeypatch)
    user = await _customer(session, balance=0, telegram_id=8081)
    offer = await _offer(session, user, percent=15)
    settings = _settings(_file(tmp_path, {CODE: _entry()}))
    price = await quote_checkout(
        session, settings, _purchase_data(), promo_code="lim", db_user=user
    )
    assert price.final_price == _discounted(15)
    assert "promo_redeem" in price.meta and "user_offer_id" not in price.meta

    await _confirm(session, settings, user, _shown(15))
    assert await _count(session) == 1
    await session.refresh(offer)
    assert offer.order_id is None


@pytest.mark.asyncio
async def test_a_bigger_offer_wins_so_the_promo_uses_nothing_up(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_panel(monkeypatch)
    user = await _customer(session, balance=0, telegram_id=8082)
    offer = await _offer(session, user, percent=25)
    settings = _settings(_file(tmp_path, {CODE: _entry(total_limit=1)}))
    price = await quote_checkout(
        session, settings, _purchase_data(), promo_code="lim", db_user=user
    )
    assert price.final_price == _discounted(25)
    assert "promo_redeem" not in price.meta and price.meta["user_offer_id"] == offer.id

    await _confirm(session, settings, user, _shown(25))
    order = (await session.execute(select(Order))).scalar_one()
    assert await _count(session) == 0, "the code lost the comparison: nothing is used up"
    await session.refresh(offer)
    assert offer.order_id == order.id


@pytest.mark.asyncio
async def test_a_bigger_referral_discount_also_leaves_the_promo_unused(
    session: AsyncSession, tmp_path: Path
) -> None:
    buyer = await _referred_buyer(session, 8083)
    price = await _quote(
        session,
        tmp_path,
        buyer,
        _entry(),
        settings_over={"referral_referred_discount_percent": 30},
    )
    assert price.final_price == _discounted(30)
    assert "promo_redeem" not in price.meta


@pytest.mark.asyncio
async def test_under_sum_the_offer_and_the_promo_are_both_applied(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_panel(monkeypatch)
    user = await _customer(session, balance=0, telegram_id=8084)
    offer = await _offer(session, user, percent=10)
    settings = _settings(_file(tmp_path, {CODE: _entry()}, combine="sum"))
    price = await quote_checkout(
        session, settings, _purchase_data(), promo_code="lim", db_user=user
    )
    assert price.final_price == _discounted(25)
    assert "promo_redeem" in price.meta and price.meta["user_offer_id"] == offer.id

    await _confirm(session, settings, user, _shown(25))
    order = (await session.execute(select(Order))).scalar_one()
    assert await _count(session) == 1
    await session.refresh(offer)
    assert offer.order_id == order.id


@pytest.mark.asyncio
async def test_a_promo_capped_by_the_maximum_discount_still_counts_as_applied(
    session: AsyncSession, tmp_path: Path
) -> None:
    buyer = await _customer(session, balance=0, telegram_id=8085)
    price = await _quote(
        session, tmp_path, buyer, _entry(), settings_over={"max_discount_percent": 10}
    )
    assert price.final_price == _discounted(10)
    assert "promo_redeem" in price.meta


# --- resellers -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reseller_with_a_limited_code_is_not_refused_and_redeems_nothing(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_panel(monkeypatch)
    shop = await _customer(session, balance=0, telegram_id=8090)
    shop.is_reseller = True
    await session.commit()
    other = await _customer(session, balance=0, telegram_id=8091)
    await _redeem(session, other)
    entry = _entry(
        total_limit=1,
        first_purchase_only=True,
        expires_at=_iso(-timedelta(hours=1)),
    )
    await _redeem_paid(session, shop)
    price = await _quote(session, tmp_path, shop, entry)
    assert "promo_redeem" not in price.meta

    settings = _settings(_file(tmp_path, {CODE: entry}))
    message, _ = await _confirm(session, settings, shop, _shown(0))
    assert texts.purchase_error not in message.texts
    assert await _count(session) == 1, "only the other customer's row"


# --- the redemption is written with the order -------------------------------------------------


@pytest.mark.asyncio
async def test_the_redemption_is_written_with_the_order_and_is_absent_when_the_order_is_refused(
    session: AsyncSession,
) -> None:
    holder = await _customer(session, balance=0, telegram_id=8100)
    await _redeem(session, holder)
    user = await _customer(session, balance=0, telegram_id=8101)
    plan = await _plan(session)
    await session.commit()
    uid = int(user.id)
    meta = {"promo_redeem": {"code": CODE, "per_user_limit": 0, "total_limit": 1}}
    price = PriceResult(FIXED, 7_500, 42_500, "IRT", meta)
    with pytest.raises(PromoUsedUp):
        await create_pending_order(session, user_id=uid, plan=plan, price=price)
    await session.rollback()
    await session.refresh(plan)
    orders = (await session.execute(select(Order).where(Order.user_id == uid))).all()
    assert orders == []
    assert await _count(session) == 1, "the holder's row; the refused order left none"

    roomy = PriceResult(
        FIXED, 7_500, 42_500, "IRT", {"promo_redeem": {**meta["promo_redeem"], "total_limit": 2}}
    )
    order = await create_pending_order(session, user_id=uid, plan=plan, price=roomy)
    await session.commit()
    rows = (
        (await session.execute(select(PromoRedemption).where(PromoRedemption.user_id == uid)))
        .scalars()
        .all()
    )
    assert [(r.code, r.order_id) for r in rows] == [(CODE, order.id)]


@pytest.mark.asyncio
async def test_handing_back_an_open_order_writes_no_redemption(session: AsyncSession) -> None:
    user = await _customer(session, balance=0, telegram_id=8102)
    plan = await _plan(session)
    first = await create_pending_order(
        session, user_id=user.id, plan=plan, price=PriceResult(FIXED, 0, FIXED, "IRT", {})
    )
    await session.commit()
    again = await create_pending_order(
        session,
        user_id=user.id,
        plan=plan,
        price=PriceResult(
            FIXED,
            7_500,
            42_500,
            "IRT",
            {"promo_redeem": {"code": CODE, "per_user_limit": 0, "total_limit": 0}},
        ),
    )
    assert again.id == first.id
    assert await _count(session) == 0


@pytest.mark.asyncio
async def test_an_order_priced_without_a_promo_writes_no_redemption(session: AsyncSession) -> None:
    user = await _customer(session, balance=0, telegram_id=8103)
    plan = await _plan(session)
    await create_pending_order(
        session, user_id=user.id, plan=plan, price=PriceResult(FIXED, 0, FIXED, "IRT", {})
    )
    await session.commit()
    assert await _count(session) == 0


@pytest.mark.asyncio
async def test_the_per_user_limit_is_rechecked_inside_the_order_transaction(
    session: AsyncSession,
) -> None:
    user = await _customer(session, balance=0, telegram_id=8104)
    await _redeem(session, user)
    plan = await _plan(session)
    meta = {"promo_redeem": {"code": CODE, "per_user_limit": 1, "total_limit": 0}}
    with pytest.raises(PromoUserLimit):
        await create_pending_order(
            session, user_id=user.id, plan=plan, price=PriceResult(FIXED, 1, FIXED - 1, "IRT", meta)
        )


# --- the customer's text ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rule", "text"),
    [
        ("expired", "purchase_promo_expired"),
        ("first_only", "purchase_promo_first_only"),
        ("user_limit", "purchase_promo_user_limit"),
        ("used_up", "purchase_promo_used_up"),
    ],
)
async def test_the_summary_answers_each_rule_with_its_own_text(
    session: AsyncSession, tmp_path: Path, rule: str, text: str
) -> None:
    buyer = await _customer(session, balance=0, telegram_id=8110)
    other = await _customer(session, balance=0, telegram_id=8111)
    entry: dict[str, object] = {
        "expired": _entry(expires_at=_iso(-timedelta(seconds=5))),
        "first_only": _entry(first_purchase_only=True),
        "user_limit": _entry(per_user_limit=1),
        "used_up": _entry(total_limit=1),
    }[rule]
    if rule == "first_only":
        await _redeem_paid(session, buyer)
    elif rule == "user_limit":
        await _redeem(session, buyer)
    elif rule == "used_up":
        await _redeem(session, other)
    shown = _Message()
    await purchase._show_summary(
        shown,
        await _state(_purchase_data()),
        session,
        _settings(_file(tmp_path, {CODE: entry})),
        promo_code="lim",
        db_user=buyer,
    )
    expected = getattr(texts, text)
    assert shown.texts == [expected]
    assert "promo_" not in shown.texts[0]


def test_the_four_customer_texts_are_the_owners_words() -> None:
    assert texts.purchase_promo_expired == "🎫 مهلت این کد تخفیف تموم شده."
    assert texts.purchase_promo_used_up == "🎫 ظرفیت این کد تخفیف پر شده."
    assert texts.purchase_promo_first_only == "🎫 این کد فقط برای اولین خریده."
    assert texts.purchase_promo_user_limit == "🎫 قبلاً از این کد استفاده کردی."


# --- filled between the summary and the till --------------------------------------------------


@pytest.mark.asyncio
async def test_a_limit_filled_since_the_summary_answers_the_rule_and_shows_the_undiscounted_summary(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_panel(monkeypatch)
    buyer = await _customer(session, balance=0, telegram_id=8120)
    other = await _customer(session, balance=0, telegram_id=8121)
    settings = _settings(_file(tmp_path, {CODE: _entry(total_limit=1)}))
    await _redeem(session, other)  # the last use went while the summary was on screen

    message, state = await _confirm(session, settings, buyer, _shown(15))
    assert message.texts[0] == texts.purchase_promo_used_up
    assert texts.purchase_error not in message.texts
    assert (await session.execute(select(Order).where(Order.user_id == buyer.id))).first() is None
    assert await _count(session) == 1
    data = await state.get_data()
    assert data.get("promo_code") is None
    assert data["final_price"] == FIXED, "the summary came back without the code"
    assert len(message.texts) >= 2


@pytest.mark.asyncio
async def test_a_limit_filled_after_the_quote_but_before_the_write_rolls_the_order_back(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The quote at confirm still saw a free use (the race the quote cannot see); the recheck
    inside the order's own transaction finds the last use gone."""
    _patch_panel(monkeypatch)
    buyer = await _customer(session, balance=0, telegram_id=8122)
    other = await _customer(session, balance=0, telegram_id=8123)
    settings = _settings(_file(tmp_path, {CODE: _entry(total_limit=1)}))
    real = purchase.create_pending_order

    async def lose_the_race(sess: AsyncSession, **kwargs: object) -> Order:
        # Another customer's order commits its row after our quote, before our write.
        held = await _live_order(sess, other)
        sess.add(PromoRedemption(code=CODE, user_id=other.id, order_id=held))
        await sess.commit()
        return await real(sess, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(purchase, "create_pending_order", lose_the_race)
    message, state = await _confirm(session, settings, buyer, _shown(15))
    assert message.texts[0] == texts.purchase_promo_used_up
    assert (await session.execute(select(Order).where(Order.user_id == buyer.id))).first() is None
    data = await state.get_data()
    assert data.get("promo_code") is None and data["final_price"] == FIXED


async def _live_order(session: AsyncSession, user: User) -> int:
    order = await _order(session, user, OrderStatus.PENDING_PAYMENT)
    return int(order.id)


@pytest.mark.asyncio
async def test_a_double_tap_on_confirm_writes_one_order_and_one_redemption(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_panel(monkeypatch)
    user = await _customer(session, balance=0, telegram_id=8130)
    settings = _settings(_file(tmp_path, {CODE: _entry(per_user_limit=1)}))
    data = _shown(15)
    await _confirm(session, settings, user, data)
    await _confirm(session, settings, user, data)
    assert len((await session.execute(select(Order))).scalars().all()) == 1
    assert await _count(session) == 1


@pytest.mark.asyncio
async def test_a_redemption_keeps_the_codes_upper_case_whatever_the_customer_typed(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_panel(monkeypatch)
    user = await _customer(session, balance=0, telegram_id=8131)
    settings = _settings(_file(tmp_path, {CODE: _entry()}))
    await _confirm(session, settings, user, _shown(15))
    row = (await session.execute(select(PromoRedemption))).scalar_one()
    assert row.code == CODE

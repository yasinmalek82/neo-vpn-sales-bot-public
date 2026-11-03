"""M3a: a personal offer is a discount for one user, with no code.

These drive ``quote_checkout`` and ``confirm_order``, the two places a price is
decided and charged, and ``create_pending_order``, where the offer is attached.
Every case is paired with the plain one that gives the discount, so a test goes
red when the rule it names is broken and not when its setup is.
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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from bot.handlers import purchase
from bot.texts import texts
from database.base import Base
from database.models import DEAD_ORDER_STATUSES, Order, OrderStatus, Plan, User, UserOffer
from database.models.plan import DataLimitResetStrategy
from services.checkout import personal_extra, quote_checkout
from services.orders import create_pending_order
from services.pricing import PriceResult
from services.user_offers import OfferNoLongerLive, best_live_offer
from services.ux_format import to_persian_digits
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


async def _offer(
    session: AsyncSession,
    user: User,
    *,
    percent: int = 10,
    expires_in: timedelta = timedelta(hours=24),
    voided: bool = False,
    min_volume_gb: int | None = None,
    order_id: int | None = None,
) -> UserOffer:
    row = UserOffer(
        user_id=user.id,
        campaign="winback",
        percent=percent,
        min_volume_gb=min_volume_gb,
        event_key=f"test:offer:{next(_keys)}",
        expires_at=datetime.now(UTC) + expires_in,
        order_id=order_id,
        voided_at=datetime.now(UTC) if voided else None,
    )
    session.add(row)
    await session.commit()
    return row


async def _plan(session: AsyncSession) -> Plan:
    plan = Plan(
        name=f"offer-plan-{next(_keys)}",
        description="test",
        data_limit_gb=10,
        duration_days=30,
        price_toman=FIXED,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1],
        hwid_limit=0,
        is_active=True,
    )
    session.add(plan)
    await session.flush()
    return plan


async def _order(
    session: AsyncSession, user: User, status: OrderStatus, *, price: int = FIXED
) -> Order:
    plan = await _plan(session)
    order = Order(user_id=user.id, plan_id=plan.id, status=status, price_toman=price)
    session.add(order)
    await session.commit()
    return order


async def _quote(
    session: AsyncSession,
    tmp_path: Path,
    user: User | None,
    *,
    promo_code: str | None = None,
    promo: bool = False,
    settings_over: dict[str, object] | None = None,
    **data: object,
) -> PriceResult:
    settings = _settings(_pricing_file(tmp_path, promo=promo), **(settings_over or {}))
    return await quote_checkout(
        session, settings, _purchase_data(**data), promo_code=promo_code, db_user=user
    )


async def _confirm(
    session: AsyncSession, tmp_path: Path, user: User, state_data: dict[str, object]
) -> _Message:
    message = _Message()
    await purchase.confirm_order(
        _callback(message),
        await _state(state_data),
        session,
        _settings(_pricing_file(tmp_path)),
        user,
    )
    return message


def _discounted(percent: int, base: int = FIXED) -> int:
    return base - base * percent // 100


# --- the discount -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_live_offer_takes_its_percent_off_and_the_order_carries_it(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_panel(monkeypatch)
    user = await _customer(session, balance=0, telegram_id=7001)
    offer = await _offer(session, user, percent=10)

    price = await _quote(session, tmp_path, user)
    assert price.final_price == _discounted(10)
    assert price.meta["user_offer_id"] == offer.id and price.meta["user_offer_percent"] == 10

    message = await _confirm(
        session,
        tmp_path,
        user,
        _purchase_data(final_price=_discounted(10), card_amount=_discounted(10), wallet_applied=0),
    )
    order = (await session.execute(select(Order))).scalar_one()
    assert order.price_toman == _discounted(10)
    assert texts.purchase_price_updated not in message.texts
    await session.refresh(offer)
    assert offer.order_id == order.id


@pytest.mark.asyncio
async def test_without_an_offer_the_price_is_the_plain_one_and_meta_says_nothing(
    session: AsyncSession, tmp_path: Path
) -> None:
    user = await _customer(session, balance=0, telegram_id=7002)
    price = await _quote(session, tmp_path, user)
    assert price.final_price == FIXED
    assert "user_offer_id" not in price.meta and "user_offer_percent" not in price.meta


@pytest.mark.asyncio
async def test_a_second_order_by_the_same_user_gets_no_offer(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_panel(monkeypatch)
    user = await _customer(session, balance=0, telegram_id=7003)
    offer = await _offer(session, user, percent=10)
    await _confirm(
        session,
        tmp_path,
        user,
        _purchase_data(final_price=_discounted(10), card_amount=_discounted(10), wallet_applied=0),
    )
    first = (await session.execute(select(Order))).scalar_one()
    first.status = OrderStatus.ACTIVE
    await session.commit()

    second = await _quote(session, tmp_path, user)
    assert second.final_price == FIXED
    assert "user_offer_id" not in second.meta
    await session.refresh(offer)
    assert offer.order_id == first.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["expired", "voided"],
)
async def test_an_expired_or_voided_offer_gives_nothing(
    session: AsyncSession, tmp_path: Path, case: str
) -> None:
    user = await _customer(session, balance=0, telegram_id=7004)
    live = await _offer(session, user, percent=10)
    assert (await _quote(session, tmp_path, user)).final_price == _discounted(10)
    if case == "expired":
        live.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    else:
        live.voided_at = datetime.now(UTC)
    await session.commit()
    price = await _quote(session, tmp_path, user)
    assert price.final_price == FIXED and "user_offer_id" not in price.meta, case


@pytest.mark.asyncio
async def test_the_best_of_several_live_offers_wins(session: AsyncSession, tmp_path: Path) -> None:
    user = await _customer(session, balance=0, telegram_id=7005)
    await _offer(session, user, percent=10)
    big = await _offer(session, user, percent=20)
    price = await _quote(session, tmp_path, user)
    assert price.final_price == _discounted(20)
    assert price.meta["user_offer_id"] == big.id


# --- min_volume_gb -------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("volume_gb", "applies"),
    [(10, False), (5, False), (None, False), (20, True)],
)
async def test_min_volume_is_refused_on_the_same_size_and_accepted_on_a_bigger_package(
    session: AsyncSession, tmp_path: Path, volume_gb: int | None, applies: bool
) -> None:
    user = await _customer(session, balance=0, telegram_id=7006)
    await _offer(session, user, percent=10, min_volume_gb=10)
    price = await _quote(session, tmp_path, user, volume_gb=volume_gb)
    expected = _discounted(10) if applies else FIXED
    assert price.final_price == expected, (volume_gb, applies)
    assert ("user_offer_id" in price.meta) is applies


# --- resellers -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reseller_gets_no_offer_on_a_build_with_no_agreed_price(
    session: AsyncSession, tmp_path: Path
) -> None:
    """An unlimited custom build has no reseller shape, so it stays at retail: that
    is exactly where an offer would leak through if the lookup were not excluded."""
    shop = await _customer(session, balance=0, telegram_id=7007)
    shop.is_reseller = True
    await session.commit()
    await _offer(session, shop, percent=30)
    unlimited = {
        "plan_type": "unlimited",
        "concurrent_users": 1,
        "duration_months": 1,
        "fixed_price_toman": None,
        "volume_gb": None,
        "duration_days": None,
    }
    price = await _quote(session, tmp_path, shop, **unlimited)

    plain = await _customer(session, balance=0, telegram_id=7008)
    plain_price = await _quote(session, tmp_path, plain, **unlimited)
    assert price.final_price == plain_price.final_price
    assert "user_offer_id" not in price.meta

    await _offer(session, plain, percent=30)
    discounted = await _quote(session, tmp_path, plain, **unlimited)
    assert discounted.final_price < plain_price.final_price, "the same build, for a retail buyer"


@pytest.mark.asyncio
async def test_the_one_function_gives_a_reseller_nothing_even_with_a_live_offer(
    session: AsyncSession,
) -> None:
    shop = await _customer(session, balance=0, telegram_id=7009)
    shop.is_reseller = True
    await session.commit()
    await _offer(session, shop, percent=30)
    got = await personal_extra(session, _settings("unused"), _purchase_data(), shop)
    assert got.percent == 0 and got.offer_id is None

    retail = await _customer(session, balance=0, telegram_id=7010)
    await _offer(session, retail, percent=30)
    assert (
        await personal_extra(session, _settings("unused"), _purchase_data(), retail)
    ).percent == 30


# --- promo codes and the referral discount --------------------------------------


@pytest.mark.asyncio
async def test_a_promo_that_beats_a_smaller_offer_wins_and_the_offer_stays_live(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_panel(monkeypatch)
    user = await _customer(session, balance=0, telegram_id=7011)
    offer = await _offer(session, user, percent=10)

    price = await _quote(session, tmp_path, user, promo=True, promo_code="off15")
    assert price.final_price == _discounted(15)
    assert "user_offer_id" not in price.meta and "user_offer_percent" not in price.meta

    await _confirm(
        session,
        tmp_path,
        user,
        _purchase_data(
            promo_code="off15",
            final_price=_discounted(15),
            card_amount=_discounted(15),
            wallet_applied=0,
        ),
    )
    await session.refresh(offer)
    assert offer.order_id is None, "the code won, so the offer is still the customer's"


@pytest.mark.asyncio
async def test_a_bigger_offer_beats_a_promo_and_is_the_one_attached(
    session: AsyncSession, tmp_path: Path
) -> None:
    user = await _customer(session, balance=0, telegram_id=7012)
    offer = await _offer(session, user, percent=25)
    price = await _quote(session, tmp_path, user, promo=True, promo_code="off15")
    assert price.final_price == _discounted(25)
    assert price.meta["user_offer_id"] == offer.id


@pytest.mark.asyncio
async def test_under_sum_both_apply_so_the_offer_is_applied(
    session: AsyncSession, tmp_path: Path
) -> None:
    path = Path(_pricing_file(tmp_path, promo=True))
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["sales_rules"] = {"discount_combine": "sum"}
    path.write_text(json.dumps(doc), encoding="utf-8")
    user = await _customer(session, balance=0, telegram_id=7013)
    offer = await _offer(session, user, percent=10)
    price = await quote_checkout(
        session,
        _settings(str(path)),
        _purchase_data(),
        promo_code="off15",
        db_user=user,
    )
    assert price.final_price == _discounted(25)
    assert price.meta["user_offer_id"] == offer.id


@pytest.mark.asyncio
async def test_a_plan_that_forbids_extra_discount_takes_no_offer(
    session: AsyncSession, tmp_path: Path
) -> None:
    user = await _customer(session, balance=0, telegram_id=7014)
    await _offer(session, user, percent=10)
    price = await _quote(session, tmp_path, user, forbid_extra_discount=True)
    assert price.final_price == FIXED
    assert "user_offer_id" not in price.meta


@pytest.mark.asyncio
async def test_max_discount_percent_caps_the_offer_but_it_still_counts_as_applied(
    session: AsyncSession, tmp_path: Path
) -> None:
    user = await _customer(session, balance=0, telegram_id=7015)
    offer = await _offer(session, user, percent=30)
    price = await _quote(session, tmp_path, user, settings_over={"max_discount_percent": 20})
    assert price.final_price == _discounted(20)
    assert price.meta["user_offer_id"] == offer.id


async def _referred_buyer(session: AsyncSession, telegram_id: int) -> User:
    referrer = User(telegram_id=telegram_id + 5000, referral_code=f"R{telegram_id}")
    session.add(referrer)
    await session.flush()
    buyer = User(
        telegram_id=telegram_id, referral_code=f"B{telegram_id}", referred_by_id=referrer.id
    )
    session.add(buyer)
    await session.commit()
    return buyer


@pytest.mark.asyncio
async def test_on_a_tie_the_referral_wins_and_the_offer_stays_unused(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_panel(monkeypatch)
    buyer = await _referred_buyer(session, 7016)
    offer = await _offer(session, buyer, percent=10)
    over = {"referral_referred_discount_percent": 10}

    price = await _quote(session, tmp_path, buyer, settings_over=over)
    assert price.final_price == _discounted(10)
    assert "user_offer_id" not in price.meta

    message = _Message()
    await purchase.confirm_order(
        _callback(message),
        await _state(
            _purchase_data(
                final_price=_discounted(10), card_amount=_discounted(10), wallet_applied=0
            )
        ),
        session,
        _settings(_pricing_file(tmp_path), **over),
        buyer,
    )
    assert (await session.execute(select(Order))).scalar_one().price_toman == _discounted(10)
    await session.refresh(offer)
    assert offer.order_id is None


@pytest.mark.asyncio
async def test_a_bigger_offer_beats_the_referral_discount(
    session: AsyncSession, tmp_path: Path
) -> None:
    buyer = await _referred_buyer(session, 7017)
    offer = await _offer(session, buyer, percent=15)
    price = await _quote(
        session, tmp_path, buyer, settings_over={"referral_referred_discount_percent": 10}
    )
    assert price.final_price == _discounted(15)
    assert price.meta["user_offer_id"] == offer.id


# --- an order that dies frees the offer ----------------------------------------


def test_dead_order_statuses_are_exactly_rejected_and_expired() -> None:
    assert frozenset({OrderStatus.REJECTED, OrderStatus.EXPIRED}) == DEAD_ORDER_STATUSES
    assert OrderStatus.PROVISION_FAILED not in DEAD_ORDER_STATUSES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "live_again"),
    [
        (OrderStatus.EXPIRED, True),
        (OrderStatus.REJECTED, True),
        (OrderStatus.PROVISION_FAILED, False),
        (OrderStatus.PENDING_PAYMENT, False),
        (OrderStatus.PAYMENT_REVIEW, False),
        (OrderStatus.ACTIVE, False),
    ],
)
async def test_an_offer_on_a_dead_order_is_live_again_and_on_any_other_it_is_not(
    session: AsyncSession, tmp_path: Path, status: OrderStatus, live_again: bool
) -> None:
    user = await _customer(session, balance=0, telegram_id=7020)
    held = await _order(session, user, status)
    offer = await _offer(session, user, percent=10, order_id=held.id)
    price = await _quote(session, tmp_path, user)
    assert (price.final_price == _discounted(10)) is live_again, status
    assert ("user_offer_id" in price.meta) is live_again
    if live_again:
        assert price.meta["user_offer_id"] == offer.id


@pytest.mark.asyncio
async def test_an_expired_order_frees_the_offer_for_the_next_order_and_it_is_taken_again(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_panel(monkeypatch)
    user = await _customer(session, balance=0, telegram_id=7021)
    offer = await _offer(session, user, percent=10)
    shown = _purchase_data(
        final_price=_discounted(10), card_amount=_discounted(10), wallet_applied=0
    )
    await _confirm(session, tmp_path, user, shown)
    first = (await session.execute(select(Order))).scalar_one()
    first.status = OrderStatus.EXPIRED
    await session.commit()

    await _confirm(session, tmp_path, user, shown)
    orders = (await session.execute(select(Order).order_by(Order.id))).scalars().all()
    assert len(orders) == 2 and orders[1].price_toman == _discounted(10)
    await session.refresh(offer)
    assert offer.order_id == orders[1].id


# --- the summary and the till --------------------------------------------------


@pytest.mark.asyncio
async def test_the_summary_shows_the_offer_and_the_same_number_the_till_charges(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_panel(monkeypatch)
    user = await _customer(session, balance=0, telegram_id=7022)
    await _offer(session, user, percent=10)
    state = await _state(_purchase_data())
    settings = _settings(_pricing_file(tmp_path))

    shown = _Message()
    await purchase._show_summary(shown, state, session, settings, promo_code=None, db_user=user)
    line = texts.purchase_personal_offer_line.format(percent=to_persian_digits(10))
    assert line in shown.texts[-1]
    summary_price = (await state.get_data())["final_price"]

    till = _Message()
    await purchase.confirm_order(
        _callback(till),
        state,
        session,
        settings,
        user,
    )
    order = (await session.execute(select(Order))).scalar_one()
    assert texts.purchase_price_updated not in till.texts
    assert order.price_toman == summary_price == _discounted(10)


@pytest.mark.asyncio
async def test_no_offer_no_line_in_the_summary(session: AsyncSession, tmp_path: Path) -> None:
    user = await _customer(session, balance=0, telegram_id=7023)
    shown = _Message()
    await purchase._show_summary(
        shown,
        await _state(_purchase_data()),
        session,
        _settings(_pricing_file(tmp_path)),
        promo_code=None,
        db_user=user,
    )
    assert "تخفیف ویژهٔ تو" not in shown.texts[-1]


# --- taken between summary and confirm -----------------------------------------


@pytest.mark.asyncio
async def test_an_offer_taken_since_the_summary_writes_no_order_at_the_discounted_price(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The quote at confirm still saw the offer live (a race the quote cannot see), but the
    guarded write finds it taken: the handler shows the summary again, nothing is written."""
    _patch_panel(monkeypatch)
    user = await _customer(session, balance=0, telegram_id=7024)
    offer = await _offer(session, user, percent=10)
    discounted = await _quote(session, tmp_path, user)
    assert discounted.meta["user_offer_id"] == offer.id, "the quote that went stale"

    async def stale_quote(*_args: object, **_kwargs: object) -> PriceResult:
        return discounted

    monkeypatch.setattr(purchase, "quote_checkout", stale_quote)
    offer.voided_at = datetime.now(UTC)  # taken after that quote was read
    await session.commit()

    message = await _confirm(
        session,
        tmp_path,
        user,
        _purchase_data(final_price=_discounted(10), card_amount=_discounted(10), wallet_applied=0),
    )
    assert (await session.execute(select(Order))).first() is None
    assert message.texts[0] == texts.purchase_price_updated
    assert texts.purchase_error not in message.texts
    # The plan row flushed before the refused order is rolled back too: no write
    # lock is held while the summary is sent again.
    assert (await session.execute(select(Plan))).first() is None


@pytest.mark.asyncio
async def test_a_quote_that_already_lost_the_offer_is_the_ordinary_moved_price(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_panel(monkeypatch)
    user = await _customer(session, balance=0, telegram_id=7025)
    offer = await _offer(session, user, percent=10)
    offer.voided_at = datetime.now(UTC)
    await session.commit()
    message = await _confirm(
        session,
        tmp_path,
        user,
        _purchase_data(final_price=_discounted(10), card_amount=_discounted(10), wallet_applied=0),
    )
    assert (await session.execute(select(Order))).first() is None
    assert message.texts[0] == texts.purchase_price_updated


@pytest.mark.asyncio
async def test_create_pending_order_refuses_a_taken_offer_and_leaves_no_order(
    session: AsyncSession,
) -> None:
    user = await _customer(session, balance=0, telegram_id=7026)
    held = await _order(session, user, OrderStatus.ACTIVE)
    offer = await _offer(session, user, percent=10, order_id=held.id)
    plan = await _plan(session)
    price = PriceResult(
        original_price=FIXED,
        discount_amount=5_000,
        final_price=45_000,
        currency="IRT",
        meta={"user_offer_id": offer.id, "user_offer_percent": 10},
    )
    with pytest.raises(OfferNoLongerLive):
        await create_pending_order(session, user_id=user.id, plan=plan, price=price)
    orders = (await session.execute(select(Order).where(Order.id != held.id))).scalars().all()
    assert orders == []
    await session.refresh(offer)
    assert offer.order_id == held.id, "the offer is still on the order that holds it"


@pytest.mark.asyncio
async def test_create_pending_order_attaches_a_live_offer_and_only_then(
    session: AsyncSession,
) -> None:
    user = await _customer(session, balance=0, telegram_id=7027)
    offer = await _offer(session, user, percent=10)
    plan = await _plan(session)
    price = PriceResult(
        original_price=FIXED,
        discount_amount=5_000,
        final_price=45_000,
        currency="IRT",
        meta={"user_offer_id": offer.id, "user_offer_percent": 10},
    )
    order = await create_pending_order(session, user_id=user.id, plan=plan, price=price)
    await session.commit()
    await session.refresh(offer)
    assert offer.order_id == order.id

    no_meta = await _customer(session, balance=0, telegram_id=7028)
    other = await _offer(session, no_meta, percent=10)
    plain = PriceResult(FIXED, 0, FIXED, "IRT", {})
    await create_pending_order(session, user_id=no_meta.id, plan=plan, price=plain)
    await session.commit()
    await session.refresh(other)
    assert other.order_id is None, "an order priced without an offer takes none"


@pytest.mark.asyncio
async def test_handing_back_an_open_order_attaches_nothing(session: AsyncSession) -> None:
    user = await _customer(session, balance=0, telegram_id=7029)
    plan = await _plan(session)
    first = await create_pending_order(
        session, user_id=user.id, plan=plan, price=PriceResult(FIXED, 0, FIXED, "IRT", {})
    )
    await session.commit()
    offer = await _offer(session, user, percent=10)
    again = await create_pending_order(
        session,
        user_id=user.id,
        plan=plan,
        price=PriceResult(
            FIXED, 5_000, 45_000, "IRT", {"user_offer_id": offer.id, "user_offer_percent": 10}
        ),
    )
    assert again.id == first.id
    await session.refresh(offer)
    assert offer.order_id is None


# --- second audit: pinned behaviours and ties ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_referred_reseller_with_an_offer_pays_the_full_fixed_price_on_the_lock_path(
    session: AsyncSession, tmp_path: Path
) -> None:
    """Before M3 the fixed-price lock gave a reseller the referral percent; an agreed
    price replaces retail, so neither the referral nor an offer applies (resellers.md).

    The data has a ready plan's fixed price and no reseller shape, so ``apply_reseller_price``
    hands retail back and ``should_lock_to_fixed_price`` is true: this is the lock path.
    """
    shop = await _referred_buyer(session, 7030)
    shop.is_reseller = True
    await session.commit()
    offer = await _offer(session, shop, percent=30)
    over = {"referral_referred_discount_percent": 10}
    data = {
        "plan_type": "unlimited",
        "concurrent_users": 1,
        "duration_months": 1,
        "volume_gb": None,
        "duration_days": None,
    }

    price = await _quote(session, tmp_path, shop, settings_over=over, **data)
    assert price.final_price == FIXED and price.discount_amount == 0
    assert "user_offer_id" not in price.meta and "user_offer_percent" not in price.meta

    plain = await _referred_buyer(session, 7031)
    cut = await _quote(session, tmp_path, plain, settings_over=over, **data)
    assert cut.final_price == _discounted(10), "the same data, a retail buyer: the referral applies"

    await session.refresh(offer)
    assert offer.order_id is None


@pytest.mark.asyncio
async def test_on_a_tie_with_the_referral_the_offer_is_still_live_after_the_order(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_panel(monkeypatch)
    buyer = await _referred_buyer(session, 7032)
    offer = await _offer(session, buyer, percent=10)
    over = {"referral_referred_discount_percent": 10}

    price = await _quote(session, tmp_path, buyer, settings_over=over)
    assert price.final_price == _discounted(10) and "user_offer_id" not in price.meta

    message = _Message()
    await purchase.confirm_order(
        _callback(message),
        await _state(
            _purchase_data(
                final_price=_discounted(10), card_amount=_discounted(10), wallet_applied=0
            )
        ),
        session,
        _settings(_pricing_file(tmp_path), **over),
        buyer,
    )
    order = (await session.execute(select(Order))).scalar_one()
    assert order.price_toman == _discounted(10)
    live = await best_live_offer(session, user_id=buyer.id, volume_gb=None)
    assert live is not None and live.id == offer.id, "still the customer's, to spend later"


@pytest.mark.asyncio
async def test_on_a_tie_with_a_promo_under_best_of_the_code_is_applied_and_the_offer_stays_live(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_panel(monkeypatch)
    user = await _customer(session, balance=0, telegram_id=7033)
    offer = await _offer(session, user, percent=15)  # the code OFF15 is also 15%
    settings = _settings(_pricing_file(tmp_path, promo=True))  # discount_combine: best_of

    price = await quote_checkout(
        session, settings, _purchase_data(), promo_code="off15", db_user=user
    )
    assert price.final_price == _discounted(15)
    assert price.meta["promo_redeem"]["code"] == "OFF15", "the tie goes to the code"
    assert "user_offer_id" not in price.meta and "user_offer_percent" not in price.meta

    await purchase.confirm_order(
        _callback(_Message()),
        await _state(
            _purchase_data(
                promo_code="off15",
                final_price=_discounted(15),
                card_amount=_discounted(15),
                wallet_applied=0,
            )
        ),
        session,
        settings,
        user,
    )
    order = (await session.execute(select(Order))).scalar_one()
    assert order.price_toman == _discounted(15)
    live = await best_live_offer(session, user_id=user.id, volume_gb=None)
    assert live is not None and live.id == offer.id

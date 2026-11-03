"""What a mobile shop pays, and the guard that keeps it below retail.

The owner's two decisions drive this file:

1. the reseller rate is a **raw** toman/GB + toman/day, not a percentage off
   retail, so that changing a retail price does not move his margin;
2. if that independent formula ever lands **above** retail, the reseller pays
   the lower price and the owner is told — «به نماینده کمترین قیمت رو بده و به
   تو هشدار بده».

Point 2 exists because of point 1: retail volume pricing is `rate × GB` minus a
duration discount with no per-day term, the reseller formula adds one, and two
differently-shaped curves always cross somewhere.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from database.base import Base
from database.models import User
from services.pricing import PlanType, calculate_price
from services.reseller import (
    ResellerError,
    clear_plan_price,
    get_plan_price,
    quote_custom_volume,
    quote_ready_plan,
    set_plan_price,
    set_rate,
)

# Retail: 2 000 toman/GB, 10% off from 60 days. No per-day component at all.
CFG = {
    "currency": "IRT",
    "volume_plan": {
        "tiers": [{"max_gb": None, "price_per_gb_toman": 2000}],
        "duration_discounts": [{"min_days": 60, "percent": 10}],
    },
    "unlimited_plan": {
        "base_price_first_user_toman": 250_000,
        "additional_user_coefficients": [0.5],
        "duration_discounts": [],
    },
}


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def shop(session: AsyncSession) -> User:
    user = User(telegram_id=900, referral_code="SHOP900", is_reseller=True)
    session.add(user)
    await session.flush()
    return user


# --------------------------------------------------------------------------- #
# Ready plans
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_reseller_with_no_agreed_price_pays_retail(
    session: AsyncSession, shop: User
) -> None:
    """A shop can be made a reseller today and priced tomorrow."""
    quote = await quote_ready_plan(
        session,
        user_id=shop.id,
        package_slug="volume-plan-1",
        retail_price_toman=150_000,
        duration_days=30,
        tier="economy",
    )
    assert quote.price_toman == 150_000
    assert quote.crossed_over is False
    assert quote.margin_toman == 0


@pytest.mark.asyncio
async def test_an_agreed_price_is_what_they_pay(session: AsyncSession, shop: User) -> None:
    await set_plan_price(
        session, shop.id, "volume-plan-1", 110_000, duration_days=30, tier="economy"
    )
    quote = await quote_ready_plan(
        session,
        user_id=shop.id,
        package_slug="volume-plan-1",
        retail_price_toman=150_000,
        duration_days=30,
        tier="economy",
    )
    assert quote.price_toman == 110_000
    assert quote.margin_toman == 40_000


@pytest.mark.asyncio
async def test_prices_are_per_reseller(session: AsyncSession, shop: User) -> None:
    """«ادمین می‌تونه براش قیمت متفاوت تنظیم بکنه» — per shop, not one list."""
    other = User(telegram_id=901, referral_code="SHOP901", is_reseller=True)
    session.add(other)
    await session.flush()

    await set_plan_price(
        session, shop.id, "volume-plan-1", 110_000, duration_days=30, tier="economy"
    )
    await set_plan_price(
        session, other.id, "volume-plan-1", 125_000, duration_days=30, tier="economy"
    )

    a = await quote_ready_plan(
        session,
        user_id=shop.id,
        package_slug="volume-plan-1",
        retail_price_toman=150_000,
        duration_days=30,
        tier="economy",
    )
    b = await quote_ready_plan(
        session,
        user_id=other.id,
        package_slug="volume-plan-1",
        retail_price_toman=150_000,
        duration_days=30,
        tier="economy",
    )
    assert (a.price_toman, b.price_toman) == (110_000, 125_000)


@pytest.mark.asyncio
async def test_clearing_a_price_puts_them_back_on_retail(session: AsyncSession, shop: User) -> None:
    await set_plan_price(
        session, shop.id, "volume-plan-1", 110_000, duration_days=30, tier="economy"
    )
    assert (
        await clear_plan_price(session, shop.id, "volume-plan-1", duration_days=30, tier="economy")
        is True
    )
    assert (
        await get_plan_price(session, shop.id, "volume-plan-1", duration_days=30, tier="economy")
        is None
    )

    quote = await quote_ready_plan(
        session,
        user_id=shop.id,
        package_slug="volume-plan-1",
        retail_price_toman=150_000,
        duration_days=30,
        tier="economy",
    )
    assert quote.price_toman == 150_000


@pytest.mark.asyncio
async def test_a_free_or_negative_reseller_price_is_refused(
    session: AsyncSession, shop: User
) -> None:
    for bad in (0, -5000):
        with pytest.raises(ResellerError):
            await set_plan_price(
                session, shop.id, "volume-plan-1", bad, duration_days=30, tier="economy"
            )


# --------------------------------------------------------------------------- #
# Custom volume
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_custom_volume_uses_the_raw_rate(session: AsyncSession, shop: User) -> None:
    await set_rate(session, shop.id, price_per_gb_toman=1200, price_per_day_toman=500)
    quote = await quote_custom_volume(session, CFG, user_id=shop.id, volume_gb=50, duration_days=30)
    # 50×1200 + 30×500 = 75 000 — against retail 50×2000 = 100 000.
    assert quote.reseller_price_toman == 75_000
    assert quote.price_toman == 75_000
    assert quote.crossed_over is False


@pytest.mark.asyncio
async def test_no_rate_card_means_retail(session: AsyncSession, shop: User) -> None:
    quote = await quote_custom_volume(session, CFG, user_id=shop.id, volume_gb=50, duration_days=30)
    retail = calculate_price(
        CFG, plan_type=PlanType.VOLUME, volume_gb=50, duration_days=30
    ).final_price
    assert quote.price_toman == retail


@pytest.mark.asyncio
async def test_retail_is_compared_after_its_own_discounts(
    session: AsyncSession, shop: User
) -> None:
    """Comparing against an undiscounted list price nobody pays would let the
    reseller quietly land above what a customer is actually charged."""
    await set_rate(session, shop.id, price_per_gb_toman=1900, price_per_day_toman=0)
    quote = await quote_custom_volume(
        session, CFG, user_id=shop.id, volume_gb=100, duration_days=90
    )
    # Retail: 100×2000 = 200 000, minus 10% for 90 days = 180 000.
    assert quote.retail_price_toman == 180_000
    # Reseller: 100×1900 = 190 000 — above the *discounted* retail.
    assert quote.reseller_price_toman == 190_000
    assert quote.crossed_over is True
    assert quote.price_toman == 180_000


# --------------------------------------------------------------------------- #
# The cross-over guard
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_long_service_is_where_the_two_formulas_cross(
    session: AsyncSession, shop: User
) -> None:
    """The concrete failure the guard exists for.

    A per-day term the retail side does not have means that the longer the
    service, the closer the reseller creeps to retail — and past some duration,
    over it. Short services are fine, so this never shows up in casual testing.
    """
    await set_rate(session, shop.id, price_per_gb_toman=1200, price_per_day_toman=500)

    short = await quote_custom_volume(session, CFG, user_id=shop.id, volume_gb=50, duration_days=30)
    assert short.crossed_over is False

    long = await quote_custom_volume(session, CFG, user_id=shop.id, volume_gb=50, duration_days=365)
    assert long.reseller_price_toman > long.retail_price_toman
    assert long.crossed_over is True
    assert long.price_toman == long.retail_price_toman, "they must never pay more than retail"


@pytest.mark.asyncio
async def test_a_crossed_over_ready_plan_charges_retail_too(
    session: AsyncSession, shop: User
) -> None:
    """The owner can fat-finger a reseller price above retail as easily as a rate."""
    await set_plan_price(
        session, shop.id, "volume-plan-1", 200_000, duration_days=30, tier="economy"
    )
    quote = await quote_ready_plan(
        session,
        user_id=shop.id,
        package_slug="volume-plan-1",
        retail_price_toman=150_000,
        duration_days=30,
        tier="economy",
    )
    assert quote.crossed_over is True
    assert quote.price_toman == 150_000
    assert quote.margin_toman == 0


@pytest.mark.asyncio
async def test_equal_prices_are_not_a_cross_over(session: AsyncSession, shop: User) -> None:
    """Selling at cost is a decision, not a mistake — it must not raise a warning."""
    await set_plan_price(
        session, shop.id, "volume-plan-1", 150_000, duration_days=30, tier="economy"
    )
    quote = await quote_ready_plan(
        session,
        user_id=shop.id,
        package_slug="volume-plan-1",
        retail_price_toman=150_000,
        duration_days=30,
        tier="economy",
    )
    assert quote.crossed_over is False
    assert quote.price_toman == 150_000


@pytest.mark.asyncio
async def test_a_negative_rate_is_refused(session: AsyncSession, shop: User) -> None:
    with pytest.raises(ResellerError):
        await set_rate(session, shop.id, price_per_gb_toman=-1)


# --------------------------------------------------------------------------- #
# Checkout — the only place the agreed price actually matters
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_checkout_charges_the_reseller_price(session: AsyncSession, shop: User) -> None:
    """Every price in the bot funnels through `_resolve_price`.

    If the reseller step were wired anywhere else, an agreed price would show
    on the admin screen and never reach a real order — the feature would look
    finished and do nothing.
    """
    from services.checkout import apply_reseller_price as _apply_reseller_price
    from services.pricing import PriceResult

    await set_plan_price(
        session, shop.id, "volume-plan-1", 110_000, duration_days=30, tier="economy"
    )
    retail = PriceResult(
        original_price=150_000,
        discount_amount=0,
        final_price=150_000,
        currency="IRT",
        meta={},
    )
    data = {
        "plan_type": "volume",
        "store_package_slug": "volume-plan-1",
        "title_fa": "۵۰ گیگ / ۳۰ روز",
        "sel_duration_days": 30,
        "sel_tier": "economy",
    }
    priced = await _apply_reseller_price(session, CFG, data, retail, shop)

    assert priced.final_price == 110_000
    assert priced.original_price == 150_000
    assert priced.meta["pricing_mode"] == "reseller"
    assert priced.meta["reseller_crossed_over"] is False


@pytest.mark.asyncio
async def test_checkout_leaves_an_ordinary_customer_alone(session: AsyncSession) -> None:
    from services.checkout import apply_reseller_price as _apply_reseller_price
    from services.pricing import PriceResult

    customer = User(telegram_id=902, referral_code="CUST902")
    session.add(customer)
    await session.flush()
    # Even with a price row somehow present, the role is what decides.
    await set_plan_price(
        session, customer.id, "volume-plan-1", 10_000, duration_days=30, tier="economy"
    )

    retail = PriceResult(
        original_price=150_000,
        discount_amount=0,
        final_price=150_000,
        currency="IRT",
        meta={},
    )
    data = {
        "plan_type": "volume",
        "store_package_slug": "volume-plan-1",
        "sel_duration_days": 30,
        "sel_tier": "economy",
    }
    priced = await _apply_reseller_price(session, CFG, data, retail, customer)
    assert priced.final_price == 150_000
    assert priced.meta.get("pricing_mode") != "reseller"


@pytest.mark.asyncio
async def test_checkout_does_not_stack_a_referral_discount_on_a_wholesale_price(
    session: AsyncSession, shop: User
) -> None:
    """The discount was already taken when the price was agreed.

    Retail arrives here with the referral percent applied; the agreed price
    replaces it outright rather than being reduced again, which would let a
    shop buy below what the owner signed up to.
    """
    from services.checkout import apply_reseller_price as _apply_reseller_price
    from services.pricing import PriceResult

    await set_plan_price(
        session, shop.id, "volume-plan-1", 110_000, duration_days=30, tier="economy"
    )
    discounted_retail = PriceResult(
        original_price=150_000,
        discount_amount=15_000,  # a 10% referral discount already applied
        final_price=135_000,
        currency="IRT",
        meta={"extra_discount_percent": 10},
    )
    data = {
        "plan_type": "volume",
        "store_package_slug": "volume-plan-1",
        "sel_duration_days": 30,
        "sel_tier": "economy",
    }
    priced = await _apply_reseller_price(session, CFG, data, discounted_retail, shop)
    assert priced.final_price == 110_000, "the agreed price is not discounted again"


@pytest.mark.asyncio
async def test_checkout_prices_a_custom_volume_build(session: AsyncSession, shop: User) -> None:
    from services.checkout import apply_reseller_price as _apply_reseller_price
    from services.pricing import PriceResult

    await set_rate(session, shop.id, price_per_gb_toman=1200, price_per_day_toman=500)
    retail = PriceResult(
        original_price=100_000,
        discount_amount=0,
        final_price=100_000,
        currency="IRT",
        meta={},
    )
    data = {"plan_type": "volume", "volume_gb": 50, "duration_days": 30}
    priced = await _apply_reseller_price(session, CFG, data, retail, shop)
    assert priced.final_price == 75_000


@pytest.mark.asyncio
async def test_a_shape_with_no_reseller_rate_stays_at_retail(
    session: AsyncSession, shop: User
) -> None:
    """An unlimited custom build: the owner agreed rates for volume and fixed
    prices for ready plans, and nothing else. Retail is the honest answer."""
    from services.checkout import apply_reseller_price as _apply_reseller_price
    from services.pricing import PriceResult

    await set_rate(session, shop.id, price_per_gb_toman=1200, price_per_day_toman=500)
    retail = PriceResult(
        original_price=250_000,
        discount_amount=0,
        final_price=250_000,
        currency="IRT",
        meta={},
    )
    data = {"plan_type": "unlimited", "concurrent_users": 2, "duration_months": 1}
    priced = await _apply_reseller_price(session, CFG, data, retail, shop)
    assert priced.final_price == 250_000


@pytest.mark.asyncio
async def test_a_cross_over_at_checkout_queues_a_report(session: AsyncSession, shop: User) -> None:
    """The owner only ever learns about a bad rate when someone buys on it."""
    from sqlalchemy import select as sa_select

    from database.models import ReportOutbox
    from services.checkout import apply_reseller_price as _apply_reseller_price
    from services.pricing import PriceResult

    await set_plan_price(
        session, shop.id, "volume-plan-1", 200_000, duration_days=30, tier="economy"
    )
    retail = PriceResult(
        original_price=150_000,
        discount_amount=0,
        final_price=150_000,
        currency="IRT",
        meta={},
    )
    data = {
        "plan_type": "volume",
        "store_package_slug": "volume-plan-1",
        "title_fa": "۵۰ گیگ / ۳۰ روز",
        "sel_duration_days": 30,
        "sel_tier": "economy",
    }
    priced = await _apply_reseller_price(session, CFG, data, retail, shop)

    assert priced.final_price == 150_000, "they must never be charged above retail"
    queued = (await session.execute(sa_select(ReportOutbox))).scalars().all()
    assert len(queued) == 1
    assert "قیمت نمایندگی" in queued[0].body


@pytest.mark.asyncio
async def test_the_price_that_reaches_the_order_is_the_reseller_price(
    session: AsyncSession, shop: User, tmp_path
) -> None:
    """`_resolve_price` is not the last word — a later guard can rewrite it.

    That guard exists to stop the engine mispricing a fixed-price plan, and it
    does it by calling `_price_from_fixed`, which knows only the retail number.
    On the order-creation path it once ran unconditionally, so a reseller
    buying a ready plan was charged full retail and the whole feature did
    nothing.

    This calls the real rule rather than restating it. An earlier version of
    this test reimplemented the condition inline and therefore passed while the
    shipped code was still broken — a mutation run caught that, not a review.
    """
    from services.checkout import resolve_price as _resolve_price
    from services.checkout import should_lock_to_fixed_price

    await set_plan_price(
        session, shop.id, "volume-plan-1", 110_000, duration_days=30, tier="economy"
    )
    data = {
        "plan_type": "volume",
        "store_package_slug": "volume-plan-1",
        "fixed_price_toman": 150_000,
        "volume_gb": 50,
        "duration_days": 30,
        "title_fa": "۵۰ گیگ / ۳۰ روز",
        "sel_duration_days": 30,
        "sel_tier": "economy",
    }
    cfg_path = tmp_path / "pricing.json"
    cfg_path.write_text(json.dumps(CFG), encoding="utf-8")
    settings = SimpleNamespace(
        max_discount_percent=None,
        referral_referred_discount_percent=0,
        pricing_config_path=str(cfg_path),
    )

    priced = await _resolve_price(session, settings, data, promo_code=None, db_user=shop)
    assert priced.final_price == 110_000

    assert (
        should_lock_to_fixed_price(data, priced, promo_code=None) is False
    ), "the guard must let a reseller price through"


@pytest.mark.asyncio
async def test_the_fixed_price_guard_still_protects_an_ordinary_customer(
    session: AsyncSession, tmp_path
) -> None:
    """The guard has a job; the reseller exception must not disable it."""
    from services.checkout import resolve_price as _resolve_price
    from services.checkout import should_lock_to_fixed_price

    customer = User(telegram_id=903, referral_code="C903")
    session.add(customer)
    await session.flush()

    data = {
        "plan_type": "volume",
        "store_package_slug": "volume-plan-1",
        "fixed_price_toman": 150_000,
        "volume_gb": 50,
        "duration_days": 30,
        "sel_duration_days": 30,
        "sel_tier": "economy",
    }
    cfg_path = tmp_path / "pricing.json"
    cfg_path.write_text(json.dumps(CFG), encoding="utf-8")
    settings = SimpleNamespace(
        max_discount_percent=None,
        referral_referred_discount_percent=0,
        pricing_config_path=str(cfg_path),
    )
    priced = await _resolve_price(session, settings, data, promo_code=None, db_user=customer)
    assert should_lock_to_fixed_price(data, priced, promo_code=None) is True


def test_the_guard_stands_down_for_a_promo_and_for_custom_builds() -> None:
    """A promo means the engine's discount is the point, and a custom build has
    no fixed price to lock to."""
    from services.checkout import should_lock_to_fixed_price
    from services.pricing import PriceResult

    plain = PriceResult(
        original_price=150_000,
        discount_amount=0,
        final_price=150_000,
        currency="IRT",
        meta={},
    )
    fixed = {"fixed_price_toman": 150_000}
    assert should_lock_to_fixed_price(fixed, plain, promo_code="SAVE15") is False
    assert should_lock_to_fixed_price({}, plain, promo_code=None) is False
    assert should_lock_to_fixed_price(fixed, plain, promo_code=None) is True

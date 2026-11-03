"""Where a reseller's world must stop.

Two shops share a bot, a panel and a catalog. Every one of these tests answers
the same question from a different direction: can one shop see, price, or
touch something that belongs to another — or to the owner?
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from bot.handlers.admin import _role_label
from bot.texts import texts
from database.base import Base
from database.models import User
from database.models.pasarguard_account import PasarGuardAccount
from services.accounts import list_services_page
from services.reseller import (
    get_plan_price,
    list_plan_prices,
    quote_custom_volume,
    quote_ready_plan,
    set_plan_price,
    set_rate,
)
from services.trial import get_reseller_trial_quota

CFG = {
    "currency": "IRT",
    "volume_plan": {
        "tiers": [{"max_gb": None, "price_per_gb_toman": 2000}],
        "duration_discounts": [],
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


async def _user(session: AsyncSession, tg: int, **flags: bool) -> User:
    user = User(telegram_id=tg, referral_code=f"R{tg}", **flags)
    session.add(user)
    await session.flush()
    return user


# --------------------------------------------------------------------------- #
# Prices
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_one_shops_price_is_invisible_to_another(session: AsyncSession) -> None:
    a = await _user(session, 800, is_reseller=True)
    b = await _user(session, 801, is_reseller=True)
    await set_plan_price(session, a.id, "vip-plan", 110_000, duration_days=30, tier="economy")

    assert await get_plan_price(session, b.id, "vip-plan", duration_days=30, tier="economy") is None
    assert await list_plan_prices(session, b.id, duration_days=30, tier="economy") == {}
    quote = await quote_ready_plan(
        session,
        user_id=b.id,
        package_slug="vip-plan",
        retail_price_toman=150_000,
        duration_days=30,
        tier="economy",
    )
    assert quote.price_toman == 150_000


@pytest.mark.asyncio
async def test_one_shops_rate_card_is_invisible_to_another(session: AsyncSession) -> None:
    a = await _user(session, 802, is_reseller=True)
    b = await _user(session, 803, is_reseller=True)
    await set_rate(session, a.id, price_per_gb_toman=1000, price_per_day_toman=0)

    quote = await quote_custom_volume(session, CFG, user_id=b.id, volume_gb=50, duration_days=30)
    assert quote.price_toman == 100_000, "b pays retail, not a's rate"


@pytest.mark.asyncio
async def test_a_price_row_without_the_role_does_nothing(session: AsyncSession) -> None:
    """The role is the gate. A leftover row from a revoked reseller must not
    quietly price a plain customer."""
    from types import SimpleNamespace

    from services.checkout import apply_reseller_price as _apply_reseller_price
    from services.pricing import PriceResult

    lapsed = await _user(session, 804, is_reseller=False)
    await set_plan_price(session, lapsed.id, "vip-plan", 10_000, duration_days=30, tier="economy")

    retail = PriceResult(
        original_price=150_000,
        discount_amount=0,
        final_price=150_000,
        currency="IRT",
        meta={},
    )
    priced = await _apply_reseller_price(
        session,
        CFG,
        {"plan_type": "volume", "store_package_slug": "vip-plan"},
        retail,
        lapsed,
    )
    assert priced.final_price == 150_000
    del SimpleNamespace


@pytest.mark.asyncio
async def test_prices_survive_a_revoke_and_regrant(session: AsyncSession) -> None:
    """«حذف نمایندگی» then «نماینده کردن» must not mean re-pricing from scratch."""
    shop = await _user(session, 805, is_reseller=True)
    await set_plan_price(session, shop.id, "vip-plan", 110_000, duration_days=30, tier="economy")
    await set_rate(session, shop.id, price_per_gb_toman=1200, trial_quota_monthly=5)
    await session.commit()

    shop.is_reseller = False
    await session.commit()
    shop.is_reseller = True
    await session.commit()

    assert (
        await get_plan_price(session, shop.id, "vip-plan", duration_days=30, tier="economy")
        == 110_000
    )
    quota = await get_reseller_trial_quota(session, shop.id)
    assert quota.quota == 5


# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_shop_only_sees_its_own_customers(session: AsyncSession) -> None:
    a = await _user(session, 806, is_reseller=True)
    b = await _user(session, 807, is_reseller=True)
    for owner, n, label in ((a, 1, "مال الف"), (b, 2, "مال ب")):
        session.add(
            PasarGuardAccount(
                user_id=owner.id,
                panel_user_id=6000 + n,
                panel_username=f"neo_{n}",
                subscription_url=f"https://p/neo_{n}",
                display_name=label,
            )
        )
    await session.commit()

    page = await list_services_page(session, a.id)
    assert [r.display_name for r in page.rows] == ["مال الف"]
    assert page.total == 1


@pytest.mark.asyncio
async def test_search_cannot_reach_across_shops(session: AsyncSession) -> None:
    """A query is a filter on their own list, never a lookup across the bot."""
    a = await _user(session, 808, is_reseller=True)
    b = await _user(session, 809, is_reseller=True)
    session.add(
        PasarGuardAccount(
            user_id=b.id,
            panel_user_id=6100,
            panel_username="neo_secret",
            subscription_url="https://p/neo_secret",
            display_name="رضایی ۰۹۱۲۹۹۹",
        )
    )
    await session.commit()

    page = await list_services_page(session, a.id, query="رضایی")
    assert page.rows == []
    assert page.total == 0


# --------------------------------------------------------------------------- #
# Trial quota
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_one_shops_giveaways_do_not_spend_anothers_quota(session: AsyncSession) -> None:
    from database.models import Order, OrderStatus, Plan

    a = await _user(session, 810, is_reseller=True)
    b = await _user(session, 811, is_reseller=True)
    await set_rate(session, a.id, trial_quota_monthly=3)
    await set_rate(session, b.id, trial_quota_monthly=3)

    plan = Plan(name="trial-2GB-1d", price_toman=0, data_limit_gb=2, duration_days=1)
    session.add(plan)
    await session.flush()
    for _ in range(3):
        session.add(
            Order(
                user_id=a.id,
                plan_id=plan.id,
                status=OrderStatus.ACTIVE,
                price_toman=0,
                discount_toman=0,
                paid_at=datetime.now(UTC),
            )
        )
    await session.commit()

    assert (await get_reseller_trial_quota(session, a.id)).remaining == 0
    assert (await get_reseller_trial_quota(session, b.id)).remaining == 3


# --------------------------------------------------------------------------- #
# Role precedence
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_admin_outranks_reseller_on_the_card(session: AsyncSession) -> None:
    """Someone who is both reads as admin: that is the permission that governs."""
    both = await _user(session, 812, is_admin=True, is_reseller=True)
    assert _role_label(both) == texts.admin_customer_role_admin

    shop = await _user(session, 813, is_reseller=True)
    assert _role_label(shop) == texts.admin_customer_role_reseller

    plain = await _user(session, 814)
    assert _role_label(plain) == texts.admin_customer_role_customer

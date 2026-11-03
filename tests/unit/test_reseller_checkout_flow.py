"""A reseller's agreed cell price is what the order stores.

The shelf and the quote service were already tested apart. ``confirm_order``
is the till. A promo on a ready plan is pinned to the docstring of
``apply_reseller_price``: the agreed price replaces retail, and a code does
not undercut it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from bot.handlers import purchase
from database.base import Base
from database.models import Order, User
from services.reseller import set_plan_price, set_rate

RETAIL = 150_000
AGREED = 110_000
AGREED_WITH_PROMO = 140_000


@pytest.fixture(autouse=True)
def _store_group_has_a_panel_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests are about the till, not group lookup: give the store group its
    panel group. Resolution and the empty-group refusal are pinned in
    ``test_single_group_source.py``."""
    monkeypatch.setattr(purchase, "resolve_groups_for_plan_type", AsyncMock(return_value=[1]))


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _pricing_file(tmp_path, *, promo: bool) -> str:
    codes: dict[str, object] = {}
    if promo:
        codes = {"OFF15": {"percent": "15", "active": True}}
    doc = {
        "currency": "IRT",
        "volume_plan": {
            "tiers": [{"max_gb": None, "price_per_gb_toman": "12000"}],
            "duration_discounts": [],
        },
        "unlimited_plan": {
            "base_price_first_user_toman": "1000",
            "additional_user_coefficients": ["1"],
            "duration_discounts": [],
        },
        "promo_codes": codes,
        "sales_rules": {"discount_combine": "best_of_promo_vs_extra"},
    }
    path = tmp_path / "pricing.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def _settings(path: str) -> SimpleNamespace:
    return SimpleNamespace(
        max_discount_percent=None,
        referral_referred_discount_percent=0,
        pricing_config_path=path,
        wallet_provision_fail_credit_max_n=1,
        pasar_guard_base_url="https://panel.example.invalid",
        pasar_guard_api_key="test",
        pasar_guard_sub_path="/sub",
        pasar_guard_timeout=5,
        payment_card_number="6037991000000000",
        payment_card_holder_name="test",
        payment_bank_name="",
        payment_instructions="",
        bank_sms_enabled=False,
        admin_telegram_ids=[],
    )


async def _state(**data: object) -> FSMContext:
    ctx = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=7, user_id=7),
    )
    payload: dict[str, object] = {
        "plan_type": "volume",
        "volume_gb": 50,
        "duration_days": 30,
        "fixed_price_toman": RETAIL,
        "hwid_limit": 0,
        "service_name": "reza",
        "store_package_slug": "volume-plan-1",
        "title_fa": "۵۰ گیگ / ۳۰ روز",
        "sel_duration_days": 30,
        "sel_tier": "economy",
        "wallet_applied": 0,
    }
    payload.update(data)
    for key, value in list(payload.items()):
        if value is None:
            payload.pop(key)
    await ctx.update_data(**payload)
    return ctx


async def _shop(session: AsyncSession) -> User:
    shop = User(
        telegram_id=7001,
        referral_code="SHOP7001",
        is_reseller=True,
        first_name="موبایل",
    )
    session.add(shop)
    await session.commit()
    return shop


async def _confirm(session: AsyncSession, shop: User, path: str, **data: object) -> Order:
    # A slug'd checkout must now name an actual published package. Keep this
    # pricing regression realistic rather than injecting an unbacked FSM price.
    from database.models.store import StoreCategory, StorePackage, StorePackagePrice, StoreVersion

    if data.get("store_package_slug", "volume-plan-1") is not None:
        version = StoreVersion(
            version=1, status="published", pricing_document=json.loads(Path(path).read_text())
        )
        session.add(version)
        await session.flush()
        category = StoreCategory(
            store_version_id=version.id,
            slug="volume",
            title_fa="حجمی",
            enabled=True,
            panel_group_ids=[1],
            sales_flags={"kind": "volume"},
        )
        session.add(category)
        await session.flush()
        session.add(
            StorePackage(
                store_version_id=version.id,
                category_id=category.id,
                slug="volume-plan-1",
                kind="ready",
                title_fa="حجمی",
                enabled=True,
                options={"volume_gb": 50, "duration_days": 30, "hwid_limit": 0},
            )
        )
        session.add(
            StorePackagePrice(
                store_version_id=version.id,
                package_slug="volume-plan-1",
                duration_days=30,
                tier="economy",
                price_toman=RETAIL,
            )
        )
        await session.commit()
    await purchase.confirm_order(
        SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(answer=AsyncMock())),  # type: ignore[arg-type]
        await _state(**data),
        session,
        _settings(path),  # type: ignore[arg-type]
        shop,
    )
    return (await session.execute(select(Order).where(Order.user_id == shop.id))).scalar_one()


@pytest.mark.asyncio
async def test_confirm_stores_the_agreed_cell_not_the_shelf_price(
    session: AsyncSession, tmp_path
) -> None:
    """110,000 was agreed. Charging the 150,000 shelf is the bug this guards."""
    shop = await _shop(session)
    await set_plan_price(
        session,
        shop.id,
        "volume-plan-1",
        AGREED,
        duration_days=30,
        tier="economy",
    )
    await session.commit()
    order = await _confirm(session, shop, _pricing_file(tmp_path, promo=False))
    assert order.price_toman == AGREED


@pytest.mark.asyncio
async def test_a_promo_does_not_undercut_the_agreed_ready_plan_price(
    session: AsyncSession, tmp_path
) -> None:
    """Shelf 150,000, agreed 140,000, 15% code. The shop pays 140,000.

    15% off the shelf is 127,500. Charging that stacks the code under a price
    the owner already discounted when they agreed it.
    """
    shop = await _shop(session)
    await set_plan_price(
        session,
        shop.id,
        "volume-plan-1",
        AGREED_WITH_PROMO,
        duration_days=30,
        tier="economy",
    )
    await session.commit()
    order = await _confirm(
        session,
        shop,
        _pricing_file(tmp_path, promo=True),
        promo_code="OFF15",
    )
    assert order.price_toman == AGREED_WITH_PROMO


@pytest.mark.asyncio
async def test_custom_volume_charges_that_shops_per_gig_rate(
    session: AsyncSession, tmp_path
) -> None:
    """10 GB at 1,000 toman/GB is 10,000, and a promo code does not move it."""
    shop = await _shop(session)
    await set_rate(session, shop.id, price_per_gb_toman=1_000, price_per_day_toman=0)
    await session.commit()
    order = await _confirm(
        session,
        shop,
        _pricing_file(tmp_path, promo=True),
        fixed_price_toman=None,
        store_package_slug=None,
        volume_gb=10,
        duration_days=30,
        promo_code="OFF15",
    )
    assert order.price_toman == 10_000

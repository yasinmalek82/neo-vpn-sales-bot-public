"""Renew stays on the service that was bought, at today's price of that plan."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from bot.handlers import purchase
from bot.handlers.purchase import _published_sale_categories
from bot.keyboards.account import AccountCB, service_detail_keyboard
from bot.keyboards.store_admin import store_cat_panel_keyboard
from bot.states import PurchaseStates
from bot.texts import texts
from database.base import Base
from database.models import Order, OrderStatus, PasarGuardAccount, Plan, User
from database.models.plan import DataLimitResetStrategy
from database.models.store import (
    StoreCategory,
    StorePackage,
    StorePackageKind,
    StorePackagePrice,
    StoreVersion,
    StoreVersionStatus,
)
from services.orders import create_pending_order
from services.pricing import PriceResult
from services.renewal import RenewUnavailable, renew_checkout_data

OLD_PRICE = 200_000
NEW_PRICE = 250_000


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        max_discount_percent=None,
        default_panel_group_ids=[1],
        referral_referred_discount_percent=0,
    )


async def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=1, user_id=1),
    )


class _Message:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def answer(self, text: str, reply_markup: object = None) -> None:
        _ = reply_markup
        self.texts.append(text)


async def _catalog(session: AsyncSession) -> tuple[StoreCategory, StoreCategory]:
    version = StoreVersion(
        version=1,
        status=StoreVersionStatus.published,
        pricing_document={"currency": "IRT"},
        purchase_steps=[],
        sales_rules={},
    )
    session.add(version)
    await session.flush()
    germany = StoreCategory(
        store_version_id=version.id,
        slug="unl-de",
        title_fa="نامحدود آلمان",
        enabled=True,
        sort_order=1,
        panel_group_ids=[11],
        sales_flags={"kind": "unlimited", "allow_new_sale": True, "durations": [30]},
    )
    turkey = StoreCategory(
        store_version_id=version.id,
        slug="unl-tr",
        title_fa="نامحدود ترکیه",
        enabled=True,
        sort_order=2,
        panel_group_ids=[22],
        sales_flags={"kind": "unlimited", "allow_new_sale": True, "durations": [30]},
    )
    session.add_all([germany, turkey])
    await session.flush()
    for cat, slug, price in (
        (germany, "de-30", NEW_PRICE),
        (turkey, "tr-30", 180_000),
    ):
        session.add(
            StorePackage(
                store_version_id=version.id,
                category_id=cat.id,
                slug=slug,
                kind=StorePackageKind.ready,
                title_fa=cat.title_fa,
                enabled=True,
                fixed_price_toman=price,
                options={"concurrent_users": 1, "duration_months": 1},
            )
        )
        session.add(
            StorePackagePrice(
                store_version_id=version.id,
                package_slug=slug,
                duration_days=30,
                tier="economy",
                price_toman=price,
            )
        )
    await session.commit()
    return germany, turkey


async def _bought(
    session: AsyncSession,
    *,
    germany: StoreCategory,
    slug: str | None,
    group_ids: list[int] | None,
) -> tuple[User, PasarGuardAccount]:
    user = User(telegram_id=501, referral_code="REN501", wallet_balance_toman=0)
    plan = Plan(
        name="unlimited-1u-1m",
        description="t",
        data_limit_gb=0,
        duration_days=30,
        price_toman=OLD_PRICE,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=group_ids or [11],
        hwid_limit=1,
        is_active=True,
    )
    session.add_all([user, plan])
    await session.flush()
    account = PasarGuardAccount(
        user_id=user.id,
        panel_user_id=9,
        panel_username="reza_de",
        plan_fingerprint="unlimited:1:30",
    )
    session.add(account)
    await session.flush()
    session.add(
        Order(
            user_id=user.id,
            plan_id=plan.id,
            status=OrderStatus.ACTIVE,
            price_toman=OLD_PRICE,
            discount_toman=0,
            panel_username="reza_de",
            renew_account_id=account.id,
            store_package_slug=slug,
            delivery_data_limit_gb=0,
            delivery_duration_days=30,
            delivery_hwid_limit=1,
            delivery_group_ids=group_ids,
        )
    )
    await session.commit()
    _ = germany
    return user, account


def test_a_healthy_service_still_offers_renew() -> None:
    labels = [
        b.text for row in service_detail_keyboard(4, has_link=True).inline_keyboard for b in row
    ]
    callbacks = [
        b.callback_data
        for row in service_detail_keyboard(4, has_link=True).inline_keyboard
        for b in row
    ]
    assert any("تمدید این سرویس" in text for text in labels)
    assert AccountCB.svc_renew(4) in callbacks


def test_the_category_screen_offers_the_panel_group_setting() -> None:
    # Several panel groups are allowed now (owner, 2026-09-30); the picker's
    # own tests are in test_panel_group_picker.py.
    cat = SimpleNamespace(id=3, parent_id=None, sales_flags={})
    labels = [b.text for row in store_cat_panel_keyboard(cat).inline_keyboard for b in row]
    assert any("گروه پنل" in text for text in labels)
    assert not any("سرورهای این گروه" in text for text in labels)


@pytest.mark.asyncio
async def test_two_unlimited_locations_stay_separate_on_the_shelf_and_on_renew(
    session: AsyncSession,
) -> None:
    germany, _turkey = await _catalog(session)
    titles = {cat.title_fa for cat in await _published_sale_categories(session)}
    assert titles == {"نامحدود آلمان", "نامحدود ترکیه"}

    user, account = await _bought(session, germany=germany, slug="de-30", group_ids=[11])
    data = await renew_checkout_data(session, user_id=user.id, account_id=account.id)
    assert data["store_package_slug"] == "de-30"
    assert data["fixed_price_toman"] == NEW_PRICE
    assert data["store_category_id"] == germany.id
    assert data["renew_account_id"] == account.id

    message = _Message()
    ctx = await _state()
    await purchase.start_renew_purchase(
        message,  # type: ignore[arg-type]
        ctx,
        session,
        _settings(),  # type: ignore[arg-type]
        user,
        account_id=account.id,
    )
    assert message.texts == [texts.purchase_enter_promo], "a retail renew asks for a code first"
    callback = SimpleNamespace(answer=AsyncMock(), message=message)
    await purchase.promo_skip(callback, ctx, session, _settings(), user)  # type: ignore[arg-type]
    assert texts.purchase_choose_type not in "".join(message.texts)
    assert "۲۵۰٬۰۰۰" in message.texts[-1]


@pytest.mark.asyncio
async def test_renew_quotes_todays_price_and_stamps_the_slug(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    germany, _turkey = await _catalog(session)
    user, account = await _bought(session, germany=germany, slug="de-30", group_ids=[11])
    ctx = await _state()
    message = _Message()
    await purchase.start_renew_purchase(
        message,  # type: ignore[arg-type]
        ctx,
        session,
        _settings(),  # type: ignore[arg-type]
        user,
        account_id=account.id,
    )
    assert await ctx.get_state() == PurchaseStates.entering_promo.state
    skip = SimpleNamespace(answer=AsyncMock(), message=message)
    await purchase.promo_skip(skip, ctx, session, _settings(), user)  # type: ignore[arg-type]
    data = await ctx.get_data()
    assert data["final_price"] == NEW_PRICE
    assert await ctx.get_state() == PurchaseStates.confirming.state

    monkeypatch.setattr(purchase, "begin_payment_wait", AsyncMock())
    callback = SimpleNamespace(answer=AsyncMock(), message=message)
    await purchase.confirm_order(
        callback,  # type: ignore[arg-type]
        ctx,
        session,
        _settings(),  # type: ignore[arg-type]
        user,
    )
    order = (
        await session.execute(
            select(Order).where(
                Order.user_id == user.id, Order.status == OrderStatus.PENDING_PAYMENT
            )
        )
    ).scalar_one()
    assert order.price_toman == NEW_PRICE
    assert order.store_package_slug == "de-30"
    assert order.renew_account_id == account.id


@pytest.mark.asyncio
async def test_a_disabled_plan_offers_any_plan_on_the_same_service(
    session: AsyncSession,
) -> None:
    """It used to say «سرویس تازه بخر», which meant a new link to import.

    Every account imported from the old bot has no plan here, and an unlimited
    customer moving to the tunnel has a plan that still exists but is not the
    one they want. Both now pick from the store onto this same account.
    """
    germany, _turkey = await _catalog(session)
    package = (
        await session.execute(select(StorePackage).where(StorePackage.slug == "de-30"))
    ).scalar_one()
    package.enabled = False
    await session.commit()
    user, account = await _bought(session, germany=germany, slug="de-30", group_ids=[11])
    with pytest.raises(RenewUnavailable):
        await renew_checkout_data(session, user_id=user.id, account_id=account.id)

    message = _Message()
    state = await _state()
    await purchase.start_renew_purchase(
        message,  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        user,
        account_id=account.id,
    )
    assert message.texts == [texts.purchase_switch_plan_gone]
    data = await state.get_data()
    assert data["intent_renew_account_id"] == account.id
    assert data["renew_resets"] is True
    assert await session.scalar(select(func.count()).select_from(Order)) == 1, "nothing new"


@pytest.mark.asyncio
async def test_a_disabled_category_blocks_renew_of_a_live_plan(session: AsyncSession) -> None:
    germany, _turkey = await _catalog(session)
    germany.enabled = False
    await session.commit()
    user, account = await _bought(session, germany=germany, slug="de-30", group_ids=[11])
    with pytest.raises(RenewUnavailable):
        await renew_checkout_data(session, user_id=user.id, account_id=account.id)


@pytest.mark.asyncio
async def test_two_locations_with_no_slug_are_not_guessed(session: AsyncSession) -> None:
    germany, _turkey = await _catalog(session)
    user, account = await _bought(session, germany=germany, slug=None, group_ids=None)
    with pytest.raises(RenewUnavailable):
        await renew_checkout_data(session, user_id=user.id, account_id=account.id)


@pytest.mark.asyncio
async def test_an_old_order_without_a_slug_follows_its_panel_group(
    session: AsyncSession,
) -> None:
    germany, _turkey = await _catalog(session)
    user, account = await _bought(session, germany=germany, slug=None, group_ids=[11])
    data = await renew_checkout_data(session, user_id=user.id, account_id=account.id)
    assert data["store_package_slug"] == "de-30"
    assert data["store_category_id"] == germany.id


@pytest.mark.asyncio
async def test_create_pending_order_keeps_the_package_slug(session: AsyncSession) -> None:
    user = User(telegram_id=502, referral_code="REN502")
    plan = Plan(
        name="volume-10GB-30d",
        description="t",
        data_limit_gb=10,
        duration_days=30,
        price_toman=100_000,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1],
        hwid_limit=0,
        is_active=True,
    )
    session.add_all([user, plan])
    await session.flush()
    order = await create_pending_order(
        session,
        user_id=user.id,
        plan=plan,
        price=PriceResult(
            original_price=100_000,
            discount_amount=0,
            final_price=100_000,
            currency="IRT",
            meta={},
        ),
        store_package_slug="de-30",
    )
    assert order.store_package_slug == "de-30"

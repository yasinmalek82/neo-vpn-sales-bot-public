"""An old checkout cannot sell changed or withdrawn published terms."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from bot.config import Settings
from bot.handlers import purchase
from bot.texts import texts
from database.base import Base
from database.models import Order, User, WalletLedgerEntry
from database.models.store import (
    StoreCategory,
    StorePackage,
    StorePackagePrice,
    StoreVersion,
    StoreVersionStatus,
)
from services.store_catalog import ready_package_purchase_payload
from services.store_config import get_published_store_config


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def _version(session, number, **changes):
    version = StoreVersion(
        version=number, status=StoreVersionStatus.published, pricing_document={"currency": "IRT"}
    )
    session.add(version)
    await session.flush()
    cat = StoreCategory(
        store_version_id=version.id,
        slug="volume",
        title_fa="حجمی",
        panel_group_ids=changes.get("groups", [11]),
        enabled=changes.get("category_enabled", True),
        sales_flags={"kind": "volume", "flat_plan_list": True},
    )
    session.add(cat)
    await session.flush()
    pkg = StorePackage(
        kind="ready",
        store_version_id=version.id,
        category_id=cat.id,
        slug="p50",
        title_fa="حجمی",
        enabled=changes.get("enabled", True),
        allow_new_sale=changes.get("allow_new_sale", True),
        options={
            "volume_gb": changes.get("volume", 50),
            "duration_days": changes.get("days", 30),
            "hwid_limit": changes.get("hwid", 0),
        },
    )
    session.add(pkg)
    if not changes.get("remove_cell"):
        session.add(
            StorePackagePrice(
                store_version_id=version.id,
                package_slug="p50",
                duration_days=changes.get("days", 30),
                tier="economy",
                price_toman=changes.get("price", 100_000),
            )
        )
    await session.commit()
    if changes.get("delete"):
        await session.delete(pkg)
        await session.commit()
    return version, cat, pkg


async def _checkout(session):
    user = User(telegram_id=81818, referral_code="probe", wallet_balance_toman=20_000)
    session.add(user)
    version, cat, pkg = await _version(session, 1)
    cfg = await get_published_store_config(session)
    data = ready_package_purchase_payload(pkg, list(cfg.prices))
    data.update(
        plan_type="volume",
        store_category_id=cat.id,
        store_category_slug="volume",
        sel_duration_days=30,
        sel_tier="economy",
        service_name="ali",
        force_new=True,
    )
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.set_data(data)
    msg = SimpleNamespace(answer=AsyncMock(), edit_reply_markup=AsyncMock())
    cb = SimpleNamespace(answer=AsyncMock(), message=msg)
    settings = Settings(_env_file=None, BOT_TOKEN="test-token", PANEL_WATCH_INTERVAL_SECONDS=0)
    await purchase._show_summary(msg, state, session, settings, None, user)
    version.status = StoreVersionStatus.archived
    await session.commit()
    return user, state, cb, settings


@pytest.mark.parametrize(
    "change", [{"price": 200_000}, {"volume": 75}, {"days": 90}, {"hwid": 2}, {"groups": [22]}]
)
async def test_changed_terms_need_new_consent(session, change):
    # Removing live refresh/consent would create an order and debit on the first tap.
    user, state, cb, settings = await _checkout(session)
    await _version(session, 2, **change)
    await purchase.confirm_order(cb, state, session, settings, user)
    assert (await session.scalars(select(Order))).all() == []
    assert (await session.scalars(select(WalletLedgerEntry))).all() == []
    assert any(c.args[0] == texts.purchase_price_updated for c in cb.message.answer.call_args_list)
    await purchase.confirm_order(cb, state, session, settings, user)
    order = (await session.scalars(select(Order))).one()
    assert order.price_toman == change.get("price", 100_000)
    assert order.delivery_data_limit_gb == change.get("volume", 50)
    assert order.delivery_duration_days == change.get("days", 30)
    assert order.delivery_hwid_limit == change.get("hwid", 0)
    assert order.delivery_group_ids == change.get("groups", [11])
    assert order.force_new is True


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": False},
        {"allow_new_sale": False},
        {"category_enabled": False},
        {"delete": True},
        {"remove_cell": True},
    ],
)
async def test_withdrawn_plan_refuses_without_money_changes(session, change):
    user, state, cb, settings = await _checkout(session)
    await _version(session, 2, **change)
    await purchase.confirm_order(cb, state, session, settings, user)
    assert (await session.scalars(select(Order))).all() == []
    assert (await session.scalars(select(WalletLedgerEntry))).all() == []
    assert user.wallet_balance_toman == 20_000


async def test_unrelated_publication_does_not_require_an_extra_tap(session):
    user, state, cb, settings = await _checkout(session)
    await _version(session, 2)
    await purchase.confirm_order(cb, state, session, settings, user)
    order = (await session.scalars(select(Order))).one()
    assert order.price_toman == 100_000
    assert order.wallet_applied_toman == 20_000
    assert user.wallet_balance_toman == 0
    assert not any(
        c.args[0] == texts.purchase_price_updated for c in cb.message.answer.call_args_list
    )


@pytest.mark.parametrize(
    "live_price,want,reconfirm", [(200_000, 80_000, False), (60_000, 60_000, True)]
)
async def test_live_retail_refresh_keeps_agreed_reseller_price_and_crossover(
    session, live_price, want, reconfirm
):
    from services.reseller import set_plan_price

    user, state, cb, settings = await _checkout(session)
    user.is_reseller = True
    await _version(session, 2, price=live_price)
    await set_plan_price(session, user.id, "p50", 80_000, duration_days=30, tier="economy")
    await purchase._show_summary(cb.message, state, session, settings, None, user)
    await session.commit()
    await purchase.confirm_order(cb, state, session, settings, user)
    if reconfirm:
        assert (await session.scalars(select(Order))).all() == []
        assert (await session.scalars(select(WalletLedgerEntry))).all() == []
        await purchase.confirm_order(cb, state, session, settings, user)
    order = (await session.scalars(select(Order))).one()
    assert order.price_toman == want


async def test_refresh_keeps_custom_and_reset_renew_intent(session):
    from services.checkout import refresh_ready_checkout

    data = {
        "plan_type": "volume",
        "volume_gb": 37,
        "duration_days": 90,
        "custom_purchase": True,
        "renew_mode": "reset",
        "renew_account_id": 8,
        "force_new": False,
    }
    assert await refresh_ready_checkout(session, data) == data


async def test_ready_renew_keeps_original_device_limit_and_explicit_account(session):
    from services.checkout import refresh_ready_checkout

    user, state, cb, settings = await _checkout(session)
    _, _, pkg = await _version(session, 2, hwid=2)
    data = await state.get_data()
    data.update(renew_account_id=8, force_new=False, hwid_limit=0)
    refreshed = await refresh_ready_checkout(session, data)
    assert refreshed["renew_account_id"] == 8
    assert refreshed["force_new"] is False
    assert refreshed["hwid_limit"] == 0
    pkg.allow_renew = False
    await session.commit()
    await state.set_data(data)
    await purchase.confirm_order(cb, state, session, settings, user)
    assert (await session.scalars(select(Order))).all() == []
    assert (await session.scalars(select(WalletLedgerEntry))).all() == []

"""Custom sales are rechecked at the till, without blocking existing renewals."""

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from bot.config import Settings
from bot.handlers import purchase
from bot.states import PurchaseStates
from bot.texts import texts
from database.base import Base
from database.models import Order, PasarGuardAccount, User, WalletLedgerEntry
from database.models.store import StoreCategory, StoreVersion, StoreVersionStatus
from services.reseller import set_rate
from services.store_catalog import custom_purchase_offered


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        yield db
    await engine.dispose()


PRICING = {
    "currency": "IRT",
    "volume_plan": {
        "volume_options_gb": [10],
        "duration_options_days": [30],
        "tiers": [{"max_gb": None, "price_per_gb_toman": "12000"}],
        "duration_discounts": [],
    },
    "promo_codes": {},
}


async def _version(
    session: AsyncSession, number: int, mode: str
) -> tuple[StoreVersion, StoreCategory]:
    version = StoreVersion(
        version=number, status=StoreVersionStatus.published, pricing_document=PRICING
    )
    session.add(version)
    await session.flush()
    category = StoreCategory(
        store_version_id=version.id,
        slug="volume",
        title_fa="حجمی",
        panel_group_ids=[11],
        enabled=True,
        sales_flags={"kind": "volume", "custom_purchase_mode": mode},
    )
    session.add(category)
    await session.commit()
    return version, category


async def _summary(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reseller: bool = False,
    renewing: bool = False,
    switching: bool = False,
) -> tuple[User, StoreVersion, FSMContext, SimpleNamespace, Settings, AsyncMock, AsyncMock]:
    user = User(
        telegram_id=818181, referral_code="probe", wallet_balance_toman=20_000, is_reseller=reseller
    )
    session.add(user)
    await session.flush()
    if reseller:
        await set_rate(session, user.id, price_per_gb_toman=9_000)
    version, category = await _version(session, 1, "everyone")
    assert custom_purchase_offered(
        category, PRICING, is_reseller=reseller, has_per_gb_rate=reseller
    )
    data = {
        "plan_type": "volume",
        "volume_gb": 10,
        "duration_days": 30,
        "store_category_id": category.id,
        "store_category_slug": category.slug,
        "custom_purchase": True,
        "force_new": not (renewing or switching),
        "service_name": "test",
    }
    if renewing or switching:
        account = PasarGuardAccount(user_id=user.id, panel_user_id=1, panel_username="test")
        session.add(account)
        await session.commit()
        data["renew_account_id"] = account.id
        if switching:
            data["renew_resets"] = True
        else:
            data["renew_mode"] = "reset"
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.set_data(data)
    message = SimpleNamespace(answer=AsyncMock(), edit_reply_markup=AsyncMock())
    callback = SimpleNamespace(answer=AsyncMock(), message=message)
    settings = Settings(_env_file=None, BOT_TOKEN="test-token", PANEL_WATCH_INTERVAL_SECONDS=0)
    reserve = AsyncMock(return_value=None)
    payment = AsyncMock()
    monkeypatch.setattr(purchase, "refuse_if_panel_down", AsyncMock(return_value=False))
    monkeypatch.setattr(purchase, "reserve_bank_invoice", reserve)
    monkeypatch.setattr(purchase, "begin_payment_wait", payment)
    await purchase._show_summary(message, state, session, settings, None, user)
    assert await state.get_state() == PurchaseStates.confirming.state
    assert (await state.get_data())["final_price"] == (90_000 if reseller else 120_000)
    return user, version, state, callback, settings, reserve, payment


async def _republish(session: AsyncSession, old: StoreVersion, mode: str) -> StoreCategory:
    old.status = StoreVersionStatus.archived
    _, category = await _version(session, 2, mode)
    return category


async def _assert_no_order_or_debit(session: AsyncSession, user: User) -> None:
    assert (await session.scalars(select(Order))).all() == []
    assert (await session.scalars(select(WalletLedgerEntry))).all() == []
    assert user.wallet_balance_toman == 20_000


@pytest.mark.parametrize("mode", ["off", "resellers"])
async def test_retail_custom_sale_withdrawn_after_summary_refuses_before_money(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    user, version, state, callback, settings, reserve, payment = await _summary(
        session, monkeypatch
    )
    category = await _republish(session, version, mode)
    assert not custom_purchase_offered(category, PRICING, is_reseller=False, has_per_gb_rate=False)

    await purchase.confirm_order(callback, state, session, settings, user)

    await _assert_no_order_or_debit(session, user)
    reserve.assert_not_awaited()
    payment.assert_not_awaited()
    assert await state.get_state() == PurchaseStates.choosing_plan_type.state
    assert any(
        c.args[0] == texts.purchase_custom_not_offered
        for c in callback.message.answer.call_args_list
    )


async def test_reseller_losing_per_gb_rate_after_summary_is_not_charged_retail(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, _, state, callback, settings, reserve, payment = await _summary(
        session, monkeypatch, reseller=True
    )
    await set_rate(session, user.id, price_per_gb_toman=0)
    await session.commit()

    await purchase.confirm_order(callback, state, session, settings, user)
    # A changed price may cause a second summary. A second confirm must not
    # turn the missing reseller rate into an unannounced retail purchase.
    await purchase.confirm_order(callback, state, session, settings, user)

    await _assert_no_order_or_debit(session, user)
    reserve.assert_not_awaited()
    payment.assert_not_awaited()


async def test_unchanged_custom_sale_still_creates_order(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, _, state, callback, settings, reserve, payment = await _summary(session, monkeypatch)

    await purchase.confirm_order(callback, state, session, settings, user)

    order = (await session.scalars(select(Order))).one()
    assert order.price_toman == 120_000
    assert order.custom_purchase is True
    assert user.wallet_balance_toman == 0
    reserve.assert_awaited_once()
    payment.assert_awaited_once()


async def test_custom_renewal_still_allowed_when_new_custom_sales_are_off(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, version, state, callback, settings, reserve, payment = await _summary(
        session, monkeypatch, renewing=True
    )
    await _republish(session, version, "off")

    await purchase.confirm_order(callback, state, session, settings, user)

    order = (await session.scalars(select(Order))).one()
    assert order.renew_account_id is not None
    assert order.renew_mode == "reset"
    assert order.custom_purchase is True
    reserve.assert_awaited_once()
    payment.assert_awaited_once()


async def test_custom_plan_switch_is_not_allowed_after_custom_sales_are_turned_off(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, version, state, callback, settings, reserve, payment = await _summary(
        session, monkeypatch, switching=True
    )
    await _republish(session, version, "off")

    await purchase.confirm_order(callback, state, session, settings, user)

    await _assert_no_order_or_debit(session, user)
    reserve.assert_not_awaited()
    payment.assert_not_awaited()

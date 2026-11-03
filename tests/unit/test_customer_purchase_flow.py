"""The till, not the price engine: confirm_order is what the customer pays.

Wallet split, renew-versus-new, and the one-open-order gate used to be proven
only as separate services. These drive ``confirm_order`` and ``quote_checkout``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
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
from api_client.exceptions import PasarGuardNotFoundError
from api_client.models import UserResponse, UserStatus
from bot.handlers import purchase
from bot.texts import texts
from database.base import Base
from database.models import (
    Order,
    OrderStatus,
    PasarGuardAccount,
    Payment,
    Plan,
    User,
    WalletLedgerEntry,
)
from database.models.plan import DataLimitResetStrategy
from services.orders import create_pending_order
from services.pricing import PriceResult
from services.wallet import REASON_CHECKOUT_DEBIT, get_balance

FIXED = 50_000


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


def _pricing_file(tmp_path, promo: bool = False) -> str:
    doc: dict[str, object] = {
        "currency": "IRT",
        "volume_plan": {
            "tiers": [{"max_gb": None, "price_per_gb_toman": "1000"}],
            "duration_discounts": [],
        },
        "unlimited_plan": {
            "base_price_first_user_toman": "1000",
            "additional_user_coefficients": ["1"],
            "duration_discounts": [],
        },
        "promo_codes": {},
        "sales_rules": {"discount_combine": "best_of_promo_vs_extra"},
    }
    if promo:
        doc["promo_codes"] = {"OFF15": {"percent": "15", "active": True}}
    path = tmp_path / "pricing.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def _settings(path: str, **overrides: object) -> SimpleNamespace:
    data: dict[str, object] = {
        "max_discount_percent": None,
        "referral_referred_discount_percent": 0,
        "pricing_config_path": path,
        "wallet_provision_fail_credit_max_n": 1,
        "pasar_guard_base_url": "https://panel.example.invalid",
        "pasar_guard_api_key": "test",
        "pasar_guard_sub_path": "/sub",
        "pasar_guard_timeout": 5,
        "payment_card_number": "6037991000000000",
        "payment_card_holder_name": "test",
        "payment_bank_name": "",
        "payment_instructions": "",
        "bank_sms_enabled": False,
        "admin_telegram_ids": [],
    }
    data.update(overrides)
    return SimpleNamespace(**data)


async def _state(data: dict[str, object]) -> FSMContext:
    ctx = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=1, user_id=1),
    )
    await ctx.update_data(**data)
    return ctx


class _Message:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def answer(self, text: str, reply_markup: object = None) -> None:
        _ = reply_markup
        self.texts.append(text)


def _callback(message: _Message) -> SimpleNamespace:
    return SimpleNamespace(answer=AsyncMock(), message=message)


def _purchase_data(**extra: object) -> dict[str, object]:
    data: dict[str, object] = {
        "plan_type": "volume",
        "volume_gb": 10,
        "duration_days": 30,
        "fixed_price_toman": FIXED,
        "hwid_limit": 0,
        "service_name": "ali",
        "sel_duration_days": 30,
        "wallet_applied": 0,
    }
    data.update(extra)
    return data


async def _customer(session: AsyncSession, *, balance: int, telegram_id: int) -> User:
    user = User(
        telegram_id=telegram_id,
        referral_code=f"C{telegram_id}",
        wallet_balance_toman=balance,
    )
    session.add(user)
    await session.commit()
    return user


class _Panel:
    async def get_user(self, username: str) -> UserResponse:
        _ = username
        raise PasarGuardNotFoundError("missing")

    async def create_user(self, payload: object) -> UserResponse:
        username = str(payload.username)
        return UserResponse(
            id=71,
            username=username,
            status=UserStatus.ACTIVE,
            used_traffic=0,
            created_at=datetime.now(UTC),
            subscription_url=f"https://panel.example.invalid/sub/{username}",
            data_limit=10 * 1024**3,
        )

    def subscription_url_from_response(self, panel_user: UserResponse) -> str:
        return str(panel_user.subscription_url)

    async def aclose(self) -> None:
        return None


def _patch_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    async def client(*_args: object, **_kwargs: object) -> _Panel:
        return _Panel()

    monkeypatch.setattr(purchase, "get_pasarguard_client", client)
    monkeypatch.setattr(purchase, "deliver_provisioned_service_card", AsyncMock())


@pytest.mark.asyncio
async def test_a_wallet_that_covers_the_plan_pays_the_fixed_price(
    session: AsyncSession, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full cover debits the plan price and never opens a card wait."""
    _patch_panel(monkeypatch)
    user = await _customer(session, balance=FIXED, telegram_id=1001)
    message = _Message()
    await purchase.confirm_order(
        _callback(message),  # type: ignore[arg-type]
        await _state(_purchase_data(wallet_applied=FIXED)),
        session,
        _settings(_pricing_file(tmp_path)),  # type: ignore[arg-type]
        user,
    )
    order = (await session.execute(select(Order))).scalar_one()
    payment = (await session.execute(select(Payment))).scalar_one()
    debit = (await session.execute(select(WalletLedgerEntry))).scalar_one()
    assert order.price_toman == FIXED
    assert order.status == OrderStatus.ACTIVE
    assert order.wallet_applied_toman == FIXED
    assert payment.method == "wallet"
    assert debit.reason == REASON_CHECKOUT_DEBIT
    assert debit.amount_toman == FIXED
    assert await get_balance(session, user.id) == 0
    assert texts.purchase_created not in "".join(message.texts)


@pytest.mark.asyncio
async def test_a_partial_wallet_leaves_the_remainder_for_the_card(
    session: AsyncSession, tmp_path
) -> None:
    """20,000 in the wallet on a 50,000 plan is a 30,000 card, not a wallet sale."""
    user = await _customer(session, balance=20_000, telegram_id=1002)
    state = await _state(_purchase_data(wallet_applied=20_000))
    await purchase.confirm_order(
        _callback(_Message()),  # type: ignore[arg-type]
        state,
        session,
        _settings(_pricing_file(tmp_path)),  # type: ignore[arg-type]
        user,
    )
    order = (await session.execute(select(Order))).scalar_one()
    payments = (await session.execute(select(Payment))).scalars().all()
    saved = await state.get_data()
    assert order.price_toman == FIXED
    assert order.wallet_applied_toman == 20_000
    assert order.status == OrderStatus.PENDING_PAYMENT
    assert payments == []
    assert saved["pending_amount"] == 30_000
    assert await get_balance(session, user.id) == 0


@pytest.mark.asyncio
async def test_renew_same_and_new_are_written_on_the_order(session: AsyncSession, tmp_path) -> None:
    """The tap has to reach the order. Provision already trusts these two fields."""
    renewing = await _customer(session, balance=0, telegram_id=1003)
    account = PasarGuardAccount(
        user_id=renewing.id,
        panel_user_id=9,
        panel_username="ali_old",
    )
    session.add(account)
    await session.commit()
    await purchase.confirm_order(
        _callback(_Message()),  # type: ignore[arg-type]
        await _state(_purchase_data(renew_account_id=account.id, force_new=False)),
        session,
        _settings(_pricing_file(tmp_path)),  # type: ignore[arg-type]
        renewing,
    )
    renewed = (
        await session.execute(select(Order).where(Order.user_id == renewing.id))
    ).scalar_one()
    assert renewed.renew_account_id == account.id
    assert renewed.force_new is False

    fresh = await _customer(session, balance=0, telegram_id=1004)
    await purchase.confirm_order(
        _callback(_Message()),  # type: ignore[arg-type]
        await _state(_purchase_data(renew_account_id=None, force_new=True)),
        session,
        _settings(_pricing_file(tmp_path)),  # type: ignore[arg-type]
        fresh,
    )
    created = (await session.execute(select(Order).where(Order.user_id == fresh.id))).scalar_one()
    assert created.renew_account_id is None
    assert created.force_new is True


@pytest.mark.asyncio
async def test_an_open_order_blocks_a_second_confirm(session: AsyncSession, tmp_path) -> None:
    user = await _customer(session, balance=0, telegram_id=1005)
    plan = Plan(
        name="already-open",
        description="test",
        data_limit_gb=10,
        duration_days=30,
        price_toman=40_000,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1],
        hwid_limit=0,
        is_active=True,
    )
    session.add(plan)
    await session.flush()
    open_order = await create_pending_order(
        session,
        user_id=user.id,
        plan=plan,
        price=PriceResult(
            original_price=40_000,
            discount_amount=0,
            final_price=40_000,
            currency="IRT",
            meta={"volume_gb": 10, "duration_days": 30},
        ),
    )
    await session.commit()
    await purchase.confirm_order(
        _callback(_Message()),  # type: ignore[arg-type]
        await _state(_purchase_data(wallet_applied=0)),
        session,
        _settings(_pricing_file(tmp_path)),  # type: ignore[arg-type]
        user,
    )
    orders = (await session.execute(select(Order))).scalars().all()
    assert [row.id for row in orders] == [open_order.id]
    assert orders[0].price_toman == 40_000


@pytest.mark.asyncio
async def test_referred_discount_applies_only_before_the_first_paid_order(
    session: AsyncSession, tmp_path
) -> None:
    """The percent lives on quote_checkout, and it stops after one paid order."""
    from services.checkout import quote_checkout

    referrer = User(telegram_id=1006, referral_code="REF1006")
    session.add(referrer)
    await session.flush()
    buyer = User(
        telegram_id=1007,
        referral_code="BUY1007",
        referred_by_id=referrer.id,
    )
    session.add(buyer)
    await session.flush()
    settings = _settings(
        _pricing_file(tmp_path),
        referral_referred_discount_percent=5,
    )
    data = _purchase_data(fixed_price_toman=150_000)
    first = await quote_checkout(
        session,
        settings,
        data,
        promo_code=None,
        db_user=buyer,
    )
    assert first.final_price == 142_500

    plan = Plan(
        name="paid-once",
        description="test",
        data_limit_gb=10,
        duration_days=30,
        price_toman=142_500,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1],
        hwid_limit=0,
        is_active=True,
    )
    session.add(plan)
    await session.flush()
    session.add(
        Order(
            user_id=buyer.id,
            plan_id=plan.id,
            status=OrderStatus.ACTIVE,
            price_toman=142_500,
        )
    )
    await session.flush()
    second = await quote_checkout(
        session,
        settings,
        data,
        promo_code=None,
        db_user=buyer,
    )
    assert second.final_price == 150_000


@pytest.mark.asyncio
async def test_a_stale_wallet_figure_does_not_spend_money(session: AsyncSession, tmp_path) -> None:
    """The summary remembered 50,000; the wallet now holds 10,000.

    The till re-reads the balance, so the order takes 10,000 from the wallet
    and leaves 40,000 for the card.
    """
    user = await _customer(session, balance=10_000, telegram_id=1008)
    message = _Message()
    state = await _state(_purchase_data(wallet_applied=FIXED))
    await purchase.confirm_order(
        _callback(message),  # type: ignore[arg-type]
        state,
        session,
        _settings(_pricing_file(tmp_path)),  # type: ignore[arg-type]
        user,
    )
    order = (await session.execute(select(Order))).scalar_one()
    debit = (await session.execute(select(WalletLedgerEntry))).scalar_one()
    saved = await state.get_data()
    assert order.price_toman == FIXED
    assert order.wallet_applied_toman == 10_000
    assert order.status == OrderStatus.PENDING_PAYMENT
    assert saved["pending_amount"] == 40_000
    assert debit.reason == REASON_CHECKOUT_DEBIT
    assert debit.amount_toman == 10_000
    assert await get_balance(session, user.id) == 0
    assert texts.purchase_error not in message.texts


@pytest.mark.asyncio
async def test_a_wallet_race_after_the_live_read_rolls_back(
    session: AsyncSession, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Balance disappears between the read and the debit. Nothing is charged."""

    async def stale_full(*_args: object, **_kwargs: object) -> int:
        return FIXED

    monkeypatch.setattr(purchase, "get_balance", stale_full)
    user = await _customer(session, balance=10_000, telegram_id=1009)
    user_id = user.id
    message = _Message()
    await purchase.confirm_order(
        _callback(message),  # type: ignore[arg-type]
        await _state(_purchase_data(wallet_applied=FIXED)),
        session,
        _settings(_pricing_file(tmp_path)),  # type: ignore[arg-type]
        user,
    )
    orders = (await session.execute(select(Order))).scalars().all()
    ledger = (await session.execute(select(WalletLedgerEntry))).scalars().all()
    assert orders == []
    assert ledger == []
    assert await get_balance(session, user_id) == 10_000
    assert message.texts == [texts.purchase_wallet_short]

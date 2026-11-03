"""Logic audit, area MONEY (2026-09-30).

Strict xfail = a proved bug that is still open. The day one is fixed its
marker turns the gate red, which is the prompt to delete the marker. The plain
tests pin hypotheses the audit checked and found sound.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from database.base import Base
from database.models import Order, OrderStatus, Plan, User
from database.models.wallet import WalletLedgerEntry
from services.automation import expire_stale_orders
from services.finance import apply_checkout_debit
from services.payments import PaymentError, approve_payment, reject_payment, submit_card_receipt
from services.referral import record_referral_reward_for_order
from services.reporting import get_finance_report, today_sales

WALLET_PART = 40_000
PRICE = 100_000


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _user(session: AsyncSession, telegram_id: int, *, balance: int = 0) -> User:
    user = User(telegram_id=telegram_id, referral_code=f"ref{telegram_id}")
    user.wallet_balance_toman = balance
    session.add(user)
    await session.flush()
    return user


async def _plan(session: AsyncSession) -> Plan:
    plan = Plan(name="audit", data_limit_gb=50, duration_days=30, price_toman=PRICE)
    session.add(plan)
    await session.flush()
    return plan


async def _combo_order(
    session: AsyncSession, *, status: OrderStatus = OrderStatus.PENDING_PAYMENT
) -> tuple[User, User, Order]:
    """A customer who paid 40k from the wallet and owes 60k by card."""
    customer = await _user(session, 1001, balance=WALLET_PART)
    admin = await _user(session, 9001)
    plan = await _plan(session)
    order = Order(
        user_id=customer.id,
        plan_id=plan.id,
        status=OrderStatus.PENDING_PAYMENT,
        price_toman=PRICE,
        discount_toman=0,
        wallet_applied_toman=WALLET_PART,
    )
    session.add(order)
    await session.flush()
    await apply_checkout_debit(session, user_id=customer.id, order=order)
    assert customer.wallet_balance_toman == 0
    if status is OrderStatus.PAYMENT_REVIEW:
        await submit_card_receipt(
            session,
            order_id=order.id,
            user_id=customer.id,
            receipt_file_id="f",
            card_number_display="6037",
        )
    await session.flush()
    return customer, admin, order


async def _balance(session: AsyncSession, user: User) -> int:
    await session.refresh(user)
    return int(user.wallet_balance_toman)


async def _refund_rows(session: AsyncSession, order: Order) -> int:
    rows = await session.scalars(
        select(WalletLedgerEntry).where(WalletLedgerEntry.idempotency_key.like("checkout_refund:%"))
    )
    return len([r for r in rows.all() if r.order_id == order.id])


# --- hypotheses checked and found sound -----------------------------------


@pytest.mark.asyncio
async def test_expiry_refunds_the_wallet_portion_exactly_once(session: AsyncSession) -> None:
    customer, _admin, order = await _combo_order(session)
    later = datetime.now(UTC) + timedelta(hours=2)
    first = await expire_stale_orders(session, expiry_minutes=30, now=later)
    second = await expire_stale_orders(session, expiry_minutes=30, now=later)
    assert [o.id for o in first] == [order.id]
    assert second == []
    assert await _balance(session, customer) == WALLET_PART
    assert await _refund_rows(session, order) == 1


@pytest.mark.asyncio
async def test_reject_refunds_the_wallet_portion_exactly_once(session: AsyncSession) -> None:
    customer, admin, order = await _combo_order(session, status=OrderStatus.PAYMENT_REVIEW)
    await reject_payment(session, order_id=order.id, admin_user_id=admin.id, note="x")
    await reject_payment(session, order_id=order.id, admin_user_id=admin.id, note="x")
    assert await _balance(session, customer) == WALLET_PART
    assert await _refund_rows(session, order) == 1


@pytest.mark.asyncio
async def test_approve_then_reject_refunds_nothing(session: AsyncSession) -> None:
    customer, admin, order = await _combo_order(session, status=OrderStatus.PAYMENT_REVIEW)
    await approve_payment(session, order_id=order.id, admin_user_id=admin.id)
    with pytest.raises(PaymentError):
        await reject_payment(session, order_id=order.id, admin_user_id=admin.id, note="late")
    assert await _balance(session, customer) == 0
    assert await _refund_rows(session, order) == 0


@pytest.mark.asyncio
async def test_reject_then_approve_does_not_approve(session: AsyncSession) -> None:
    customer, admin, order = await _combo_order(session, status=OrderStatus.PAYMENT_REVIEW)
    await reject_payment(session, order_id=order.id, admin_user_id=admin.id, note="x")
    with pytest.raises(PaymentError):
        await approve_payment(session, order_id=order.id, admin_user_id=admin.id)
    await session.refresh(order)
    assert order.status is OrderStatus.REJECTED
    assert await _balance(session, customer) == WALLET_PART


# --- AUDIT-MONEY-1: «امروز» is the UTC day, the owner lives in Tehran --------


@pytest.mark.asyncio
async def test_today_window_is_the_tehran_day(session: AsyncSession) -> None:
    """A sale at 13:30 Tehran on Sep 30 is not «فروش امروز» at 00:30 Tehran on Oct 1."""
    customer = await _user(session, 1)
    plan = await _plan(session)
    order = Order(
        user_id=customer.id,
        plan_id=plan.id,
        status=OrderStatus.ACTIVE,
        price_toman=PRICE,
        discount_toman=0,
    )
    order.activated_at = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)  # 13:30 Tehran, Sep 30
    session.add(order)
    await session.commit()

    now = datetime(2026, 9, 30, 21, 0, tzinfo=UTC)  # 00:30 Tehran, Oct 1
    assert (await today_sales(session, now=now)).orders == 0


# --- AUDIT-MONEY-2: free trials counted as paying customers ------------------


@pytest.mark.asyncio
async def test_a_free_trial_is_not_a_paying_customer(session: AsyncSession) -> None:
    customer = await _user(session, 1)
    plan = await _plan(session)
    trial = Order(
        user_id=customer.id,
        plan_id=plan.id,
        status=OrderStatus.ACTIVE,
        price_toman=0,
        discount_toman=0,
        trial_kind="personal",
    )
    trial.activated_at = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
    session.add(trial)
    await session.commit()

    report = await get_finance_report(session, now=datetime(2026, 9, 30, 12, 0, tzinfo=UTC))
    assert report.paying_customers == 0


# --- AUDIT-MONEY-3: a giveaway earns the referrer a reward -------------------


@pytest.mark.asyncio
async def test_a_free_trial_earns_the_referrer_nothing(session: AsyncSession) -> None:
    referrer = await _user(session, 1)
    buyer = await _user(session, 2)
    buyer.referred_by_id = referrer.id
    plan = await _plan(session)
    trial = Order(
        user_id=buyer.id,
        plan_id=plan.id,
        status=OrderStatus.ACTIVE,
        price_toman=0,
        discount_toman=0,
        trial_kind="personal",
    )
    session.add(trial)
    await session.flush()

    log, created = await record_referral_reward_for_order(
        session, order_id=trial.id, reward_value=10
    )
    assert log is None
    assert created is False


# --- AUDIT-MONEY-4: promo code is not validated on a no-extra-discount plan --


@pytest.mark.asyncio
async def test_an_unknown_promo_is_refused_on_every_ready_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import checkout

    async def cfg(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"currency": "IRT", "promo_codes": {}, "sales_rules": {}}

    monkeypatch.setattr(checkout, "load_pricing_config_async", cfg)
    with pytest.raises(ValueError, match="invalid_promo_code"):
        await checkout.quote_checkout(
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(max_discount_percent=None, referral_referred_discount_percent=0),
            {
                "plan_type": "volume",
                "volume_gb": 50,
                "duration_days": 30,
                "fixed_price_toman": 150_000,
                "forbid_extra_discount": True,
            },
            promo_code="NOSUCHCODE",
            db_user=None,
        )

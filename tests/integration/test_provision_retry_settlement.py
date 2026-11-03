"""Durable retry-settlement admission for refunded provisioning failures."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api_client import UserResponse, UserStatus
from api_client.exceptions import PasarGuardNotFoundError
from database.models import Order, OrderStatus, Plan, User, WalletEntryType, WalletLedgerEntry
from database.models.plan import DataLimitResetStrategy
from services.admin import AdminError, retry_provision
from services.provisioning import ProvisionError, provision_order
from services.wallet import (
    REASON_RETRY_DEBIT,
    credit_provision_failed,
    debit,
    retry_debit_key,
)

PRICE_TOMAN = 150_000


async def _seed_refunded_failure(
    factory: async_sessionmaker[AsyncSession],
    *,
    status: OrderStatus = OrderStatus.PROVISION_FAILED,
) -> tuple[int, int, int]:
    """Persist a failed, refunded order and return its order/customer/other-user ids."""
    async with factory() as session:
        customer = User(
            telegram_id=1001,
            username="customer",
            first_name="Customer",
            last_name=None,
            language_code="fa",
            referral_code="CUSTOMER1",
            is_admin=False,
        )
        other_user = User(
            telegram_id=1002,
            username="other",
            first_name="Other",
            last_name=None,
            language_code="fa",
            referral_code="OTHER0001",
            is_admin=False,
        )
        plan = Plan(
            name="volume-10GB-30d",
            description="test",
            data_limit_gb=10,
            duration_days=30,
            price_toman=PRICE_TOMAN,
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            group_ids=[1],
            hwid_limit=1,
            is_active=True,
        )
        session.add_all([customer, other_user, plan])
        await session.flush()
        order = Order(
            user_id=customer.id,
            plan_id=plan.id,
            status=status,
            price_toman=PRICE_TOMAN,
            discount_toman=0,
        )
        session.add(order)
        await session.flush()
        await credit_provision_failed(
            session,
            user_id=customer.id,
            order_id=order.id,
            amount_toman=PRICE_TOMAN,
        )
        await session.commit()
        return int(order.id), int(customer.id), int(other_user.id)


def _panel_client(*, username: str = "durable_retry") -> AsyncMock:
    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    client.create_user = AsyncMock(
        return_value=UserResponse(
            id=42,
            username=username,
            status=UserStatus.ACTIVE,
            used_traffic=0,
            created_at=datetime.now(UTC),
            subscription_url=f"https://panel.example.com/sub/{username}",
            data_limit=10 * 1024**3,
        )
    )
    client.subscription_url_from_response = lambda user: user.subscription_url
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        OrderStatus.PROVISION_FAILED,
        OrderStatus.PAYMENT_APPROVED,
        OrderStatus.PROVISIONING,
    ],
)
async def test_reopened_stale_provision_rejects_refunded_order_before_panel(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
    status: OrderStatus,
) -> None:
    """Removing retry settlement must allow an old approval replay into the panel."""
    factory, _url = two_sessions
    order_id, _customer_id, _other_user_id = await _seed_refunded_failure(factory, status=status)
    client = _panel_client()

    async with factory() as session:
        with pytest.raises(ProvisionError, match="retry_payment_required"):
            await provision_order(session, client, order_id=order_id, default_group_ids=[1])
        order = await session.get(Order, order_id)
        assert order is not None
        assert order.status == status

    client.get_user.assert_not_awaited()
    client.create_user.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("idempotency_key", "retry_debit:999999"),
        ("entry_type", WalletEntryType.credit),
        ("reason", "checkout_debit"),
        ("user_id", "other_user"),
        ("order_id", None),
        ("amount_toman", PRICE_TOMAN - 1),
    ],
)
async def test_reopened_provision_rejects_malformed_retry_settlement_before_panel(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
    field: str,
    value: object,
) -> None:
    """Each malformed retry entry must fail instead of authorizing the panel call."""
    factory, _url = two_sessions
    order_id, customer_id, other_user_id = await _seed_refunded_failure(factory)

    async with factory() as session:
        entry_values: dict[str, Any] = {
            "user_id": customer_id,
            "entry_type": WalletEntryType.debit,
            "amount_toman": PRICE_TOMAN,
            "reason": REASON_RETRY_DEBIT,
            "order_id": order_id,
            "idempotency_key": retry_debit_key(order_id),
            "balance_after_toman": 0,
        }
        entry_values[field] = other_user_id if value == "other_user" else value
        session.add(WalletLedgerEntry(**entry_values))
        await session.commit()

    client = _panel_client()
    async with factory() as session:
        with pytest.raises(ProvisionError, match="retry_payment_required"):
            await provision_order(session, client, order_id=order_id, default_group_ids=[1])
        order = await session.get(Order, order_id)
        assert order is not None
        assert order.status == OrderStatus.PROVISION_FAILED

    client.get_user.assert_not_awaited()
    client.create_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_retry_debits_once_then_provisions_and_replays_as_active_noop(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    """Breaking the retry debit makes a refunded retry provision without repayment."""
    factory, _url = two_sessions
    order_id, customer_id, _other_user_id = await _seed_refunded_failure(factory)
    first_client = _panel_client()

    async with factory() as session:
        account, _reward = await retry_provision(session, first_client, order_id=order_id)
        await session.commit()
        assert account.panel_username == "durable_retry"

    async with factory() as session:
        entries = list(
            (
                await session.execute(
                    select(WalletLedgerEntry).where(
                        WalletLedgerEntry.idempotency_key == retry_debit_key(order_id)
                    )
                )
            ).scalars()
        )
        customer = await session.get(User, customer_id)
        assert customer is not None
        assert len(entries) == 1
        assert entries[0].entry_type is WalletEntryType.debit
        assert entries[0].amount_toman == PRICE_TOMAN
        assert customer.wallet_balance_toman == 0

    replay_client = _panel_client()
    async with factory() as session:
        account, _reward = await retry_provision(session, replay_client, order_id=order_id)
        assert account.panel_username == "durable_retry"

    replay_client.get_user.assert_not_awaited()
    replay_client.create_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_retry_with_spent_refund_fails_before_panel(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    """Removing the balance check would let an unfunded retry reach the panel."""
    factory, _url = two_sessions
    order_id, customer_id, _other_user_id = await _seed_refunded_failure(factory)
    async with factory() as session:
        await debit(
            session,
            user_id=customer_id,
            amount_toman=PRICE_TOMAN,
            reason="spent_refund",
            idempotency_key="spent_refund:1",
        )
        await session.commit()

    client = _panel_client()
    async with factory() as session:
        with pytest.raises(AdminError, match="insufficient_wallet_for_retry"):
            await retry_provision(session, client, order_id=order_id)
        order = await session.get(Order, order_id)
        assert order is not None
        assert order.status == OrderStatus.PROVISION_FAILED

    client.get_user.assert_not_awaited()
    client.create_user.assert_not_awaited()

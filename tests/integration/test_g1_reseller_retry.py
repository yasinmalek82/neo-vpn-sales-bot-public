"""G1: a failed reseller-trial retry must not keep its retry debit."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api_client import UserResponse
from api_client.exceptions import PasarGuardAPIError
from bot.handlers.referral import _commit_reseller_trial_failure
from database.models import Order, OrderStatus, User
from database.models.wallet import WalletLedgerEntry
from services.finance import settle_reseller_trial_provision_failure
from services.trial import TrialError, resume_reseller_trial
from services.wallet import retry_debit_key
from tests.integration.test_reseller_trial_recovery import (
    PRICE,
    _client,
    _issue,
    _seed_shop,
    _user,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "retry_error",
    [
        PasarGuardAPIError("panel down", status_code=500),
        PasarGuardAPIError("bad group", status_code=422),
        TimeoutError("reply lost"),
    ],
)
async def test_failed_reseller_retry_keeps_the_refund_and_no_retry_debit(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
    retry_error: Exception,
) -> None:
    factory, _url = two_sessions
    shop_id = await _seed_shop(factory, balance=10_000)
    panel_users: dict[str, UserResponse] = {}

    async def reject(_payload: Any) -> UserResponse:
        raise PasarGuardAPIError("bad group", status_code=422)

    client = _client(panel_users, reject)
    async with factory() as first:
        with pytest.raises(TrialError) as raised:
            await _issue(first, client, shop_id)
        order_id = raised.value.order_id
        assert order_id is not None
        assert (
            await settle_reseller_trial_provision_failure(
                first,
                order_id=order_id,
                original_exception=raised.value.original_exception,
                max_n=1,
            )
            == PRICE
        )
        await first.commit()

    async def fail(_payload: Any) -> UserResponse:
        raise retry_error

    client.create_user = AsyncMock(side_effect=fail)
    async with factory() as retry:
        with pytest.raises(TrialError) as again:
            await resume_reseller_trial(retry, client, user_id=shop_id, order_id=order_id)
        # What the handler does next: settle, then commit whatever is left.
        settings = type("S", (), {"wallet_provision_fail_credit_max_n": 1})()
        await _commit_reseller_trial_failure(retry, settings, again.value)  # type: ignore[arg-type]

    async with factory() as check:
        shop = await check.get(User, shop_id)
        order = await check.get(Order, order_id)
        debits = list(
            (
                await check.execute(
                    select(WalletLedgerEntry).where(
                        WalletLedgerEntry.idempotency_key == retry_debit_key(order_id)
                    )
                )
            ).scalars()
        )
        assert debits == []
        assert shop is not None and shop.wallet_balance_toman == 10_000
        assert order is not None and order.status is OrderStatus.PROVISION_FAILED

    # A healthy panel next time: one charge, one delivery.
    async def accept(payload: Any) -> UserResponse:
        username = str(payload.username)
        panel_users[username] = _user(username)
        return panel_users[username]

    client.create_user = AsyncMock(side_effect=accept)
    async with factory() as healthy:
        result = await resume_reseller_trial(healthy, client, user_id=shop_id, order_id=order_id)
        await healthy.commit()
        assert result.order.status is OrderStatus.ACTIVE
        shop = await healthy.get(User, shop_id)
        assert shop is not None and shop.wallet_balance_toman == 10_000 - PRICE

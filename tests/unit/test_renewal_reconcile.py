"""A durable renewal target is fulfilled only when all bought values are present."""

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api_client import UserModify, UserResponse, UserStatus
from database.models import Order, OrderStatus, PasarGuardAccount
from services.provisioning import ProvisionError, provision_order
from tests.unit.test_foundation_hardening import _plan, _user
from tests.unit.test_foundation_hardening import (
    session as session,  # noqa: PLC0414 - re-export pytest fixture
)

GB = 1024**3
TARGET_EXPIRY = 1_900_000_000


class Panel:
    def __init__(self, *, expiry: int | None, quota: int, hwid: int | None) -> None:
        self.user = UserResponse(
            id=42,
            username="renew_reconcile",
            status=UserStatus.ACTIVE,
            created_at=datetime.now(UTC),
            used_traffic=3 * GB,
            data_limit=quota,
            expire=expiry,
            hwid_limit=hwid,
            subscription_url="https://example.invalid/sub/test",
        )
        self.writes = 0

    async def get_user(self, username: str) -> UserResponse:
        assert username == self.user.username
        return self.user.model_copy()

    async def modify_user(self, username: str, payload: UserModify) -> UserResponse:
        assert username == self.user.username
        self.writes += 1
        self.user.expire = payload.expire
        self.user.data_limit = payload.data_limit
        self.user.hwid_limit = payload.hwid_limit
        return self.user.model_copy()

    def subscription_url_from_response(self, user: UserResponse) -> str:
        return user.subscription_url or ""


async def pending_renewal(session: AsyncSession, *, expiry: int = TARGET_EXPIRY) -> Order:
    user = await _user(session)
    plan = await _plan(session)
    plan.duration_days = 0
    account = PasarGuardAccount(
        user_id=user.id,
        panel_user_id=42,
        panel_username="renew_reconcile",
        plan_fingerprint="volume:10:0",
    )
    session.add(account)
    await session.flush()
    order = Order(
        user_id=user.id,
        plan_id=plan.id,
        status=OrderStatus.PROVISIONING,
        price_toman=50_000,
        discount_toman=0,
        panel_username=account.panel_username,
        renew_account_id=account.id,
        renew_target_expire_unix=expiry,
        renew_target_data_limit=20 * GB,
        delivery_data_limit_gb=10,
        delivery_duration_days=None,
        delivery_hwid_limit=1,
    )
    session.add(order)
    await session.commit()
    return order


@pytest.mark.parametrize("quota,hwid", [(10 * GB, 1), (20 * GB, None), (20 * GB, 2)])
async def test_matching_expiry_does_not_hide_unapplied_quota_or_devices(
    session: AsyncSession, quota: int, hwid: int | None
) -> None:
    order = await pending_renewal(session)
    panel = Panel(expiry=TARGET_EXPIRY, quota=quota, hwid=hwid)

    await provision_order(session, panel, order_id=order.id)  # type: ignore[arg-type]

    assert order.status == OrderStatus.ACTIVE
    assert panel.user.data_limit == 20 * GB
    assert panel.user.hwid_limit == 1
    assert panel.user.expire == TARGET_EXPIRY
    assert panel.user.used_traffic == 3 * GB
    assert panel.writes == 1


@pytest.mark.parametrize("expiry", [TARGET_EXPIRY, None])
async def test_complete_target_after_lost_reply_is_not_written_twice(
    session: AsyncSession, expiry: int | None
) -> None:
    order = await pending_renewal(session, expiry=expiry or 0)
    panel = Panel(expiry=expiry, quota=20 * GB, hwid=1)

    await provision_order(session, panel, order_id=order.id)  # type: ignore[arg-type]

    assert order.status == OrderStatus.ACTIVE
    assert panel.writes == 0
    assert panel.user.used_traffic == 3 * GB


@pytest.mark.parametrize(
    "expiry,quota", [(TARGET_EXPIRY + 86400, 10 * GB), (TARGET_EXPIRY, 25 * GB)]
)
async def test_conflicting_later_panel_change_is_not_overwritten_or_declared_fulfilled(
    session: AsyncSession, expiry: int, quota: int
) -> None:
    order = await pending_renewal(session)
    panel = Panel(expiry=expiry, quota=quota, hwid=1)

    with pytest.raises(ProvisionError, match="renew_state_conflict"):
        await provision_order(session, panel, order_id=order.id)  # type: ignore[arg-type]

    assert order.status == OrderStatus.PROVISIONING
    assert panel.writes == 0
    assert panel.user.data_limit == quota
    assert panel.user.expire == expiry

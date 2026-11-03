"""Unit tests for order provisioning against mocked PasarGuard client."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from api_client import UserResponse, UserStatus
from api_client.exceptions import PasarGuardAPIError, PasarGuardNotFoundError
from database.base import Base
from database.models import Order, OrderStatus, Plan, User
from database.models.plan import DataLimitResetStrategy
from services.payments import approve_payment, submit_card_receipt
from services.provisioning import provision_order, resolve_panel_group_ids


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _panel_user(username: str, *, user_id: int = 42) -> UserResponse:
    return UserResponse(
        id=user_id,
        username=username,
        status=UserStatus.ACTIVE,
        used_traffic=0,
        created_at=datetime.now(UTC),
        subscription_url=f"https://panel.example.com/sub/{username}",
        data_limit=10 * 1024**3,
    )


async def _approved_order(session: AsyncSession) -> tuple[User, Order]:
    customer = User(
        telegram_id=1001,
        username="cust",
        first_name="C",
        last_name=None,
        language_code="fa",
        referral_code="CUST0001",
        is_admin=False,
    )
    admin = User(
        telegram_id=9001,
        username="admin",
        first_name="A",
        last_name=None,
        language_code="fa",
        referral_code="ADMIN001",
        is_admin=True,
    )
    plan = Plan(
        name="volume-10GB-30d",
        description="test",
        data_limit_gb=10,
        duration_days=30,
        price_toman=150_000,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1, 2],
        hwid_limit=2,
        is_active=True,
    )
    session.add_all([customer, admin, plan])
    await session.flush()
    order = Order(
        user_id=customer.id,
        plan_id=plan.id,
        status=OrderStatus.PENDING_PAYMENT,
        price_toman=150_000,
        discount_toman=0,
    )
    session.add(order)
    await session.flush()
    await submit_card_receipt(
        session,
        order_id=order.id,
        user_id=customer.id,
        receipt_file_id="file",
        card_number_display="6037",
    )
    await approve_payment(session, order_id=order.id, admin_user_id=admin.id)
    await session.refresh(order)
    return customer, order


@pytest.mark.asyncio
async def test_approve_and_provision_success(session: AsyncSession) -> None:
    _customer, order = await _approved_order(session)
    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    # Echo the requested name back, the way the panel does. The name is random
    # now, so a test that hardcoded `neo_{order.id}` was asserting the old
    # guessable scheme rather than the behaviour.
    client.create_user = AsyncMock(side_effect=lambda payload: _panel_user(payload.username))
    client.subscription_url_from_response = lambda u: u.subscription_url

    account, _reward = await provision_order(
        session, client, order_id=order.id, default_group_ids=[9]
    )
    await session.refresh(order)
    assert order.status == OrderStatus.ACTIVE
    assert order.activated_at is not None
    username = order.panel_username
    assert username and username != f"neo_{order.id}"
    assert account.panel_username == username
    assert account.subscription_url.endswith(username)
    client.create_user.assert_awaited_once()
    payload = client.create_user.await_args.args[0]
    assert payload.username == username
    assert payload.data_limit == 10 * 1024**3
    assert payload.group_ids == [1, 2]
    assert payload.hwid_limit == 2


@pytest.mark.asyncio
async def test_a_retry_reuses_the_panel_username_it_already_chose(
    session: AsyncSession,
) -> None:
    """A lost reply must not buy a second panel account.

    `create_user` reaches the panel, the response times out, the order lands in
    PROVISION_FAILED. Both username generators end in a random suffix, so the
    retry used to ask for a *different* name, get a 404 from `get_user`, and
    create a second panel user — orphaned, paid for and invisible. The name is
    stamped on the order before the panel is called, so the retry finds it.
    """
    _customer, order = await _approved_order(session)
    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    client.create_user = AsyncMock(side_effect=TimeoutError("panel ate the reply"))
    client.subscription_url_from_response = lambda u: u.subscription_url

    with pytest.raises(TimeoutError):
        await provision_order(session, client, order_id=order.id, default_group_ids=[9])
    await session.refresh(order)
    first_attempt = order.panel_username
    assert first_attempt, "the chosen name must survive the failure"

    client.create_user = AsyncMock(side_effect=lambda payload: _panel_user(payload.username))
    account, _reward = await provision_order(
        session, client, order_id=order.id, default_group_ids=[9]
    )
    assert client.create_user.await_args.args[0].username == first_attempt
    assert account.panel_username == first_attempt


@pytest.mark.asyncio
async def test_provision_reuses_existing_panel_user(session: AsyncSession) -> None:
    _customer, order = await _approved_order(session)
    client = AsyncMock()
    existing = _panel_user(f"neo_{order.id}", user_id=99)
    client.get_user = AsyncMock(return_value=existing)
    client.create_user = AsyncMock()
    client.subscription_url_from_response = lambda u: u.subscription_url

    account, _reward = await provision_order(
        session, client, order_id=order.id, default_group_ids=[1]
    )
    client.create_user.assert_not_awaited()
    assert account.panel_user_id == 99
    await session.refresh(order)
    assert order.status == OrderStatus.ACTIVE


@pytest.mark.asyncio
async def test_provision_failure_sets_status(session: AsyncSession) -> None:
    _customer, order = await _approved_order(session)
    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    client.create_user = AsyncMock(side_effect=PasarGuardAPIError("boom", status_code=500))
    with pytest.raises(PasarGuardAPIError):
        await provision_order(session, client, order_id=order.id, default_group_ids=[1])
    await session.refresh(order)
    assert order.status == OrderStatus.PROVISION_FAILED


@pytest.mark.asyncio
async def test_provision_active_returns_existing(session: AsyncSession) -> None:
    customer, order = await _approved_order(session)
    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    client.create_user = AsyncMock(return_value=_panel_user(f"neo_{order.id}"))
    client.subscription_url_from_response = lambda u: u.subscription_url
    first, _r1 = await provision_order(session, client, order_id=order.id, default_group_ids=[1])
    second, _r2 = await provision_order(session, client, order_id=order.id, default_group_ids=[1])
    assert first.id == second.id
    assert client.create_user.await_count == 1


def test_resolve_panel_group_ids_plan_wins() -> None:
    assert resolve_panel_group_ids([3], [1, 2]) == [3]
    assert resolve_panel_group_ids([3, 4], [5]) == [3, 4]


def test_resolve_panel_group_ids_defaults_when_plan_empty() -> None:
    assert resolve_panel_group_ids([], [5]) == [5]
    assert resolve_panel_group_ids(None, [5]) == [5]
    assert resolve_panel_group_ids([], [1, 2]) == [1, 2]


def test_resolve_panel_group_ids_never_unions() -> None:
    assert resolve_panel_group_ids([3], [1, 2]) == [3]
    assert resolve_panel_group_ids([1], [1, 2]) == [1]
    assert resolve_panel_group_ids(None, None) == []
    assert resolve_panel_group_ids([], []) == []


async def _approved_order_with_groups(
    session: AsyncSession, group_ids: list[int]
) -> tuple[User, Order]:
    customer = User(
        telegram_id=1001,
        username="cust",
        first_name="C",
        last_name=None,
        language_code="fa",
        referral_code="CUST0001",
        is_admin=False,
    )
    admin = User(
        telegram_id=9001,
        username="admin",
        first_name="A",
        last_name=None,
        language_code="fa",
        referral_code="ADMIN001",
        is_admin=True,
    )
    plan = Plan(
        name=f"volume-10GB-30d-g{'-'.join(map(str, group_ids)) or 'empty'}",
        description="test",
        data_limit_gb=10,
        duration_days=30,
        price_toman=150_000,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=list(group_ids),
        hwid_limit=2,
        is_active=True,
    )
    session.add_all([customer, admin, plan])
    await session.flush()
    order = Order(
        user_id=customer.id,
        plan_id=plan.id,
        status=OrderStatus.PENDING_PAYMENT,
        price_toman=150_000,
        discount_toman=0,
    )
    session.add(order)
    await session.flush()
    await submit_card_receipt(
        session,
        order_id=order.id,
        user_id=customer.id,
        receipt_file_id="file",
        card_number_display="6037",
    )
    await approve_payment(session, order_id=order.id, admin_user_id=admin.id)
    await session.refresh(order)
    return customer, order


@pytest.mark.asyncio
async def test_provision_plan_group_ids_win_over_defaults(session: AsyncSession) -> None:
    _customer, order = await _approved_order_with_groups(session, [3])
    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    client.create_user = AsyncMock(return_value=_panel_user(f"neo_{order.id}"))
    client.subscription_url_from_response = lambda u: u.subscription_url

    await provision_order(session, client, order_id=order.id, default_group_ids=[1, 2])
    payload = client.create_user.await_args.args[0]
    assert payload.group_ids == [3]


@pytest.mark.asyncio
async def test_provision_empty_plan_uses_defaults_exactly(session: AsyncSession) -> None:
    _customer, order = await _approved_order_with_groups(session, [])
    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    client.create_user = AsyncMock(return_value=_panel_user(f"neo_{order.id}"))
    client.subscription_url_from_response = lambda u: u.subscription_url

    await provision_order(session, client, order_id=order.id, default_group_ids=[5])
    payload = client.create_user.await_args.args[0]
    assert payload.group_ids == [5]


# --- Phase B1a: renew extend + 1:N different plan ---


def test_plan_fingerprint_volume_and_unlimited() -> None:
    from services.orders import plan_fingerprint

    volume = Plan(
        name="v",
        description=None,
        data_limit_gb=50,
        duration_days=30,
        price_toman=1,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[],
        hwid_limit=2,
    )
    unlimited = Plan(
        name="u",
        description=None,
        data_limit_gb=0,
        duration_days=90,
        price_toman=1,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[],
        hwid_limit=3,
    )
    assert plan_fingerprint(volume) == "volume:50:30"
    assert plan_fingerprint(unlimited) == "unlimited:3:90"


def test_compute_renew_expire_early_and_after() -> None:
    from services.provisioning import compute_renew_expire_unix

    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    now_unix = int(now.timestamp())
    future = now_unix + 10 * 86400
    # Early renew: extend from current expire
    assert (
        compute_renew_expire_unix(current_expire=future, duration_days=30, now=now)
        == future + 30 * 86400
    )
    # After expiry: extend from now
    past = now_unix - 5 * 86400
    assert (
        compute_renew_expire_unix(current_expire=past, duration_days=30, now=now)
        == now_unix + 30 * 86400
    )
    # Missing expire: from now
    assert (
        compute_renew_expire_unix(current_expire=None, duration_days=7, now=now)
        == now_unix + 7 * 86400
    )


def test_compute_renew_data_limit_rules() -> None:
    from services.provisioning import compute_renew_data_limit

    gb = 1024**3
    # cap = current limit + new; usage stays counted (modify_user does not reset it),
    # so 3 GB used leaves 7 + 10 = 17 GB usable
    assert (
        compute_renew_data_limit(
            current_data_limit=10 * gb,
            used_traffic=3 * gb,
            new_plan_data_limit_gb=10,
        )
        == 20 * gb
    )
    # used > limit → no leftover: cap lifted from usage, exactly the new volume usable
    assert (
        compute_renew_data_limit(
            current_data_limit=10 * gb,
            used_traffic=12 * gb,
            new_plan_data_limit_gb=5,
        )
        == 17 * gb
    )
    # current unlimited
    assert (
        compute_renew_data_limit(
            current_data_limit=None,
            used_traffic=1,
            new_plan_data_limit_gb=10,
        )
        is None
    )
    assert (
        compute_renew_data_limit(
            current_data_limit=0,
            used_traffic=1,
            new_plan_data_limit_gb=10,
        )
        is None
    )
    # new plan unlimited
    assert (
        compute_renew_data_limit(
            current_data_limit=10 * gb,
            used_traffic=1,
            new_plan_data_limit_gb=0,
        )
        is None
    )


async def _second_approved_order(
    session: AsyncSession,
    customer: User,
    admin: User,
    *,
    plan: Plan | None = None,
    data_limit_gb: int = 10,
    duration_days: int = 30,
    hwid_limit: int = 2,
    name: str | None = None,
) -> Order:
    if plan is None:
        plan = Plan(
            name=name or f"volume-{data_limit_gb}GB-{duration_days}d-extra",
            description="test2",
            data_limit_gb=data_limit_gb,
            duration_days=duration_days,
            price_toman=150_000,
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            group_ids=[1, 2],
            hwid_limit=hwid_limit,
            is_active=True,
        )
        session.add(plan)
        await session.flush()
    order = Order(
        user_id=customer.id,
        plan_id=plan.id,
        status=OrderStatus.PENDING_PAYMENT,
        price_toman=150_000,
        discount_toman=0,
    )
    session.add(order)
    await session.flush()
    await submit_card_receipt(
        session,
        order_id=order.id,
        user_id=customer.id,
        receipt_file_id="file2",
        card_number_display="6037",
    )
    await approve_payment(session, order_id=order.id, admin_user_id=admin.id)
    await session.refresh(order)
    return order


@pytest.mark.asyncio
async def test_early_renew_extends_from_current_expire(session: AsyncSession) -> None:
    from sqlalchemy import select

    from database.models import PasarGuardAccount
    from services.provisioning import compute_renew_expire_unix

    customer, order1 = await _approved_order(session)
    admin = (await session.execute(select(User).where(User.telegram_id == 9001))).scalar_one()

    client = AsyncMock()
    first_expire = int((datetime.now(UTC) + timedelta(days=20)).timestamp())
    created = _panel_user(f"neo_{order1.id}", user_id=42)
    created.expire = first_expire
    created.used_traffic = 3 * 1024**3
    created.data_limit = 10 * 1024**3
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    client.create_user = AsyncMock(return_value=created)
    client.subscription_url_from_response = lambda u: u.subscription_url

    account1, _ = await provision_order(session, client, order_id=order1.id, default_group_ids=[1])
    assert account1.panel_username == f"neo_{order1.id}"

    # Same plan second purchase → renew
    # Reuse the same plan row so fingerprint matches order1
    plan1 = (await session.execute(select(Plan).where(Plan.id == order1.plan_id))).scalar_one()
    order2 = await _second_approved_order(session, customer, admin, plan=plan1)

    live = _panel_user(f"neo_{order1.id}", user_id=42)
    live.expire = first_expire
    live.used_traffic = 3 * 1024**3
    live.data_limit = 10 * 1024**3
    renewed = _panel_user(f"neo_{order1.id}", user_id=42)
    expected_expire = compute_renew_expire_unix(current_expire=first_expire, duration_days=30)
    renewed.expire = expected_expire
    renewed.used_traffic = 3 * 1024**3
    renewed.data_limit = 20 * 1024**3
    renewed.subscription_url = f"https://panel.example.com/sub/neo_{order1.id}"

    client.get_user = AsyncMock(return_value=live)
    client.create_user = AsyncMock()
    client.modify_user = AsyncMock(return_value=renewed)

    account2, _ = await provision_order(session, client, order_id=order2.id, default_group_ids=[1])
    await session.refresh(order2)

    client.create_user.assert_not_awaited()
    client.modify_user.assert_awaited_once()
    mod_args = client.modify_user.await_args
    assert mod_args.args[0] == f"neo_{order1.id}"
    payload = mod_args.args[1]
    assert payload.expire == expected_expire
    # cap = current 10 GB + plan 10 GB; the 3 GB already used stays counted,
    # so the customer can still use 7 + 10 = 17 GB (AUDIT-PROV-1).
    assert payload.data_limit == 20 * 1024**3
    assert payload.hwid_limit == 2

    assert order2.status == OrderStatus.ACTIVE
    assert order2.panel_username == f"neo_{order1.id}"
    assert account2.id == account1.id
    assert account2.panel_username == f"neo_{order1.id}"

    # Still a single account row for same-plan renew
    rows = (
        (
            await session.execute(
                select(PasarGuardAccount).where(PasarGuardAccount.user_id == customer.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_renew_after_expiry_extends_from_now(session: AsyncSession) -> None:
    from sqlalchemy import select

    from services.provisioning import compute_renew_expire_unix

    customer, order1 = await _approved_order(session)
    admin = (await session.execute(select(User).where(User.telegram_id == 9001))).scalar_one()

    past_expire = int((datetime.now(UTC) - timedelta(days=5)).timestamp())
    created = _panel_user(f"neo_{order1.id}", user_id=7)
    created.expire = past_expire
    created.used_traffic = 1 * 1024**3
    created.data_limit = 10 * 1024**3

    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    client.create_user = AsyncMock(return_value=created)
    client.subscription_url_from_response = lambda u: u.subscription_url
    await provision_order(session, client, order_id=order1.id, default_group_ids=[1])

    plan1 = (await session.execute(select(Plan).where(Plan.id == order1.plan_id))).scalar_one()
    order2 = await _second_approved_order(session, customer, admin, plan=plan1)

    live = _panel_user(f"neo_{order1.id}", user_id=7)
    live.expire = past_expire
    live.used_traffic = 1 * 1024**3
    live.data_limit = 10 * 1024**3

    before = datetime.now(UTC)
    expected = compute_renew_expire_unix(current_expire=past_expire, duration_days=30, now=before)
    renewed = _panel_user(f"neo_{order1.id}", user_id=7)
    renewed.expire = expected
    renewed.data_limit = 20 * 1024**3

    client.get_user = AsyncMock(return_value=live)
    client.modify_user = AsyncMock(return_value=renewed)
    client.create_user = AsyncMock()

    await provision_order(session, client, order_id=order2.id, default_group_ids=[1])
    payload = client.modify_user.await_args.args[1]
    # Allow 2s skew between test clock and helper call inside provision
    assert payload.expire >= expected
    assert payload.expire <= expected + 2
    assert payload.data_limit == 20 * 1024**3
    client.create_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_different_plan_creates_second_account(session: AsyncSession) -> None:
    from sqlalchemy import select

    from database.models import PasarGuardAccount

    customer, order1 = await _approved_order(session)
    admin = (await session.execute(select(User).where(User.telegram_id == 9001))).scalar_one()

    client = AsyncMock()
    first = _panel_user(f"neo_{order1.id}", user_id=10)
    first.subscription_url = f"https://panel.example.com/sub/neo_{order1.id}"
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    client.create_user = AsyncMock(return_value=first)
    client.subscription_url_from_response = lambda u: u.subscription_url
    account1, _ = await provision_order(session, client, order_id=order1.id, default_group_ids=[1])
    first_username = account1.panel_username
    first_sub = account1.subscription_url

    # Different plan (unlimited fingerprint)
    order2 = await _second_approved_order(
        session,
        customer,
        admin,
        data_limit_gb=0,
        duration_days=90,
        hwid_limit=5,
        name="unlimited-5u-3m",
    )

    second = _panel_user(f"neo_{order2.id}", user_id=99)
    second.subscription_url = f"https://panel.example.com/sub/neo_{order2.id}"
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    client.create_user = AsyncMock(return_value=second)
    client.modify_user = AsyncMock()

    account2, _ = await provision_order(session, client, order_id=order2.id, default_group_ids=[1])
    await session.refresh(order2)
    await session.refresh(account1)

    client.modify_user.assert_not_awaited()
    client.create_user.assert_awaited()
    assert account2.id != account1.id
    assert account2.panel_username == f"neo_{order2.id}"
    assert order2.panel_username == f"neo_{order2.id}"
    assert account1.panel_username == first_username
    assert account1.subscription_url == first_sub

    rows = (
        (
            await session.execute(
                select(PasarGuardAccount).where(PasarGuardAccount.user_id == customer.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_idempotent_renew_does_not_double_extend(session: AsyncSession) -> None:
    from sqlalchemy import select

    customer, order1 = await _approved_order(session)
    admin = (await session.execute(select(User).where(User.telegram_id == 9001))).scalar_one()

    client = AsyncMock()
    created = _panel_user(f"neo_{order1.id}", user_id=3)
    created.expire = int((datetime.now(UTC) + timedelta(days=15)).timestamp())
    created.used_traffic = 0
    created.data_limit = 10 * 1024**3
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    client.create_user = AsyncMock(return_value=created)
    client.subscription_url_from_response = lambda u: u.subscription_url
    await provision_order(session, client, order_id=order1.id, default_group_ids=[1])

    plan1 = (await session.execute(select(Plan).where(Plan.id == order1.plan_id))).scalar_one()
    order2 = await _second_approved_order(session, customer, admin, plan=plan1)

    live = _panel_user(f"neo_{order1.id}", user_id=3)
    live.expire = created.expire
    live.used_traffic = 0
    live.data_limit = 10 * 1024**3
    renewed = _panel_user(f"neo_{order1.id}", user_id=3)
    renewed.expire = int(created.expire) + 30 * 86400
    renewed.data_limit = 20 * 1024**3

    client.get_user = AsyncMock(return_value=live)
    client.modify_user = AsyncMock(return_value=renewed)
    client.create_user = AsyncMock()

    first_acct, _ = await provision_order(
        session, client, order_id=order2.id, default_group_ids=[1]
    )
    second_acct, _ = await provision_order(
        session, client, order_id=order2.id, default_group_ids=[1]
    )
    assert first_acct.id == second_acct.id
    assert client.modify_user.await_count == 1

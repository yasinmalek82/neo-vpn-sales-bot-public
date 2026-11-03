"""Unit tests for user account overview (mocked PasarGuard client)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from api_client import UserResponse, UserStatus
from api_client.exceptions import PasarGuardAPIError
from database.base import Base
from database.models import Order, OrderStatus, PasarGuardAccount, Plan, User
from database.models.order import TRIAL_KIND_PERSONAL
from database.models.plan import DataLimitResetStrategy
from services.accounts import (
    PERSONAL_TRIAL_LABEL,
    RECENT_ORDERS_LIMIT,
    account_label,
    format_traffic_gb,
    get_account_overview,
    list_named_services,
    normalize_expire,
)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _panel_user(  # noqa: PLR0913
    username: str,
    *,
    user_id: int = 42,
    used_traffic: int = 2 * 1024**3,
    data_limit: int | None = 10 * 1024**3,
    expire: datetime | int | None = None,
    subscription_url: str | None = None,
    status: UserStatus = UserStatus.ACTIVE,
) -> UserResponse:
    if expire is None:
        expire = datetime.now(UTC) + timedelta(days=20)
    return UserResponse(
        id=user_id,
        username=username,
        status=status,
        used_traffic=used_traffic,
        created_at=datetime.now(UTC),
        expire=expire,
        data_limit=data_limit,
        subscription_url=subscription_url or f"https://panel.example.com/sub/{username}",
    )


async def _seed_user(session: AsyncSession) -> User:
    customer = User(
        telegram_id=1001,
        username="cust",
        first_name="C",
        last_name=None,
        language_code="fa",
        referral_code="CUST0001",
        is_admin=False,
    )
    session.add(customer)
    await session.flush()
    return customer


async def _seed_plan(session: AsyncSession, *, data_limit_gb: int = 10) -> Plan:
    plan = Plan(
        name=f"volume-{data_limit_gb}GB-30d",
        description="test",
        data_limit_gb=data_limit_gb,
        duration_days=30,
        price_toman=150_000,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1],
        hwid_limit=1,
        is_active=True,
    )
    session.add(plan)
    await session.flush()
    return plan


async def _add_order(  # noqa: PLR0913
    session: AsyncSession,
    user: User,
    plan: Plan,
    *,
    status: OrderStatus,
    price_toman: int = 150_000,
    expires_at: datetime | None = None,
    created_at: datetime | None = None,
) -> Order:
    order = Order(
        user_id=user.id,
        plan_id=plan.id,
        status=status,
        price_toman=price_toman,
        discount_toman=0,
        expires_at=expires_at,
    )
    session.add(order)
    await session.flush()
    if created_at is not None:
        order.created_at = created_at
        await session.flush()
    return order


def _client(panel_user: UserResponse | None = None, *, error: Exception | None = None) -> AsyncMock:
    client = AsyncMock()
    if error is not None:
        client.get_user = AsyncMock(side_effect=error)
    elif panel_user is not None:
        client.get_user = AsyncMock(return_value=panel_user)
    else:
        client.get_user = AsyncMock(side_effect=AssertionError("get_user should not be called"))
    client.subscription_url_from_response = lambda u: u.subscription_url
    return client


@pytest.mark.asyncio
async def test_empty_overview_when_no_account_or_orders(session: AsyncSession) -> None:
    user = await _seed_user(session)
    overview = await get_account_overview(session, user_id=user.id, client=_client())
    assert overview.is_empty
    assert overview.has_account is False
    assert overview.has_orders is False
    assert overview.stale is False
    assert overview.recent_orders == []


@pytest.mark.asyncio
async def test_live_refresh_from_panel(session: AsyncSession) -> None:
    user = await _seed_user(session)
    plan = await _seed_plan(session)
    expire_db = datetime.now(UTC) + timedelta(days=5)
    await _add_order(
        session,
        user,
        plan,
        status=OrderStatus.ACTIVE,
        expires_at=expire_db,
    )
    account = PasarGuardAccount(
        user_id=user.id,
        panel_user_id=42,
        panel_username="neo_1",
        subscription_url="https://old.example/sub/neo_1",
    )
    session.add(account)
    await session.flush()

    live_expire = datetime.now(UTC) + timedelta(days=25)
    panel = _panel_user(
        "neo_1",
        used_traffic=3 * 1024**3,
        data_limit=20 * 1024**3,
        expire=live_expire,
        subscription_url="https://panel.example.com/sub/neo_1",
    )
    overview = await get_account_overview(session, user_id=user.id, client=_client(panel))

    assert overview.has_account is True
    assert overview.stale is False
    assert overview.is_live is True
    assert overview.status == UserStatus.ACTIVE.value
    assert overview.used_traffic == 3 * 1024**3
    assert overview.data_limit == 20 * 1024**3
    # Refresh must NOT overwrite a stored subscription URL (only revoke_sub rotates).
    assert overview.subscription_url == "https://old.example/sub/neo_1"
    assert overview.expire is not None
    assert abs((overview.expire - live_expire).total_seconds()) < 2  # noqa: PLR2004
    await session.refresh(account)
    assert account.subscription_url == "https://old.example/sub/neo_1"


@pytest.mark.asyncio
async def test_panel_error_falls_back_and_flags_stale(session: AsyncSession) -> None:
    user = await _seed_user(session)
    plan = await _seed_plan(session, data_limit_gb=10)
    expire_db = datetime(2026, 10, 1, tzinfo=UTC)
    await _add_order(
        session,
        user,
        plan,
        status=OrderStatus.ACTIVE,
        expires_at=expire_db,
        price_toman=150_000,
    )
    session.add(
        PasarGuardAccount(
            user_id=user.id,
            panel_user_id=7,
            panel_username="neo_9",
            subscription_url="https://db.example/sub/neo_9",
        )
    )
    await session.flush()

    client = _client(error=PasarGuardAPIError("down", status_code=500))
    overview = await get_account_overview(session, user_id=user.id, client=client)

    assert overview.has_account is True
    assert overview.stale is True
    assert overview.is_live is False
    assert overview.subscription_url == "https://db.example/sub/neo_9"
    assert overview.panel_username == "neo_9"
    assert overview.status == OrderStatus.ACTIVE.value
    assert overview.expire == expire_db
    assert overview.used_traffic is None
    assert overview.data_limit == 10 * 1024**3


@pytest.mark.asyncio
async def test_prefers_latest_active_order_for_fallback(session: AsyncSession) -> None:
    user = await _seed_user(session)
    plan = await _seed_plan(session, data_limit_gb=15)
    now = datetime.now(UTC)
    active_expire = now + timedelta(days=12)
    await _add_order(
        session,
        user,
        plan,
        status=OrderStatus.ACTIVE,
        expires_at=active_expire,
        created_at=now - timedelta(days=2),
        price_toman=100_000,
    )
    await _add_order(
        session,
        user,
        plan,
        status=OrderStatus.PENDING_PAYMENT,
        expires_at=None,
        created_at=now,
        price_toman=200_000,
    )
    session.add(
        PasarGuardAccount(
            user_id=user.id,
            panel_user_id=1,
            panel_username="neo_x",
            subscription_url="https://db.example/sub/x",
        )
    )
    await session.flush()

    overview = await get_account_overview(
        session,
        user_id=user.id,
        client=_client(error=PasarGuardAPIError("down", status_code=503)),
    )
    assert overview.stale is True
    assert overview.status == OrderStatus.ACTIVE.value
    assert overview.expire == active_expire
    assert overview.data_limit == 15 * 1024**3
    assert [item.status for item in overview.recent_orders] == [
        OrderStatus.PENDING_PAYMENT.value,
        OrderStatus.ACTIVE.value,
    ]


@pytest.mark.asyncio
async def test_recent_orders_capped_at_ten(session: AsyncSession) -> None:
    user = await _seed_user(session)
    plan = await _seed_plan(session)
    now = datetime.now(UTC)
    for i in range(12):
        await _add_order(
            session,
            user,
            plan,
            status=OrderStatus.EXPIRED,
            price_toman=10_000 + i,
            created_at=now - timedelta(minutes=12 - i),
        )
    overview = await get_account_overview(session, user_id=user.id, client=_client())
    assert overview.has_account is False
    assert overview.has_orders is True
    assert len(overview.recent_orders) == RECENT_ORDERS_LIMIT
    assert overview.recent_orders[0].price_toman == 10_000 + 11
    assert overview.recent_orders[-1].price_toman == 10_000 + 2


@pytest.mark.asyncio
async def test_unix_expire_normalized(session: AsyncSession) -> None:
    user = await _seed_user(session)
    session.add(
        PasarGuardAccount(
            user_id=user.id,
            panel_user_id=3,
            panel_username="neo_unix",
            subscription_url=None,
        )
    )
    await session.flush()
    ts = int((datetime.now(UTC) + timedelta(days=3)).timestamp())
    panel = _panel_user("neo_unix", expire=ts, data_limit=None, used_traffic=0)
    overview = await get_account_overview(session, user_id=user.id, client=_client(panel))
    assert overview.expire is not None
    assert abs(overview.expire.timestamp() - ts) < 1
    assert overview.data_limit is None


def test_format_traffic_gb() -> None:
    assert format_traffic_gb(10 * 1024**3) == "10"
    assert format_traffic_gb(int(1.5 * 1024**3)) == "1.50"


def test_normalize_expire() -> None:
    ts = 1_800_000_000
    parsed = normalize_expire(ts)
    assert parsed is not None
    assert int(parsed.timestamp()) == ts
    naive = datetime(2026, 9, 12, 12, 0, 0)
    aware = normalize_expire(naive)
    assert aware is not None
    assert aware.tzinfo is not None
    assert normalize_expire(None) is None


@pytest.mark.asyncio
async def test_list_named_services_excludes_unnamed(session: AsyncSession) -> None:
    user = await _seed_user(session)
    session.add(
        PasarGuardAccount(
            user_id=user.id,
            panel_user_id=1,
            panel_username="legacy_unnamed",
            subscription_url="https://db.example/sub/legacy",
            service_name=None,
            display_name=None,
        )
    )
    session.add(
        PasarGuardAccount(
            user_id=user.id,
            panel_user_id=2,
            panel_username="named_svc_ab12",
            subscription_url="https://db.example/sub/named",
            service_name="myvpn",
            display_name="myvpn",
        )
    )
    session.add(
        PasarGuardAccount(
            user_id=user.id,
            panel_user_id=3,
            panel_username="blank_names",
            subscription_url=None,
            service_name="   ",
            display_name="",
        )
    )
    await session.flush()

    named = await list_named_services(session, user.id)
    assert len(named) == 1
    assert named[0].panel_username == "named_svc_ab12"
    assert named[0].service_name == "myvpn"


@pytest.mark.asyncio
async def test_personal_trial_without_a_name_is_listed(session: AsyncSession) -> None:
    """The repeat-tap message sends the customer here. An unnamed trial must show."""
    user = await _seed_user(session)
    plan = await _seed_plan(session)
    order = await _add_order(session, user, plan, status=OrderStatus.ACTIVE, price_toman=0)
    order.trial_kind = TRIAL_KIND_PERSONAL
    order.panel_username = "neo_trialabcd"
    session.add(
        PasarGuardAccount(
            user_id=user.id,
            panel_user_id=9,
            panel_username="neo_trialabcd",
            subscription_url="https://db.example/sub/trial",
            service_name=None,
            display_name=None,
        )
    )
    await session.flush()

    listed = await list_named_services(session, user.id)
    assert [row.panel_username for row in listed] == ["neo_trialabcd"]
    assert listed[0].display_name == PERSONAL_TRIAL_LABEL
    assert account_label(listed[0]) == PERSONAL_TRIAL_LABEL


@pytest.mark.asyncio
async def test_get_account_overview_by_account_id(session: AsyncSession) -> None:
    user = await _seed_user(session)
    plan = await _seed_plan(session)
    named = PasarGuardAccount(
        user_id=user.id,
        panel_user_id=10,
        panel_username="svc_one_x1",
        subscription_url="https://db.example/sub/one",
        service_name="alpha",
        display_name="alpha",
    )
    other = PasarGuardAccount(
        user_id=user.id,
        panel_user_id=11,
        panel_username="svc_two_y2",
        subscription_url="https://db.example/sub/two",
        service_name="beta",
        display_name="beta",
    )
    session.add_all([named, other])
    await session.flush()
    order = await _add_order(
        session,
        user,
        plan,
        status=OrderStatus.ACTIVE,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    order.panel_username = named.panel_username
    await session.flush()

    panel = _panel_user(
        "svc_one_x1",
        used_traffic=1 * 1024**3,
        data_limit=10 * 1024**3,
        subscription_url="https://panel.example.com/sub/one",
    )
    overview = await get_account_overview(
        session,
        user_id=user.id,
        client=_client(panel),
        account_id=named.id,
    )
    assert overview.has_account is True
    assert overview.account_id == named.id
    assert overview.panel_username == "svc_one_x1"
    assert overview.display_name == "alpha"
    assert overview.subscription_url == "https://db.example/sub/one"

    missing = await get_account_overview(
        session,
        user_id=user.id,
        client=_client(),
        account_id=999_999,
    )
    assert missing.has_account is False


def test_account_label_uses_panel_username() -> None:
    account = PasarGuardAccount(
        user_id=1,
        panel_user_id=1,
        panel_username="yasin_u7jf",
        display_name="myvpn",
        service_name="myvpn",
    )
    assert account_label(account) == "yasin_u7jf"

    blank = PasarGuardAccount(
        user_id=1,
        panel_user_id=2,
        panel_username="   ",
        display_name="shown",
        service_name="svc",
    )
    assert account_label(blank) == "shown"


@pytest.mark.asyncio
async def test_live_refresh_keeps_stored_subscription_url(session: AsyncSession) -> None:
    """Panel returning a different sub URL on GET must not rotate the DB link."""
    user = await _seed_user(session)
    account = PasarGuardAccount(
        user_id=user.id,
        panel_user_id=99,
        panel_username="stable_sub",
        subscription_url="https://db.example/sub/stable_token",
    )
    session.add(account)
    await session.flush()

    panel = _panel_user(
        "stable_sub",
        subscription_url="https://panel.example.com/sub/rotated_token",
    )
    overview = await get_account_overview(session, user_id=user.id, client=_client(panel))
    assert overview.subscription_url == "https://db.example/sub/stable_token"
    await session.refresh(account)
    assert account.subscription_url == "https://db.example/sub/stable_token"


@pytest.mark.asyncio
async def test_live_refresh_fills_empty_subscription_url(session: AsyncSession) -> None:
    """When DB URL is empty, overview may fill from panel once."""
    user = await _seed_user(session)
    account = PasarGuardAccount(
        user_id=user.id,
        panel_user_id=100,
        panel_username="empty_sub",
        subscription_url=None,
    )
    session.add(account)
    await session.flush()

    panel = _panel_user(
        "empty_sub",
        subscription_url="https://panel.example.com/sub/filled",
    )
    overview = await get_account_overview(session, user_id=user.id, client=_client(panel))
    assert overview.subscription_url == "https://panel.example.com/sub/filled"
    await session.refresh(account)
    assert account.subscription_url == "https://panel.example.com/sub/filled"

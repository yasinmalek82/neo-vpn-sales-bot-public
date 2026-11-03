"""The value of a service's unused days, shown on the admin service card.

Owner's policy (2026-09-26): direct stopped working and support cannot fix it
→ the unused days go to the wallet toward the tunnel product. The bot shows
«paid ÷ period × days left»; the owner credits it by hand. Integer toman, no
float anywhere, and never more than was paid.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from bot.handlers.admin import _service_text
from database.base import Base
from database.models import Order, OrderStatus, PasarGuardAccount, Plan, User
from database.models.plan import DataLimitResetStrategy
from services.compensation import (
    UnusedValue,
    compute_unused_value,
    unused_value_for_account,
)
from services.service_ops import ServiceSnapshot

NOW = int(datetime(2026, 9, 26, 12, 0, tzinfo=UTC).timestamp())
DAY = 86_400


@pytest.mark.parametrize(
    ("paid", "period", "left_seconds", "expected"),
    [
        (150_000, 30, 10 * DAY, 50_000),
        (100_000, 30, 7 * DAY, 23_333),  # rounded down, whole toman
        (100_000, 30, 2 * DAY, 6_666),  # 6666.67: down, never up, never a float
        (150_000, 30, 10 * DAY + 23 * 3600, 50_000),  # a partial day is not a day
        (150_000, 30, 50 * DAY, 150_000),  # early renew: capped at what was paid
        (430_000, 90, 45 * DAY, 215_000),  # a 3-month plan is valued per its own days
    ],
)
def test_the_unused_value_is_whole_toman_and_never_more_than_paid(
    paid: int, period: int, left_seconds: int, expected: int
) -> None:
    value = compute_unused_value(
        paid_toman=paid, period_days=period, expire_unix=NOW + left_seconds, now_unix=NOW
    )
    assert value is not None
    assert value.amount_toman == expected
    assert isinstance(value.amount_toman, int)
    assert value.amount_toman <= paid


@pytest.mark.parametrize(
    ("paid", "period", "expire"),
    [
        (0, 30, NOW + 10 * DAY),  # a trial: nothing was paid
        (150_000, 30, None),  # no expiry
        (150_000, 30, NOW - DAY),  # already over
        (150_000, 30, NOW + 3600),  # less than a day left
        (150_000, 0, NOW + 10 * DAY),  # no period to divide by
    ],
)
def test_nothing_to_value(paid: int, period: int, expire: int | None) -> None:
    assert (
        compute_unused_value(paid_toman=paid, period_days=period, expire_unix=expire, now_unix=NOW)
        is None
    )


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def _seed(session: AsyncSession) -> tuple[User, PasarGuardAccount, Plan]:
    user = User(telegram_id=901, referral_code="CMP901", wallet_balance_toman=0)
    stranger = User(telegram_id=902, referral_code="CMP902", wallet_balance_toman=0)
    plan = Plan(
        name="unlimited-1u-1m",
        description="t",
        data_limit_gb=0,
        duration_days=30,
        price_toman=150_000,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[],
        hwid_limit=1,
        is_active=True,
    )
    session.add_all([user, stranger, plan])
    await session.flush()
    account = PasarGuardAccount(user_id=user.id, panel_user_id=1, panel_username="reza_de")
    session.add(account)
    await session.flush()

    def order(owner: User, price: int, status: OrderStatus, days: int = 30) -> Order:
        return Order(
            user_id=owner.id,
            plan_id=plan.id,
            status=status,
            price_toman=price,
            discount_toman=0,
            panel_username="reza_de",
            delivery_duration_days=days,
        )

    session.add_all(
        [
            order(user, 120_000, OrderStatus.ACTIVE),  # the older payment
            order(user, 150_000, OrderStatus.ACTIVE),  # the one to value
            order(user, 0, OrderStatus.ACTIVE),  # a free trial on the same name
            order(user, 999_000, OrderStatus.PROVISION_FAILED),  # never delivered
            order(stranger, 777_000, OrderStatus.ACTIVE),  # someone else's row
        ]
    )
    await session.flush()
    return user, account, plan


@pytest.mark.asyncio
async def test_the_last_paid_delivery_of_this_service_is_what_is_valued(
    session: AsyncSession,
) -> None:
    _user, account, _plan = await _seed(session)
    value = await unused_value_for_account(
        session,
        account,
        expire_unix=NOW + 10 * DAY,
        now=datetime.fromtimestamp(NOW, tz=UTC),
    )
    assert value == UnusedValue(
        amount_toman=50_000, paid_toman=150_000, period_days=30, days_left=10
    )


def _snapshot() -> ServiceSnapshot:
    return ServiceSnapshot(
        panel_username="reza_de",
        status="active",
        expire_unix=NOW + 10 * DAY,
        data_limit_bytes=None,
        used_traffic_bytes=0,
    )


def test_the_card_shows_the_figure_and_its_formula() -> None:
    text = _service_text(
        _snapshot(),
        name="reza",
        unused=UnusedValue(amount_toman=50_000, paid_toman=150_000, period_days=30, days_left=10),
    )
    assert "ارزش روزهای باقی‌مانده: ۵۰٬۰۰۰ تومان" in text
    assert "۱۵۰٬۰۰۰ پرداختی ÷ ۳۰ روز × ۱۰ روز" in text


def test_a_card_with_nothing_to_value_says_nothing() -> None:
    assert "ارزش" not in _service_text(_snapshot(), name="reza")


async def _plan_id(session: AsyncSession) -> int:
    plan = Plan(
        name="p90",
        description="t",
        data_limit_gb=0,
        duration_days=30,
        price_toman=90_000,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[],
        hwid_limit=1,
        is_active=True,
    )
    session.add(plan)
    await session.flush()
    return plan.id


@pytest.mark.asyncio
async def test_gifted_days_are_not_valued_at_the_paid_rate(session: AsyncSession) -> None:
    """Paid 90k/30d, day 28, admin gifts 30 days: only the 2 paid days left count."""
    user = User(telegram_id=903, referral_code="CMP903", wallet_balance_toman=0)
    session.add(user)
    await session.flush()
    plan_id = await _plan_id(session)
    account = PasarGuardAccount(user_id=user.id, panel_user_id=2, panel_username="gift_x")
    session.add(account)
    now = datetime.fromtimestamp(NOW, tz=UTC)
    session.add(
        Order(
            user_id=user.id,
            plan_id=plan_id,
            status=OrderStatus.ACTIVE,
            price_toman=90_000,
            discount_toman=0,
            panel_username="gift_x",
            delivery_duration_days=30,
            activated_at=datetime.fromtimestamp(NOW - 28 * DAY, tz=UTC),
        )
    )
    await session.flush()
    value = await unused_value_for_account(session, account, expire_unix=NOW + 32 * DAY, now=now)
    assert value is not None
    assert value.days_left == 2
    assert value.amount_toman == 6_000


async def _early_renewal(session: AsyncSession) -> PasarGuardAccount:
    """A 30-day order activated on day 0, renewed early on day 25: expiry is day 60."""
    user = User(telegram_id=904, referral_code="CMP904", wallet_balance_toman=0)
    session.add(user)
    await session.flush()
    plan_id = await _plan_id(session)
    account = PasarGuardAccount(user_id=user.id, panel_user_id=3, panel_username="renew_x")
    session.add(account)
    day0 = NOW - 40 * DAY
    for paid, activated, target in (
        (90_000, day0, None),
        (90_000, day0 + 25 * DAY, day0 + 60 * DAY),
    ):
        session.add(
            Order(
                user_id=user.id,
                plan_id=plan_id,
                status=OrderStatus.ACTIVE,
                price_toman=paid,
                discount_toman=0,
                panel_username="renew_x",
                delivery_duration_days=30,
                activated_at=datetime.fromtimestamp(activated, tz=UTC),
                renew_target_expire_unix=target,
            )
        )
    await session.flush()
    return account


@pytest.mark.asyncio
async def test_an_early_renewal_is_valued_from_the_old_expiry_not_from_activation(
    session: AsyncSession,
) -> None:
    """Day 40 of a renewal bought on day 25: 20 paid days left, not 15."""
    account = await _early_renewal(session)
    now = datetime.fromtimestamp(NOW, tz=UTC)
    value = await unused_value_for_account(session, account, expire_unix=NOW + 20 * DAY, now=now)
    assert value is not None
    assert (value.days_left, value.amount_toman) == (20, 60_000)
    # Day 55: activation + 30 is long past, but 5 paid days remain.
    later = datetime.fromtimestamp(NOW + 15 * DAY, tz=UTC)
    value = await unused_value_for_account(session, account, expire_unix=NOW + 20 * DAY, now=later)
    assert value is not None and value.days_left == 5


@pytest.mark.asyncio
async def test_days_gifted_after_an_early_renewal_are_not_valued(session: AsyncSession) -> None:
    account = await _early_renewal(session)
    now = datetime.fromtimestamp(NOW, tz=UTC)
    # extend_days +30 moved the panel expiry to day 90; the renewal still bought day 60.
    value = await unused_value_for_account(session, account, expire_unix=NOW + 50 * DAY, now=now)
    assert value is not None
    assert (value.days_left, value.amount_toman) == (20, 60_000)

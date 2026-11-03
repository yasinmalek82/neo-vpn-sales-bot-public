"""Unit tests for user onboarding repository."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import database.models  # noqa: F401 — register mappers
from database.base import Base
from database.repositories.user import get_or_create_user


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_create_user_and_referral(session) -> None:
    referrer, created, _ = await get_or_create_user(
        session,
        telegram_id=1001,
        username="ref",
        first_name="Ref",
        last_name=None,
        language_code="fa",
        admin_telegram_ids=[],
    )
    assert created is True
    assert referrer.referral_code

    invited, created2, note = await get_or_create_user(
        session,
        telegram_id=1002,
        username="new",
        first_name="New",
        last_name=None,
        language_code="fa",
        admin_telegram_ids=[1002],
        start_payload=referrer.referral_code,
    )
    assert created2 is True
    assert note == "saved"
    assert invited.referred_by_id == referrer.id
    assert invited.is_admin is True


@pytest.mark.asyncio
async def test_self_referral_ignored(session) -> None:
    user, _, _ = await get_or_create_user(
        session,
        telegram_id=2001,
        username=None,
        first_name="A",
        last_name=None,
        language_code="fa",
        admin_telegram_ids=[],
    )
    # Recreate path won't use payload; simulate new user with same telegram impossible.
    # New telegram trying to use own code can't happen on create of same id.
    other, created, note = await get_or_create_user(
        session,
        telegram_id=2002,
        username=None,
        first_name="B",
        last_name=None,
        language_code="fa",
        admin_telegram_ids=[],
        start_payload="NOPECODE",
    )
    assert created is True
    assert note == "invalid"
    assert other.referred_by_id is None


@pytest.mark.asyncio
async def test_existing_user_welcome_path(session) -> None:
    u1, c1, _ = await get_or_create_user(
        session,
        telegram_id=3001,
        username="a",
        first_name="A",
        last_name=None,
        language_code="fa",
        admin_telegram_ids=[3001],
    )
    assert c1 is True
    u2, c2, note = await get_or_create_user(
        session,
        telegram_id=3001,
        username="a2",
        first_name="A2",
        last_name=None,
        language_code="fa",
        admin_telegram_ids=[3001],
        start_payload="NOSUCHCODE",
    )
    assert c2 is False
    # A payload matching no referral code is reported, not silently dropped —
    # but it still attributes nobody.
    assert note == "invalid"
    assert u2.referred_by_id is None
    assert u2.id == u1.id
    assert u2.username == "a2"
    assert u2.is_admin is True


# --------------------------------------------------------------------------- #
# An invite link opened by someone already in the database
# --------------------------------------------------------------------------- #


async def _make(session, telegram_id: int, username: str, **kw):
    return await get_or_create_user(
        session,
        telegram_id=telegram_id,
        username=username,
        first_name=username,
        last_name=None,
        language_code="fa",
        admin_telegram_ids=[],
        **kw,
    )


@pytest.mark.asyncio
async def test_existing_user_who_never_bought_is_attributed(session) -> None:
    """The defect the owner hit.

    The third test account had pressed /start before, so `get_or_create_user`
    returned early and never even read the payload — the invite did nothing and
    said nothing. Someone who only pressed /start is not an established
    customer, so the referrer should get them.
    """
    referrer, _, _ = await _make(session, 2001, "ref")
    invitee, _, _ = await _make(session, 2002, "friend")
    assert invitee.referred_by_id is None

    invitee, created, note = await _make(
        session, 2002, "friend", start_payload=referrer.referral_code
    )
    assert created is False
    assert note == "saved"
    assert invitee.referred_by_id == referrer.id


@pytest.mark.asyncio
async def test_a_paying_customer_is_not_re_attributed(session) -> None:
    """Anti-abuse: otherwise anyone can harvest a customer they did not bring."""
    from datetime import UTC, datetime

    from database.models import Order, OrderStatus, Plan

    referrer, _, _ = await _make(session, 2003, "ref2")
    buyer, _, _ = await _make(session, 2004, "buyer")

    plan = Plan(name="vol-50-30", price_toman=150_000, data_limit_gb=50, duration_days=30)
    session.add(plan)
    await session.flush()
    session.add(
        Order(
            user_id=buyer.id,
            plan_id=plan.id,
            status=OrderStatus.ACTIVE,
            price_toman=150_000,
            discount_toman=0,
            paid_at=datetime.now(UTC),
        )
    )
    await session.commit()

    buyer, _, note = await _make(session, 2004, "buyer", start_payload=referrer.referral_code)
    assert note == "established"
    assert buyer.referred_by_id is None


@pytest.mark.asyncio
async def test_a_second_invite_link_does_not_steal_the_first_referrer(session) -> None:
    first, _, _ = await _make(session, 2005, "first")
    second, _, _ = await _make(session, 2006, "second")
    invitee, _, note = await _make(session, 2007, "invitee", start_payload=first.referral_code)
    assert note == "saved"

    invitee, _, note = await _make(session, 2007, "invitee", start_payload=second.referral_code)
    assert note == "already"
    assert invitee.referred_by_id == first.id


@pytest.mark.asyncio
async def test_own_link_is_refused_for_an_existing_user_too(session) -> None:
    user, _, _ = await _make(session, 2008, "solo")
    user, _, note = await _make(session, 2008, "solo", start_payload=user.referral_code)
    assert note == "self"
    assert user.referred_by_id is None


@pytest.mark.asyncio
async def test_no_payload_stays_silent(session) -> None:
    await _make(session, 2009, "quiet")
    _, created, note = await _make(session, 2009, "quiet")
    assert created is False
    assert note is None

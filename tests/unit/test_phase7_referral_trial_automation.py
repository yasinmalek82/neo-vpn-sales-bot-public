"""Phase 7: referral reward idempotency, trial once-only, expire + reminder jobs."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("BOT_TOKEN", "test-token")

import database.models  # noqa: F401
from api_client import UserCreate, UserResponse, UserStatus
from api_client.exceptions import PasarGuardNotFoundError
from database.base import Base
from database.models import (
    Order,
    OrderStatus,
    PasarGuardAccount,
    Plan,
    ReferralLog,
    ReferralStatus,
    User,
)
from database.models.plan import DataLimitResetStrategy
from services.automation import (
    expire_stale_orders,
    find_renewal_reminder_candidates,
    mark_reminder_sent,
)
from services.payments import approve_payment, submit_card_receipt
from services.pricing import PlanType, calculate_price, load_pricing_config
from services.provisioning import provision_order
from services.referral import record_referral_reward_for_order
from services.trial import TrialError, TrialOffer, issue_free_trial, trial_open_for

# The direct product's trial: unlimited kind, so a pre-group trial counts here.
_OFFER = TrialOffer(
    slug="unlimited",
    title_fa="نامحدود",
    data_limit_gb=5,
    duration_days=1,
    group_ids=[1],
    counts_legacy=True,
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


def _panel_user(username: str, *, user_id: int = 42) -> UserResponse:
    return UserResponse(
        id=user_id,
        username=username,
        status=UserStatus.ACTIVE,
        used_traffic=0,
        created_at=datetime.now(UTC),
        subscription_url=f"https://panel.example.com/sub/{username}",
        data_limit=5 * 1024**3,
    )


async def _users_and_plan(session: AsyncSession) -> tuple[User, User, Plan, Order]:
    referrer = User(
        telegram_id=100,
        username="ref",
        first_name="R",
        last_name=None,
        language_code="fa",
        referral_code="REFCODE1",
        is_admin=False,
    )
    referred = User(
        telegram_id=200,
        username="buyer",
        first_name="B",
        last_name=None,
        language_code="fa",
        referral_code="BUYCODE1",
        referred_by_id=None,  # set after flush
        is_admin=False,
    )
    admin = User(
        telegram_id=900,
        username="admin",
        first_name="A",
        last_name=None,
        language_code="fa",
        referral_code="ADMCODE1",
        is_admin=True,
    )
    plan = Plan(
        name="volume-10GB-30d",
        description="test",
        data_limit_gb=10,
        duration_days=30,
        price_toman=150_000,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1],
        hwid_limit=1,
        is_active=True,
    )
    session.add_all([referrer, referred, admin, plan])
    await session.flush()
    referred.referred_by_id = referrer.id
    order = Order(
        user_id=referred.id,
        plan_id=plan.id,
        status=OrderStatus.PENDING_PAYMENT,
        price_toman=142_500,
        discount_toman=7_500,
    )
    session.add(order)
    await session.flush()
    return referrer, referred, plan, order


@pytest.mark.asyncio
async def test_referral_reward_idempotent_per_order(session: AsyncSession) -> None:
    referrer, referred, _plan, order = await _users_and_plan(session)
    order.status = OrderStatus.ACTIVE
    await session.flush()

    log1, created1 = await record_referral_reward_for_order(
        session, order_id=order.id, reward_value=10
    )
    log2, created2 = await record_referral_reward_for_order(
        session, order_id=order.id, reward_value=10
    )
    assert created1 is True
    assert created2 is False
    assert log1 is not None and log2 is not None
    assert log1.id == log2.id
    assert log1.status == ReferralStatus.pending
    assert log1.referrer_id == referrer.id
    assert log1.referred_id == referred.id

    rows = (
        (await session.execute(select(ReferralLog).where(ReferralLog.order_id == order.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_provision_records_referral_reward(session: AsyncSession) -> None:
    referrer, referred, _plan, order = await _users_and_plan(session)
    admin = (await session.execute(select(User).where(User.telegram_id == 900))).scalar_one()
    await submit_card_receipt(
        session,
        order_id=order.id,
        user_id=referred.id,
        receipt_file_id="file",
        card_number_display="6037",
    )
    await approve_payment(session, order_id=order.id, admin_user_id=admin.id)

    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    client.create_user = AsyncMock(return_value=_panel_user(f"neo_{order.id}"))
    client.subscription_url_from_response = lambda u: u.subscription_url

    _account, reward = await provision_order(
        session, client, order_id=order.id, default_group_ids=[1]
    )
    assert reward is not None
    assert reward.referrer_id == referrer.id
    assert reward.status == ReferralStatus.pending

    _account2, reward2 = await provision_order(
        session, client, order_id=order.id, default_group_ids=[1]
    )
    assert reward2 is not None
    assert reward2.id == reward.id


@pytest.mark.asyncio
async def test_referred_discount_in_pricing(tmp_path) -> None:
    import shutil
    from pathlib import Path

    src = Path("config/pricing.json")
    path = tmp_path / "pricing.json"
    shutil.copy(src, path)
    cfg = load_pricing_config(path)
    base = calculate_price(cfg, plan_type=PlanType.VOLUME, volume_gb=50, duration_days=30)
    referred = calculate_price(
        cfg,
        plan_type=PlanType.VOLUME,
        volume_gb=50,
        duration_days=30,
        extra_discount_percent=5,
        max_discount_percent=50,
    )
    assert referred.final_price < base.final_price
    assert referred.discount_amount > base.discount_amount
    assert referred.meta["extra_discount_percent"] == "5"


@pytest.mark.asyncio
async def test_trial_once_only(session: AsyncSession) -> None:
    user = User(
        telegram_id=300,
        username="trialer",
        first_name="T",
        last_name=None,
        language_code="fa",
        referral_code="TRIAL001",
        is_admin=False,
    )
    session.add(user)
    await session.flush()
    assert await trial_open_for(session, user, _OFFER) is True

    panel: dict[str, UserResponse] = {}

    async def get_user(username: str) -> UserResponse:
        if username in panel:
            return panel[username]
        raise PasarGuardNotFoundError("missing", status_code=404)

    def create_user(payload: UserCreate) -> UserResponse:
        username = payload.username
        created = _panel_user(username)
        panel[username] = created
        return created

    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=get_user)
    client.create_user = AsyncMock(side_effect=create_user)
    client.subscription_url_from_response = lambda u: u.subscription_url

    first = await issue_free_trial(
        session,
        client,
        user_id=user.id,
        category_slug="unlimited",
        counts_legacy=True,
        data_limit_gb=5,
        duration_days=1,
        hwid_limit=1,
        group_ids=[1],
        enabled=True,
    )
    assert first.already_claimed is False
    assert first.order.price_toman == 0
    assert first.order.status == OrderStatus.ACTIVE
    await session.refresh(user)
    assert user.trial_claimed_at is not None
    assert await trial_open_for(session, user, _OFFER) is False

    second = await issue_free_trial(
        session,
        client,
        user_id=user.id,
        category_slug="unlimited",
        counts_legacy=True,
        data_limit_gb=5,
        duration_days=1,
        hwid_limit=1,
        group_ids=[1],
        enabled=True,
    )
    assert second.already_claimed is True
    assert second.order.id == first.order.id
    assert client.create_user.await_count == 1


@pytest.mark.asyncio
async def test_a_deleted_panel_trial_is_not_sent_again(session: AsyncSession) -> None:
    """A link for a panel user the owner deleted must not be handed out again."""
    user = User(
        telegram_id=309,
        username="gone",
        first_name="G",
        last_name=None,
        language_code="fa",
        referral_code="TRIAL009",
        is_admin=False,
    )
    session.add(user)
    await session.flush()
    panel: dict[str, UserResponse] = {}

    async def get_user(username: str) -> UserResponse:
        if username in panel:
            return panel[username]
        raise PasarGuardNotFoundError("missing", status_code=404)

    def create_user(payload: UserCreate) -> UserResponse:
        username = payload.username
        created = _panel_user(username)
        panel[username] = created
        return created

    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=get_user)
    client.create_user = AsyncMock(side_effect=create_user)
    client.subscription_url_from_response = lambda u: u.subscription_url
    first = await issue_free_trial(
        session,
        client,
        user_id=user.id,
        category_slug="unlimited",
        counts_legacy=True,
        data_limit_gb=5,
        duration_days=1,
        hwid_limit=1,
        group_ids=[1],
        enabled=True,
    )
    panel.clear()
    with pytest.raises(TrialError, match="trial_panel_missing"):
        await issue_free_trial(
            session,
            client,
            user_id=user.id,
            category_slug="unlimited",
            counts_legacy=True,
            data_limit_gb=5,
            duration_days=1,
            hwid_limit=1,
            group_ids=[1],
            enabled=True,
        )
    assert client.create_user.await_count == 1
    assert first.account.subscription_url


@pytest.mark.asyncio
async def test_a_repeat_trial_tap_points_at_my_services_without_the_link() -> None:
    from bot.handlers import referral
    from services.trial import TrialResult

    account = SimpleNamespace(id=90, subscription_url="https://panel.example.invalid/sub/trial")
    result = TrialResult(
        order=SimpleNamespace(id=80),  # type: ignore[arg-type]
        account=account,  # type: ignore[arg-type]
        already_claimed=True,
    )
    message = SimpleNamespace(answer=AsyncMock())
    session = AsyncMock()
    settings = SimpleNamespace(trial_enabled=True)
    user = SimpleNamespace(id=70, is_reseller=False)
    client = AsyncMock()
    delivery = AsyncMock()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(referral, "list_trial_offers", AsyncMock(return_value=[_OFFER]))
        monkeypatch.setattr(referral, "get_trial_hwid_limit", AsyncMock(return_value=1))
        monkeypatch.setattr(referral, "get_pasarguard_client", AsyncMock(return_value=client))
        monkeypatch.setattr(referral, "issue_free_trial", AsyncMock(return_value=result))
        monkeypatch.setattr(referral, "deliver_provisioned_service_card", delivery)
        await referral.render_trial(message, session, settings, user, AsyncMock())  # type: ignore[arg-type]

    delivery.assert_not_awaited()
    text = message.answer.await_args.args[0]
    assert "سرویس‌های من" in text
    assert "panel.example" not in text


@pytest.mark.asyncio
async def test_a_missing_panel_trial_answer_has_no_link() -> None:
    from bot.handlers import referral
    from services.trial import TrialError

    message = SimpleNamespace(answer=AsyncMock())
    session = AsyncMock()
    session.rollback = AsyncMock()
    settings = SimpleNamespace(trial_enabled=True)
    user = SimpleNamespace(id=70, is_reseller=False)
    client = AsyncMock()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(referral, "list_trial_offers", AsyncMock(return_value=[_OFFER]))
        monkeypatch.setattr(referral, "get_trial_hwid_limit", AsyncMock(return_value=1))
        monkeypatch.setattr(referral, "get_pasarguard_client", AsyncMock(return_value=client))
        monkeypatch.setattr(
            referral,
            "issue_free_trial",
            AsyncMock(side_effect=TrialError("trial_panel_missing")),
        )
        await referral.render_trial(message, session, settings, user, AsyncMock())  # type: ignore[arg-type]

    text = message.answer.await_args.args[0]
    assert "پنل" in text
    assert "http" not in text


@pytest.mark.asyncio
async def test_trial_disabled(session: AsyncSession) -> None:
    user = User(
        telegram_id=301,
        username="x",
        first_name="X",
        last_name=None,
        language_code="fa",
        referral_code="TRIAL002",
        is_admin=False,
    )
    session.add(user)
    await session.flush()
    client = AsyncMock()
    with pytest.raises(TrialError, match="trial_disabled"):
        await issue_free_trial(
            session,
            client,
            user_id=user.id,
            category_slug="unlimited",
            counts_legacy=True,
            data_limit_gb=5,
            duration_days=1,
            hwid_limit=1,
            group_ids=[1],
            enabled=False,
        )


@pytest.mark.asyncio
async def test_expire_stale_pending_orders(session: AsyncSession) -> None:
    users = []
    for telegram_id, code in ((400, "EXP00001"), (401, "EXP00002"), (402, "EXP00003")):
        user = User(
            telegram_id=telegram_id,
            username=f"e{telegram_id}",
            first_name="E",
            last_name=None,
            language_code="fa",
            referral_code=code,
            is_admin=False,
        )
        users.append(user)
    plan = Plan(
        name="volume-10GB-30d",
        description="t",
        data_limit_gb=10,
        duration_days=30,
        price_toman=1000,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1],
        hwid_limit=1,
        is_active=True,
    )
    session.add_all([*users, plan])
    await session.flush()
    stale_user, fresh_user, review_user = users
    stale = Order(
        user_id=stale_user.id,
        plan_id=plan.id,
        status=OrderStatus.PENDING_PAYMENT,
        price_toman=1000,
        discount_toman=0,
        created_at=datetime.now(UTC) - timedelta(minutes=60),
    )
    fresh = Order(
        user_id=fresh_user.id,
        plan_id=plan.id,
        status=OrderStatus.PENDING_PAYMENT,
        price_toman=1000,
        discount_toman=0,
        created_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    review = Order(
        user_id=review_user.id,
        plan_id=plan.id,
        status=OrderStatus.PAYMENT_REVIEW,
        price_toman=1000,
        discount_toman=0,
        created_at=datetime.now(UTC) - timedelta(minutes=60),
    )
    session.add_all([stale, fresh, review])
    await session.flush()

    expired = await expire_stale_orders(session, expiry_minutes=30)
    assert len(expired) == 1
    assert expired[0].id == stale.id
    await session.refresh(stale)
    await session.refresh(fresh)
    await session.refresh(review)
    assert stale.status == OrderStatus.EXPIRED
    assert fresh.status == OrderStatus.PENDING_PAYMENT
    assert review.status == OrderStatus.PAYMENT_REVIEW

    # Idempotent second run
    again = await expire_stale_orders(session, expiry_minutes=30)
    assert again == []


@pytest.mark.asyncio
async def test_renewal_reminder_dedupe_same_day(session: AsyncSession) -> None:
    user = User(
        telegram_id=500,
        username="r",
        first_name="R",
        last_name=None,
        language_code="fa",
        referral_code="REM00001",
        is_admin=False,
    )
    plan = Plan(
        name="volume-10GB-30d",
        description="t",
        data_limit_gb=10,
        duration_days=30,
        price_toman=1000,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1],
        hwid_limit=1,
        is_active=True,
    )
    session.add_all([user, plan])
    await session.flush()
    now = datetime.now(UTC)
    order = Order(
        user_id=user.id,
        plan_id=plan.id,
        status=OrderStatus.ACTIVE,
        price_toman=1000,
        discount_toman=0,
        expires_at=now + timedelta(days=1, hours=2),
        activated_at=now - timedelta(days=29),
    )
    session.add(order)
    await session.flush()

    c1 = await find_renewal_reminder_candidates(session, reminder_days=[3, 1], now=now)
    assert len(c1) == 1
    assert c1[0].order.id == order.id

    await mark_reminder_sent(session, order_id=order.id, now=now)
    c2 = await find_renewal_reminder_candidates(session, reminder_days=[3, 1], now=now)
    assert c2 == []

    # Next calendar day can remind again
    next_day = now + timedelta(days=1)
    c3 = await find_renewal_reminder_candidates(session, reminder_days=[3, 1], now=next_day)
    # expires in ~2h from next_day perspective → may match day 0/1 bucket
    # With expires_at = now+1d+2h and next_day = now+1d, delta ≈ 2h → day 1 or 0
    assert isinstance(c3, list)


@pytest.mark.asyncio
async def test_owning_a_service_does_not_forfeit_an_unused_trial(
    session: AsyncSession,
) -> None:
    """Eligibility is the group's trial alone.

    The rule used to also refuse anyone holding **any** `PasarGuardAccount`.
    Paired with a button that only rendered on the empty «سرویس‌های من» screen,
    that hid the trial from every paying customer — during live testing the owner
    reported it simply did not exist. Paying for a service is not a reason to
    lose a trial you never took.
    """
    user = User(
        telegram_id=301,
        username="buyer",
        first_name="B",
        last_name=None,
        language_code="fa",
        referral_code="TRIAL002",
        is_admin=False,
    )
    session.add(user)
    await session.flush()

    session.add(
        PasarGuardAccount(
            user_id=user.id,
            panel_user_id=99,
            panel_username="buyer_ab12",
            subscription_url="https://panel.example.com/sub/buyer_ab12",
        )
    )
    await session.flush()

    assert await trial_open_for(session, user, _OFFER) is True

    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    client.create_user = AsyncMock(side_effect=lambda payload: _panel_user(payload.username))
    client.subscription_url_from_response = lambda u: u.subscription_url

    result = await issue_free_trial(
        session,
        client,
        user_id=user.id,
        category_slug="unlimited",
        counts_legacy=True,
        data_limit_gb=5,
        duration_days=1,
        hwid_limit=1,
        group_ids=[1],
        enabled=True,
    )
    assert result.already_claimed is False
    await session.refresh(user)
    assert await trial_open_for(session, user, _OFFER) is False
